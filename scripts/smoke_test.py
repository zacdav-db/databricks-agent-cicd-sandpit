"""End-to-end checks for deployed apps, MCP tools, UC function, and trace tables."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import time
from datetime import datetime
from typing import Any

from app_names import (
    langchain_agent_app_name,
    mcp_app_name,
    omnigent_app_name,
)
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config


def _progress(message: str) -> None:
    print(f"[smoke] {message}", flush=True)


def _api_json(
    client: WorkspaceClient,
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload = client.api_client.do(
        method,
        url=url,
        body=body,
        headers=headers,
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} {url} returned a non-object response.")
    return payload


def _responses_stream(
    client: WorkspaceClient,
    url: str,
    message: str,
) -> dict[str, Any]:
    """Call the standard Responses API and validate its SSE lifecycle."""
    response = client.api_client.do(
        "POST",
        url=f"{url.rstrip('/')}/responses",
        body={
            "input": [{"role": "user", "content": message}],
            "stream": True,
        },
        headers={
            "Accept": "text/event-stream",
            "x-mlflow-return-trace-id": "true",
        },
        raw=True,
        response_headers=["Content-Type"],
    )
    if not isinstance(response, dict) or "contents" not in response:
        raise RuntimeError("Responses streaming request returned no response body.")
    content_type = response.get("Content-Type") or ""
    if "text/event-stream" not in content_type:
        raise RuntimeError(f"Responses API did not return SSE: {content_type!r}")
    with response["contents"] as contents:
        response_text = contents.read().decode("utf-8")

    raw_events = [
        line.removeprefix("data:").strip()
        for line in response_text.splitlines()
        if line.startswith("data:")
    ]
    if not raw_events or raw_events[-1] != "[DONE]":
        raise RuntimeError("Responses stream did not end with the [DONE] sentinel.")
    events = [json.loads(value) for value in raw_events if value != "[DONE]"]
    error = next((event.get("error") for event in events if event.get("error")), None)
    if error:
        raise RuntimeError(f"Responses stream returned an error: {error}")

    deltas = [
        event["delta"]
        for event in events
        if event.get("type") == "response.output_text.delta"
    ]
    done = next(
        (
            event
            for event in events
            if event.get("type") == "response.output_item.done"
        ),
        None,
    )
    trace_id = next(
        (
            event.get("trace_id")
            for event in events
            if isinstance(event.get("trace_id"), str)
        ),
        None,
    )
    if len(deltas) < 2 or not done or not trace_id:
        raise RuntimeError(
            "Responses stream must contain multiple deltas, one completed item, "
            "and a trace ID.",
        )
    output = "".join(deltas)
    item = done.get("item") or {}
    final_text = "".join(
        part.get("text", "")
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )
    if not output or final_text != output:
        raise RuntimeError(
            "Responses stream deltas did not match its completed output item.",
        )
    return {
        "delta_count": len(deltas),
        "output": output,
        "trace_id": trace_id,
    }


def _assert_playground_agent(
    client: WorkspaceClient,
    app_name: str,
    app_url: str,
) -> dict[str, Any]:
    """Verify the Databricks App contract used by AI Playground discovery."""
    if not app_name.startswith("agent-"):
        raise RuntimeError(
            f"ResponsesAgent App name must start with agent-: {app_name}",
        )
    info = _api_json(client, "GET", f"{app_url.rstrip('/')}/agent/info")
    if info.get("use_case") != "agent" or info.get("agent_api") != "responses":
        raise RuntimeError(f"{app_name} is not a ResponsesAgent App: {info}")
    return info


def _mcp_json_payloads(result: dict[str, Any]) -> list[Any]:
    payloads: list[Any] = []
    structured = result.get("structuredContent")
    if structured is not None:
        payloads.append(structured)
    for item in result.get("content", []):
        if item.get("type") != "text":
            continue
        try:
            payloads.append(json.loads(item.get("text", "")))
        except json.JSONDecodeError:
            continue
    return payloads


def _mcp_first_cell(result: dict[str, Any]) -> Any:
    for payload in _mcp_json_payloads(result):
        candidate = payload.get("result", payload) if isinstance(payload, dict) else payload
        rows = candidate.get("rows") if isinstance(candidate, dict) else None
        if rows and rows[0]:
            return rows[0][0]
    raise RuntimeError(f"Managed MCP result did not contain a row: {result}")


def _mcp_scalar(result: dict[str, Any]) -> str:
    """Return one scalar value from an MCP tool response."""
    for payload in _mcp_json_payloads(result):
        candidate = payload.get("result", payload) if isinstance(payload, dict) else payload
        if isinstance(candidate, str | int | float | bool):
            return str(candidate)
    raise RuntimeError(f"MCP result did not contain a scalar value: {result}")


def _wait_for_app(client: WorkspaceClient, name: str, timeout: int = 900) -> str:
    deadline = time.monotonic() + timeout
    last_status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        app = client.apps.get(name=name)
        last_status = app.as_dict()
        if last_status.get("app_status", {}).get("state") == "RUNNING" and app.url:
            return app.url.rstrip("/")
        time.sleep(10)
    raise TimeoutError(f"App {name} did not become ready: {last_status}")


def _execute(client: WorkspaceClient, warehouse_id: str, statement: str) -> dict[str, Any]:
    response = client.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=statement,
        wait_timeout="50s",
    ).as_dict()
    if response.get("status", {}).get("state") != "SUCCEEDED":
        raise RuntimeError(response)
    return response


def _wait_for_trace(
    client: WorkspaceClient,
    warehouse_id: str,
    trace_table: str,
    raw_trace_id: str,
    timeout: int = 180,
    root_span_name: str | None = None,
) -> dict[str, int]:
    trace_id = raw_trace_id.rsplit("/", maxsplit=1)[-1]
    if not trace_id.isalnum():
        raise RuntimeError(f"Unexpected trace ID format: {trace_id}")
    child_expression = "0"
    if root_span_name:
        escaped_root_name = root_span_name.replace("'", "''")
        child_expression = (
            "COUNT_IF(parent_span_id = ("
            f"SELECT MAX(span_id) FROM {trace_table} "
            f"WHERE trace_id = '{trace_id}' AND name = '{escaped_root_name}'"
            "))"
        )
    deadline = time.monotonic() + timeout
    trace_result: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        trace_result = _execute(
            client,
            warehouse_id,
            (
                "SELECT COUNT(*) AS trace_rows, "
                f"{child_expression} AS direct_child_rows FROM {trace_table} "
                f"WHERE trace_id = '{trace_id}'"
            ),
        )
        rows = trace_result.get("result", {}).get("data_array", [])
        if rows:
            counts = {
                "trace_rows": int(rows[0][0]),
                "direct_child_rows": int(rows[0][1]),
            }
            if counts["trace_rows"] > 0 and (
                root_span_name is None or counts["direct_child_rows"] > 0
            ):
                return counts
        time.sleep(10)
    expected = (
        f"a child of {root_span_name!r}"
        if root_span_name
        else "at least one trace row"
    )
    raise RuntimeError(
        f"Trace {trace_id} did not contain {expected} in {trace_table}: "
        f"{trace_result}",
    )


def _mcp_request(
    client: WorkspaceClient,
    server_url: str,
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    response = client.api_client.do(
        "POST",
        url=server_url,
        headers={"Accept": "application/json, text/event-stream"},
        body={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        },
        raw=True,
        response_headers=["Content-Type"],
    )
    if not isinstance(response, dict) or "contents" not in response:
        raise RuntimeError(f"MCP request {method} returned no response body.")
    with response["contents"] as contents:
        response_text = contents.read().decode("utf-8")
    if "text/event-stream" in (response.get("Content-Type") or ""):
        messages = [
            json.loads(line.removeprefix("data:").strip())
            for line in response_text.splitlines()
            if line.startswith("data:")
        ]
        payload = next(
            (message for message in messages if message.get("id") == request_id),
            None,
        )
    else:
        payload = json.loads(response_text)
    if not payload or "error" in payload:
        raise RuntimeError(f"MCP request {method} failed: {payload or response_text}")
    return payload["result"]


def _smoke_omnigent_delegation(
    client: WorkspaceClient,
    *,
    omnigent_url: str,
    agent_id: str,
    expected_identity: str,
    warehouse_id: str,
    trace_table: str,
) -> dict[str, Any]:
    """Exercise approval, direct LangChain delegation, custom MCP, and tracing."""
    session_id: str | None = None
    try:
        session = _api_json(
            client,
            "POST",
            f"{omnigent_url}/v1/sessions",
            body={"agent_id": agent_id},
            headers={"Origin": "omnigent://internal"},
        )
        session_id = str(session["id"])

        deadline = time.monotonic() + 180
        host: dict[str, Any] | None = None
        hosts: dict[str, Any] = {}
        while time.monotonic() < deadline:
            hosts = _api_json(client, "GET", f"{omnigent_url}/v1/hosts")
            host = next(
                (
                    item
                    for item in hosts.get("hosts", [])
                    if item.get("status") == "online"
                ),
                None,
            )
            if host is not None:
                break
            time.sleep(3)
        if host is None:
            raise TimeoutError(f"Omnigent has no online host: {hosts}")
        host_id = str(host["host_id"])
        filesystem = _api_json(
            client,
            "GET",
            f"{omnigent_url}/v1/hosts/{host_id}/filesystem?limit=1",
        )
        entries = filesystem.get("data", [])
        if not entries or "/" not in str(entries[0].get("path", "")):
            raise RuntimeError(f"Could not resolve the Omnigent host home: {filesystem}")
        workspace = str(entries[0]["path"]).rsplit("/", maxsplit=1)[0]
        _api_json(
            client,
            "POST",
            f"{omnigent_url}/v1/hosts/{host_id}/runners",
            body={"session_id": session_id, "workspace": workspace},
        )

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            snapshot = _api_json(
                client,
                "GET",
                f"{omnigent_url}/v1/sessions/{session_id}",
            )
            if snapshot.get("runner_id") and snapshot.get("runner_online"):
                break
            time.sleep(2)
        else:
            raise TimeoutError("Omnigent did not bind an online runner.")

        _api_json(
            client,
            "POST",
            f"{omnigent_url}/v1/sessions/{session_id}/events",
            body={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Delegate to databricks_agent. Ask LangChain to call "
                                "get_current_identity from the custom MCP. Return the "
                                "identity and LangChain trace ID."
                            ),
                        },
                    ],
                },
            },
        )

        deadline = time.monotonic() + 180
        elicitation: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            snapshot = _api_json(
                client,
                "GET",
                f"{omnigent_url}/v1/sessions/{session_id}",
            )
            elicitation = next(
                (
                    item
                    for item in snapshot.get("pending_elicitations", [])
                    if item.get("params", {}).get("policy_name")
                    == "approve_subagent_spawn"
                ),
                None,
            )
            if elicitation is not None:
                break
            if snapshot.get("status") == "failed":
                raise RuntimeError(f"Omnigent delegation failed: {snapshot}")
            time.sleep(2)
        if elicitation is None:
            raise TimeoutError("Omnigent did not request subagent approval.")

        elicitation_id = str(elicitation["elicitation_id"])
        _api_json(
            client,
            "POST",
            (
                f"{omnigent_url}/v1/sessions/{session_id}/elicitations/"
                f"{elicitation_id}/resolve"
            ),
            body={"action": "accept"},
        )

        deadline = time.monotonic() + 300
        final_snapshot: dict[str, Any] | None = None
        trace_ids: list[str] = []
        while time.monotonic() < deadline:
            snapshot = _api_json(
                client,
                "GET",
                f"{omnigent_url}/v1/sessions/{session_id}",
            )
            if snapshot.get("status") == "failed":
                raise RuntimeError(f"Omnigent delegation failed: {snapshot}")
            if snapshot.get("status") == "idle" and len(snapshot.get("items", [])) > 1:
                transcript = json.dumps(snapshot.get("items", []))
                trace_ids = re.findall(
                    r'trace:/[^\s`"}]+/([0-9a-fA-F]{32})',
                    transcript,
                )
                if expected_identity in transcript and trace_ids:
                    final_snapshot = snapshot
                    break
            time.sleep(3)
        if final_snapshot is None:
            raise TimeoutError(
                "Omnigent delegation did not return the custom MCP identity and "
                "LangChain trace ID.",
            )

        trace_id = trace_ids[-1]
        _wait_for_trace(
            client,
            warehouse_id,
            trace_table,
            trace_id,
        )
        return {
            "approval_policy": "approve_subagent_spawn",
            "custom_mcp_identity": expected_identity,
            "langchain_trace_id": trace_id,
        }
    finally:
        if session_id is not None:
            with contextlib.suppress(Exception):
                client.api_client.do(
                    "DELETE",
                    url=f"{omnigent_url}/v1/sessions/{session_id}",
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    parser.add_argument("--target", required=True, choices=("dev", "prod"))
    parser.add_argument(
        "--warehouse-id",
        default=os.getenv("DATABRICKS_WAREHOUSE_ID", "f7a871ffa2a9ab80"),
    )
    parser.add_argument("--trace-table")
    parser.add_argument("--uc-function")
    parser.add_argument("--uc-time-function")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    catalog = os.getenv("UC_CATALOG", "zacdav_sandpit_catalog")
    schema = os.getenv("UC_SCHEMA", f"{args.target}_agent_cicd")
    trace_prefix = os.getenv(
        "UC_TRACE_TABLE_PREFIX",
        f"{args.target}_sandpit_agent_cicd",
    )
    args.trace_table = (
        args.trace_table or f"{catalog}.{schema}.{trace_prefix}_otel_spans"
    )
    args.uc_function = (
        args.uc_function
        or f"{catalog}.{schema}.{args.target}_estimate_project_cost"
    )
    args.uc_time_function = (
        args.uc_time_function
        or f"{catalog}.{schema}.{args.target}_current_utc_timestamp"
    )
    client = WorkspaceClient(
        config=Config(
            profile=args.profile,
            http_timeout_seconds=180,
        ),
    )
    mcp_url = _wait_for_app(client, mcp_app_name(args.target))
    agent_url = _wait_for_app(client, langchain_agent_app_name(args.target))
    omnigent_url = _wait_for_app(client, omnigent_app_name(args.target))
    _progress("All three runtime-example Databricks Apps report RUNNING.")

    _api_json(client, "GET", f"{agent_url}/api/health")

    invocation_payload = _api_json(
        client,
        "POST",
        f"{agent_url}/api/invocations",
        body={"input": "Estimate 8 hours at $125/hour with 10 percent contingency."},
    )
    if not invocation_payload.get("trace_id"):
        raise RuntimeError(f"Agent did not return a trace ID: {invocation_payload}")
    if "1100" not in invocation_payload.get("output", "").replace(",", ""):
        raise RuntimeError(f"Agent returned an unexpected estimate: {invocation_payload}")
    _progress("LangChain invocation returned the expected estimate and a trace ID.")

    mcp_endpoint = f"{mcp_url}/mcp"
    _mcp_request(
        client,
        mcp_endpoint,
        1,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "deployment-smoke-test", "version": "1.0"},
        },
    )
    tools_result = _mcp_request(client, mcp_endpoint, 2, "tools/list", {})
    tool_names = {tool["name"] for tool in tools_result["tools"]}
    required_tools = {
        "health",
        "uppercase",
        "get_current_identity",
    }
    if not required_tools.issubset(tool_names):
        raise RuntimeError(f"Missing MCP tools: {required_tools - tool_names}")
    if "invoke_langchain_agent" in tool_names:
        raise RuntimeError("Custom MCP must not proxy back to the LangChain App.")
    mcp_result = _mcp_request(
        client,
        mcp_endpoint,
        3,
        "tools/call",
        {"name": "uppercase", "arguments": {"text": "bundle deployed"}},
    )
    if "BUNDLE DEPLOYED" not in json.dumps(mcp_result):
        raise RuntimeError(f"Unexpected MCP result: {mcp_result}")
    identity_result = _mcp_request(
        client,
        mcp_endpoint,
        4,
        "tools/call",
        {"name": "get_current_identity", "arguments": {}},
    )
    custom_mcp_identity = _mcp_scalar(identity_result)
    _progress(f"Custom MCP exposed and executed all {len(tool_names)} tools.")

    custom_tool_payload = _api_json(
        client,
        "POST",
        f"{agent_url}/api/invocations",
        body={
            "input": (
                "Call get_current_identity from the custom MCP and return its "
                "exact value."
            ),
        },
    )
    if not custom_tool_payload.get("trace_id"):
        raise RuntimeError(
            f"LangChain custom-MCP call returned no trace ID: {custom_tool_payload}",
        )
    if custom_mcp_identity not in custom_tool_payload.get("output", ""):
        raise RuntimeError(
            f"LangChain custom-MCP call returned an unexpected result: "
            f"{custom_tool_payload}",
        )
    _progress("LangChain returned the non-inferable custom MCP identity.")

    streaming_payload = _responses_stream(
        client,
        agent_url,
        "Reply with one complete sentence confirming that streaming is working.",
    )
    _progress(
        "LangChain returned a standard Responses API SSE stream with "
        f"{streaming_payload['delta_count']} text deltas.",
    )

    managed_results: dict[str, Any] = {}
    for request_id, (function_name, arguments) in enumerate(
        (
            (
                args.uc_function,
                {"hours": 8, "hourly_rate": 125, "contingency_percent": 10},
            ),
            (args.uc_time_function, {}),
        ),
        start=10,
    ):
        parts = function_name.split(".")
        if len(parts) != 3 or any(not part.replace("_", "").isalnum() for part in parts):
            raise RuntimeError(f"Invalid Unity Catalog function name: {function_name}")
        endpoint = (
            f"{client.config.host.rstrip('/')}/api/2.0/mcp/functions/"
            f"{'/'.join(parts)}"
        )
        _mcp_request(
            client,
            endpoint,
            request_id,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "deployment-smoke-test", "version": "1.0"},
            },
        )
        listed = _mcp_request(
            client,
            endpoint,
            request_id + 100,
            "tools/list",
            {},
        )
        expected_tool = "__".join(parts)
        listed_names = {tool["name"] for tool in listed["tools"]}
        if expected_tool not in listed_names:
            raise RuntimeError(
                f"Managed MCP did not expose {expected_tool}: {sorted(listed_names)}",
            )
        managed_results[function_name] = _mcp_request(
            client,
            endpoint,
            request_id + 200,
            "tools/call",
            {"name": expected_tool, "arguments": arguments},
        )
    cost_value = _mcp_first_cell(managed_results[args.uc_function])
    if float(cost_value) != 1100.0:
        raise RuntimeError(
            f"Managed MCP returned an unexpected cost: {managed_results[args.uc_function]}",
        )
    time_value = str(_mcp_first_cell(managed_results[args.uc_time_function]))
    try:
        parsed_time = datetime.fromisoformat(time_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(
            f"Managed MCP returned an invalid UTC timestamp: {time_value!r}",
        ) from exc
    if parsed_time.tzinfo is None or not time_value.endswith("Z"):
        raise RuntimeError(
            f"Managed MCP returned a non-UTC timestamp: {time_value!r}",
        )
    _progress(
        "Databricks managed MCP listed and executed both Unity Catalog functions.",
    )

    agent_service_name = (
        f"{args.uc_function.rsplit('.', maxsplit=1)[0]}."
        f"{args.target}_sandpit_langchain_agent"
    )
    agent_service = client.ai_gateway.get_agent_service(
        f"agent-services/{agent_service_name}",
    )
    if (agent_service.name or "").removeprefix("agent-services/") != agent_service_name:
        raise RuntimeError(f"Unexpected UC Agent Service: {agent_service}")
    if agent_service.config is None or agent_service.config.base_path != "/responses":
        raise RuntimeError(f"Unexpected Agent Service base path: {agent_service}")
    _progress("The LangChain App is discoverable as a Unity Catalog Agent Service.")

    _wait_for_trace(
        client,
        args.warehouse_id,
        args.trace_table,
        invocation_payload["trace_id"],
    )
    _wait_for_trace(
        client,
        args.warehouse_id,
        args.trace_table,
        custom_tool_payload["trace_id"],
    )
    _wait_for_trace(
        client,
        args.warehouse_id,
        args.trace_table,
        streaming_payload["trace_id"],
    )
    _progress("All LangChain traces are queryable in the Unity Catalog spans table.")

    _api_json(client, "GET", f"{omnigent_url}/health")

    deadline = time.monotonic() + 120
    omnigent_agent: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        agents_response = _api_json(client, "GET", f"{omnigent_url}/v1/agents")
        omnigent_agent = next(
            (
                agent
                for agent in agents_response.get("data", [])
                if agent.get("name") == "sandpit_supervisor"
            ),
            None,
        )
        if omnigent_agent:
            break
        time.sleep(10)
    if omnigent_agent is None:
        raise RuntimeError("Omnigent supervisor did not become ready.")

    # Omnigent 0.6 does not reliably expose YAML function policies through the
    # agent listing API. scripts/validate_omnigent.py validates both definitions.
    # The launcher supervises the colocated host process and exits if it fails.
    policy_names = {policy["name"] for policy in omnigent_agent.get("policies", [])}
    mcp_servers = {server["name"] for server in omnigent_agent.get("mcp_servers", [])}
    if mcp_servers:
        raise RuntimeError(
            f"Omnigent must delegate to LangChain, not MCP directly: {mcp_servers}",
        )
    omnigent_delegation = _smoke_omnigent_delegation(
        client,
        omnigent_url=omnigent_url,
        agent_id=str(omnigent_agent["id"]),
        expected_identity=custom_mcp_identity,
        warehouse_id=args.warehouse_id,
        trace_table=args.trace_table,
    )
    _progress(
        "Omnigent approval, LangChain delegation, custom MCP, and trace "
        "completed end to end.",
    )

    print(
        json.dumps(
            {
                "agent": invocation_payload,
                "agent_service": agent_service_name,
                "managed_mcp_functions": managed_results,
                "mcp_tool_count": len(tool_names),
                "mcp_uppercase": mcp_result,
                "langchain_custom_mcp": custom_tool_payload,
                "langchain_streaming": streaming_payload,
                "omnigent_delegation": omnigent_delegation,
                "omnigent_policies": sorted(policy_names),
                "omnigent_supervisor": omnigent_agent["name"],
                "omnigent_url": omnigent_url,
                "trace_table": args.trace_table,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()
