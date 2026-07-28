"""End-to-end checks for deployed apps, MCP tools, UC function, and trace tables."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from typing import Any

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
) -> dict[str, Any]:
    payload = client.api_client.do(method, url=url, body=body)
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} {url} returned a non-object response.")
    return payload


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


def _find_nonempty_string(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        for child in value.values():
            found = _find_nonempty_string(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_nonempty_string(child, key)
            if found:
                return found
    return None


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
) -> None:
    trace_id = raw_trace_id.rsplit("/", maxsplit=1)[-1]
    if not trace_id.isalnum():
        raise RuntimeError(f"Unexpected trace ID format: {trace_id}")
    deadline = time.monotonic() + timeout
    trace_result: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        trace_result = _execute(
            client,
            warehouse_id,
            (
                f"SELECT COUNT(*) AS trace_rows FROM {trace_table} "
                f"WHERE trace_id = '{trace_id}'"
            ),
        )
        rows = trace_result.get("result", {}).get("data_array", [])
        if rows and int(rows[0][0]) > 0:
            return
        time.sleep(10)
    raise RuntimeError(f"No trace rows appeared in {trace_table}: {trace_result}")


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
    prefix = args.target
    mcp_url = _wait_for_app(client, f"{prefix}-sandpit-mcp-tools")
    agent_url = _wait_for_app(client, f"{prefix}-sandpit-langchain-agent")
    omnigent_url = _wait_for_app(client, f"{prefix}-sandpit-omnigent")
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
        "invoke_langchain_agent",
    }
    if not required_tools.issubset(tool_names):
        raise RuntimeError(f"Missing MCP tools: {required_tools - tool_names}")
    mcp_result = _mcp_request(
        client,
        mcp_endpoint,
        3,
        "tools/call",
        {"name": "uppercase", "arguments": {"text": "bundle deployed"}},
    )
    if "BUNDLE DEPLOYED" not in json.dumps(mcp_result):
        raise RuntimeError(f"Unexpected MCP result: {mcp_result}")
    bridge_result = _mcp_request(
        client,
        mcp_endpoint,
        4,
        "tools/call",
        {
            "name": "invoke_langchain_agent",
            "arguments": {
                "message": "Estimate 5 hours at $200/hour with no contingency.",
            },
        },
    )
    bridge_trace_id = next(
        (
            trace_id
            for payload in _mcp_json_payloads(bridge_result)
            if (trace_id := _find_nonempty_string(payload, "trace_id"))
        ),
        None,
    )
    if not bridge_trace_id:
        raise RuntimeError(f"LangChain bridge returned no trace ID: {bridge_result}")
    _progress(f"Custom MCP exposed and executed all {len(tool_names)} tools.")

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
        f"{prefix}_sandpit_langchain_agent"
    )
    agent_service = _api_json(
        client,
        "GET",
        (
            f"{client.config.host.rstrip('/')}/api/2.1/unity-catalog/"
            f"agent-services/{agent_service_name}"
        ),
    )
    if agent_service.get("name", "").removeprefix("agent-services/") != agent_service_name:
        raise RuntimeError(f"Unexpected UC Agent Service: {agent_service}")
    if agent_service.get("config", {}).get("base_path") != "/api/invocations":
        raise RuntimeError(f"Unexpected Agent Service base path: {agent_service}")
    _progress("The LangChain App is discoverable as a Unity Catalog Agent Service.")

    _wait_for_trace(
        client,
        args.warehouse_id,
        args.trace_table,
        invocation_payload["trace_id"],
    )
    _progress("The LangChain trace is queryable in the Unity Catalog spans table.")

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
    required_mcp_servers = {"custom_mcp", "project_cost"}
    if not required_mcp_servers.issubset(mcp_servers):
        raise RuntimeError(
            f"Missing Omnigent MCP configuration: {required_mcp_servers - mcp_servers}",
        )
    _progress("Omnigent supervisor and both MCP integrations are registered.")

    print(
        json.dumps(
            {
                "agent": invocation_payload,
                "agent_service": agent_service_name,
                "managed_mcp_functions": managed_results,
                "mcp_tool_count": len(tool_names),
                "mcp_uppercase": mcp_result,
                "mcp_langchain_bridge": bridge_result,
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
