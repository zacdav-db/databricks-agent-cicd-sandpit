"""Focused acceptance tests for the three explicit runtime Apps."""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from register_uc_agent import (
    DEFAULT_METADATA_PRINCIPAL,
    gateway_agent,
    verify_gateway_registration,
)
from smoke_test import (
    _api_json,
    _find_nonempty_string,
    _mcp_json_payloads,
    _mcp_request,
    _wait_for_app,
    _wait_for_trace,
)


def _smoke_langchain(
    client: WorkspaceClient,
    target: str,
    warehouse_id: str,
    metadata_principal: str,
) -> dict[str, Any]:
    url = _wait_for_app(client, f"{target}-sandpit-langchain-agent")
    catalog = os.getenv("UC_CATALOG", "zacdav_sandpit_catalog")
    schema = os.getenv("UC_SCHEMA", f"{target}_agent_cicd")
    gateway = verify_gateway_registration(
        client,
        catalog=catalog,
        schema=schema,
        registration=gateway_agent(target, runtime_agent="langchain"),
        principal=metadata_principal,
    )
    _api_json(client, "GET", f"{url}/api/health")
    result = _api_json(
        client,
        "POST",
        f"{url}/api/invocations",
        body={"input": "Estimate 8 hours at $125/hour with 10 percent contingency."},
    )
    if not result.get("trace_id") or "1100" not in result.get("output", "").replace(
        ",",
        "",
    ):
        raise RuntimeError(f"LangChain App returned an invalid result: {result}")
    trace_prefix = os.getenv(
        "UC_TRACE_TABLE_PREFIX",
        f"{target}_sandpit_agent_cicd",
    )
    trace_table = f"{catalog}.{schema}.{trace_prefix}_otel_spans"
    _wait_for_trace(
        client,
        warehouse_id,
        trace_table,
        result["trace_id"],
    )
    return {
        "gateway_agent_service": gateway["agent_service"],
        "gateway_registration_verified": True,
        "result": result,
        "trace_table": trace_table,
    }


def _smoke_mcp(client: WorkspaceClient, target: str) -> dict[str, Any]:
    url = _wait_for_app(client, f"mcp-{target}-sandpit-tools")
    endpoint = f"{url}/mcp"
    _mcp_request(
        client,
        endpoint,
        1,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "focused-smoke-test", "version": "1.0"},
        },
    )
    listed = _mcp_request(client, endpoint, 2, "tools/list", {})
    tool_names = {tool["name"] for tool in listed["tools"]}
    required = {"health", "uppercase", "invoke_langchain_agent"}
    if not required.issubset(tool_names):
        raise RuntimeError(f"Missing MCP tools: {required - tool_names}")
    uppercase = _mcp_request(
        client,
        endpoint,
        3,
        "tools/call",
        {"name": "uppercase", "arguments": {"text": "isolated deployment"}},
    )
    if "ISOLATED DEPLOYMENT" not in json.dumps(uppercase):
        raise RuntimeError(f"Unexpected MCP response: {uppercase}")
    bridge = _mcp_request(
        client,
        endpoint,
        4,
        "tools/call",
        {
            "name": "invoke_langchain_agent",
            "arguments": {"message": "Reply briefly with the word healthy."},
        },
    )
    trace_id = next(
        (
            value
            for payload in _mcp_json_payloads(bridge)
            if (value := _find_nonempty_string(payload, "trace_id"))
        ),
        None,
    )
    if not trace_id:
        raise RuntimeError(f"MCP bridge returned no trace ID: {bridge}")
    return {"tool_count": len(tool_names), "bridge_trace_id": trace_id}


def _smoke_omnigent(
    client: WorkspaceClient,
    target: str,
    metadata_principal: str,
) -> dict[str, Any]:
    url = _wait_for_app(client, f"{target}-sandpit-omnigent")
    catalog = os.getenv("UC_CATALOG", "zacdav_sandpit_catalog")
    schema = os.getenv("UC_SCHEMA", f"{target}_agent_cicd")
    gateway = verify_gateway_registration(
        client,
        catalog=catalog,
        schema=schema,
        registration=gateway_agent(target, runtime_agent="omnigent"),
        principal=metadata_principal,
    )
    _api_json(client, "GET", f"{url}/health")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = _api_json(client, "GET", f"{url}/v1/agents")
        agent = next(
            (
                item
                for item in response.get("data", [])
                if item.get("name") == "sandpit_supervisor"
            ),
            None,
        )
        if agent:
            servers = {server["name"] for server in agent.get("mcp_servers", [])}
            required = {"custom_mcp", "project_cost"}
            if not required.issubset(servers):
                raise RuntimeError(
                    f"Missing Omnigent MCP servers: {required - servers}",
                )
            return {
                "agent": agent["name"],
                "gateway_agent_service": gateway["agent_service"],
                "gateway_registration_verified": True,
                "mcp_servers": sorted(servers),
            }
        time.sleep(10)
    raise TimeoutError("Omnigent supervisor did not become ready.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--app",
        required=True,
        choices=("langchain", "mcp", "omnigent"),
    )
    parser.add_argument("--target", required=True, choices=("dev", "prod"))
    parser.add_argument("--profile")
    parser.add_argument(
        "--warehouse-id",
        default=os.getenv("DATABRICKS_WAREHOUSE_ID", "f7a871ffa2a9ab80"),
    )
    parser.add_argument(
        "--metadata-principal",
        default=DEFAULT_METADATA_PRINCIPAL,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = WorkspaceClient(
        config=Config(profile=args.profile, http_timeout_seconds=180),
    )
    if args.app == "langchain":
        result = _smoke_langchain(
            client,
            args.target,
            args.warehouse_id,
            args.metadata_principal,
        )
    elif args.app == "mcp":
        result = _smoke_mcp(client, args.target)
    else:
        result = _smoke_omnigent(
            client,
            args.target,
            args.metadata_principal,
        )
    print(json.dumps({"app": args.app, **result}, sort_keys=True))


if __name__ == "__main__":
    main()
