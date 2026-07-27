"""End-to-end checks for deployed apps, MCP tools, UC function, and trace tables."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

import requests
from databricks.sdk import WorkspaceClient


def _headers(client: WorkspaceClient) -> dict[str, str]:
    headers = client.config.authenticate()
    headers["Content-Type"] = "application/json"
    return headers


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


def _mcp_request(
    server_url: str,
    headers: dict[str, str],
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    response = requests.post(
        server_url,
        headers={**headers, "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        },
        timeout=180,
    )
    response.raise_for_status()
    if "text/event-stream" in response.headers.get("Content-Type", ""):
        messages = [
            json.loads(line.removeprefix("data:").strip())
            for line in response.text.splitlines()
            if line.startswith("data:")
        ]
        payload = next(
            (message for message in messages if message.get("id") == request_id),
            None,
        )
    else:
        payload = response.json()
    if not payload or "error" in payload:
        raise RuntimeError(f"MCP request {method} failed: {payload or response.text}")
    return payload["result"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    parser.add_argument("--target", default="prod")
    parser.add_argument("--warehouse-id", default="f7a871ffa2a9ab80")
    parser.add_argument(
        "--trace-table",
        default="zacdav_sandpit_catalog.default.sandpit_agent_cicd_otel_spans",
    )
    parser.add_argument(
        "--uc-function",
        default="zacdav_sandpit_catalog.default.estimate_project_cost",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    suffix = args.target
    mcp_url = _wait_for_app(client, f"mcp-sandpit-tools-{suffix}")
    agent_url = _wait_for_app(client, f"sandpit-lc-agent-{suffix}")
    omnigent_url = _wait_for_app(client, f"sandpit-omnigent-{suffix}")

    health = requests.get(
        f"{agent_url}/api/health",
        headers=_headers(client),
        timeout=30,
    )
    health.raise_for_status()

    invocation = requests.post(
        f"{agent_url}/api/invocations",
        headers=_headers(client),
        json={"input": "Estimate 8 hours at $125/hour with 10 percent contingency."},
        timeout=180,
    )
    invocation.raise_for_status()
    invocation_payload = invocation.json()
    if not invocation_payload.get("trace_id"):
        raise RuntimeError(f"Agent did not return a trace ID: {invocation_payload}")
    if "1100" not in invocation_payload.get("output", "").replace(",", ""):
        raise RuntimeError(f"Agent returned an unexpected estimate: {invocation_payload}")

    mcp_endpoint = f"{mcp_url}/mcp"
    mcp_headers = _headers(client)
    _mcp_request(
        mcp_endpoint,
        mcp_headers,
        1,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "deployment-smoke-test", "version": "1.0"},
        },
    )
    tools_result = _mcp_request(mcp_endpoint, mcp_headers, 2, "tools/list", {})
    tool_names = {tool["name"] for tool in tools_result["tools"]}
    required_tools = {
        "health",
        "uppercase",
        "estimate_delivery_cost",
        "get_current_identity",
        "invoke_langchain_agent",
    }
    if not required_tools.issubset(tool_names):
        raise RuntimeError(f"Missing MCP tools: {required_tools - tool_names}")
    mcp_result = _mcp_request(
        mcp_endpoint,
        mcp_headers,
        3,
        "tools/call",
        {"name": "uppercase", "arguments": {"text": "bundle deployed"}},
    )
    if "BUNDLE DEPLOYED" not in json.dumps(mcp_result):
        raise RuntimeError(f"Unexpected MCP result: {mcp_result}")
    bridge_result = _mcp_request(
        mcp_endpoint,
        mcp_headers,
        4,
        "tools/call",
        {
            "name": "invoke_langchain_agent",
            "arguments": {
                "message": "Estimate 5 hours at $200/hour with no contingency.",
            },
        },
    )
    if "trace_id" not in json.dumps(bridge_result):
        raise RuntimeError(f"LangChain bridge returned no trace ID: {bridge_result}")

    function_result = _execute(
        client,
        args.warehouse_id,
        f"SELECT {args.uc_function}(8, 125, 10) AS estimate",
    )
    function_rows = function_result.get("result", {}).get("data_array", [])
    if not function_rows or float(function_rows[0][0]) != 1100:
        raise RuntimeError(f"Unexpected UC function result: {function_result}")

    trace_id = invocation_payload["trace_id"].rsplit("/", maxsplit=1)[-1]
    if not trace_id.isalnum():
        raise RuntimeError(f"Unexpected trace ID format: {trace_id}")
    deadline = time.monotonic() + 180
    trace_result: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        trace_result = _execute(
            client,
            args.warehouse_id,
            (
                f"SELECT COUNT(*) AS trace_rows FROM {args.trace_table} "
                f"WHERE trace_id = '{trace_id}'"
            ),
        )
        rows = trace_result.get("result", {}).get("data_array", [])
        if rows and int(rows[0][0]) > 0:
            break
        time.sleep(10)
    else:
        raise RuntimeError(f"No trace rows appeared in {args.trace_table}: {trace_result}")

    omnigent_health = requests.get(
        f"{omnigent_url}/health",
        headers=_headers(client),
        timeout=30,
    )
    omnigent_health.raise_for_status()

    deadline = time.monotonic() + 300
    omnigent_agent: dict[str, Any] | None = None
    online_hosts: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        agents_response = requests.get(
            f"{omnigent_url}/v1/agents",
            headers=_headers(client),
            timeout=30,
        )
        agents_response.raise_for_status()
        omnigent_agent = next(
            (
                agent
                for agent in agents_response.json().get("data", [])
                if agent.get("name") == "sandpit_supervisor"
            ),
            None,
        )
        hosts_response = requests.get(
            f"{omnigent_url}/v1/hosts",
            headers=_headers(client),
            timeout=30,
        )
        hosts_response.raise_for_status()
        online_hosts = [
            host
            for host in hosts_response.json().get("hosts", [])
            if host.get("status") == "online"
        ]
        if omnigent_agent and online_hosts:
            break
        time.sleep(10)
    if omnigent_agent is None or not online_hosts:
        raise RuntimeError("Omnigent supervisor or colocated host did not become ready.")

    policy_names = {policy["name"] for policy in omnigent_agent.get("policies", [])}
    required_policies = {"approve_subagent_spawn", "approve_each_cost_dollar"}
    if not required_policies.issubset(policy_names):
        raise RuntimeError(f"Missing Omnigent policies: {required_policies - policy_names}")
    mcp_servers = {server["name"] for server in omnigent_agent.get("mcp_servers", [])}
    required_mcp_servers = {"custom_mcp", "project_cost"}
    if not required_mcp_servers.issubset(mcp_servers):
        raise RuntimeError(
            f"Missing Omnigent MCP configuration: {required_mcp_servers - mcp_servers}",
        )

    print(
        json.dumps(
            {
                "agent": invocation_payload,
                "function": function_result.get("result", {}).get("data_array"),
                "mcp_tool_count": len(tool_names),
                "mcp_uppercase": mcp_result,
                "mcp_langchain_bridge": bridge_result,
                "omnigent_policies": sorted(policy_names),
                "omnigent_online_hosts": len(online_hosts),
                "omnigent_url": omnigent_url,
                "trace_table": args.trace_table,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()
