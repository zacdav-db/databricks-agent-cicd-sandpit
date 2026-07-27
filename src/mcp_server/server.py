"""Custom MCP tools hosted as a Databricks App."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import requests
from databricks.sdk import WorkspaceClient
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "sandpit-tools",
    host="0.0.0.0",
    port=int(os.getenv("DATABRICKS_APP_PORT", "8000")),
    stateless_http=True,
)


def _delivery_cost(hours: float, hourly_rate: float, contingency_percent: float) -> float:
    if min(hours, hourly_rate, contingency_percent) < 0:
        raise ValueError("Cost inputs must be non-negative.")
    return round(hours * hourly_rate * (1 + contingency_percent / 100), 2)


@lru_cache(maxsize=1)
def _langchain_agent_url() -> str:
    app_name = os.environ["LANGCHAIN_AGENT_APP_NAME"]
    app = WorkspaceClient().apps.get(name=app_name)
    if not app.url:
        raise RuntimeError(f"Databricks App {app_name} does not have a URL.")
    return app.url.rstrip("/")


@mcp.tool()
def health() -> dict[str, str]:
    """Confirm that the custom MCP server is operational."""
    return {"status": "ok", "server": "sandpit-tools"}


@mcp.tool()
def uppercase(text: str) -> str:
    """Convert text to uppercase."""
    return text.upper()


@mcp.tool()
def estimate_delivery_cost(
    hours: float,
    hourly_rate: float,
    contingency_percent: float = 10.0,
) -> float:
    """Estimate delivery cost from hours, hourly rate, and contingency percentage."""
    return _delivery_cost(hours, hourly_rate, contingency_percent)


@mcp.tool()
def get_current_identity() -> str:
    """Return the Databricks identity used by this app."""
    identity = WorkspaceClient().current_user.me()
    return identity.user_name or identity.display_name or "unknown"


@mcp.tool()
def invoke_langchain_agent(message: str) -> dict[str, Any]:
    """Call the bundle-deployed LangChain agent and return its answer and trace ID."""
    client = WorkspaceClient()
    headers = client.config.authenticate()
    headers["Content-Type"] = "application/json"
    response = requests.post(
        f"{_langchain_agent_url()}/api/invocations",
        headers=headers,
        json={"input": message},
        timeout=180,
    )
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
