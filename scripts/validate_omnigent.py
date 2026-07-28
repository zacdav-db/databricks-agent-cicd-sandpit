"""Validate the bundled Omnigent YAML without requiring deployment-time variables."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from omnigent.spec import load
from omnigent.tools.local import load_local_python_tools

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "src" / "omnigent_app" / "sandpit_supervisor"
sys.path.insert(0, str(AGENT.parent))


def main() -> None:
    defaults = {
        "DATABRICKS_CONFIG_PROFILE": "sandpit",
        "LANGCHAIN_AGENT_URL": "https://agent.example.databricksapps.com",
        "MODEL_ENDPOINT": "databricks-claude-sonnet-4-5",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    spec = load(AGENT)
    if spec.name != "sandpit_supervisor":
        raise RuntimeError(f"Unexpected agent name: {spec.name}")

    mcp_names = {server.name for server in spec.mcp_servers}
    if mcp_names:
        raise RuntimeError(
            f"Omnigent must delegate to LangChain, not MCP directly: {mcp_names}",
        )

    subagents = {agent.name: agent for agent in spec.sub_agents}
    databricks_agent = subagents.get("databricks_agent")
    if databricks_agent is None:
        raise RuntimeError("The LangChain delegate subagent is not configured.")
    delegate_mcp_servers = {
        server.name for server in databricks_agent.mcp_servers
    }
    if delegate_mcp_servers:
        raise RuntimeError(
            "The LangChain delegate must not use an MCP bridge: "
            f"{delegate_mcp_servers}",
        )
    delegate_root = AGENT / "agents" / "databricks_agent"
    tool_names = {
        tool.name()
        for tool in load_local_python_tools(
            databricks_agent.local_tools,
            delegate_root,
        )
    }
    if tool_names != {"invoke_langchain_agent"}:
        raise RuntimeError(
            f"The LangChain delegate has unexpected local tools: {tool_names}",
        )

    policies = {
        policy.name
        for policy in (spec.guardrails.policies if spec.guardrails else [])
    }
    required_policies = {"approve_subagent_spawn", "approve_each_cost_dollar"}
    if not required_policies.issubset(policies):
        raise RuntimeError(f"Missing Omnigent policies: {required_policies - policies}")

    print(
        "Validated Omnigent agent: "
        f"{spec.name} (direct LangChain delegation and approval policies)",
    )


if __name__ == "__main__":
    main()
