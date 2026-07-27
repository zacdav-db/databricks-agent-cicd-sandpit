"""Validate the bundled Omnigent YAML without requiring deployment-time variables."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from omnigent.spec import load

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "src" / "omnigent_app" / "sandpit_supervisor"
sys.path.insert(0, str(AGENT.parent))


def main() -> None:
    defaults = {
        "CUSTOM_MCP_URL": "https://example.databricksapps.com",
        "DATABRICKS_CONFIG_PROFILE": "sandpit",
        "DATABRICKS_HOST": "https://example.cloud.databricks.com",
        "DATABRICKS_WAREHOUSE_ID": "warehouse-id",
        "MODEL_ENDPOINT": "databricks-claude-sonnet-4-5",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    spec = load(AGENT)
    if spec.name != "sandpit_supervisor":
        raise RuntimeError(f"Unexpected agent name: {spec.name}")

    mcp_names = {server.name for server in spec.mcp_servers}
    required_mcp = {"custom_mcp", "project_cost"}
    if not required_mcp.issubset(mcp_names):
        raise RuntimeError(f"Missing MCP servers: {required_mcp - mcp_names}")
    uc_server = next(server for server in spec.mcp_servers if server.name == "project_cost")
    if "/api/2.0/mcp/functions/" not in (uc_server.url or ""):
        raise RuntimeError("The Unity Catalog function MCP endpoint is not configured.")

    subagents = {agent.name: agent for agent in spec.sub_agents}
    databricks_agent = subagents.get("databricks_agent")
    if databricks_agent is None:
        raise RuntimeError("The LangChain bridge subagent is not configured.")
    bridge_servers = {server.name for server in databricks_agent.mcp_servers}
    if "langchain_agent" not in bridge_servers:
        raise RuntimeError("The LangChain bridge tool is not configured.")

    policies = {
        policy.name
        for policy in (spec.guardrails.policies if spec.guardrails else [])
    }
    required_policies = {"approve_subagent_spawn", "approve_each_cost_dollar"}
    if not required_policies.issubset(policies):
        raise RuntimeError(f"Missing Omnigent policies: {required_policies - policies}")

    print(
        "Validated Omnigent agent: "
        f"{spec.name} (UC function MCP, custom MCP, LangChain bridge, policies)",
    )


if __name__ == "__main__":
    main()
