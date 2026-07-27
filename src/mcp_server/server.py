"""Custom MCP tools hosted as a Databricks App."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "sandpit-tools",
    host="0.0.0.0",
    port=int(os.getenv("DATABRICKS_APP_PORT", "8000")),
    stateless_http=True,
)


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
def get_current_identity() -> str:
    """Return the Databricks identity used by this app."""
    identity = WorkspaceClient().current_user.me()
    return identity.user_name or identity.display_name or "unknown"


@mcp.tool()
def invoke_langchain_agent(message: str) -> dict[str, Any]:
    """Call the bundle-deployed LangChain agent and return its answer and trace ID."""
    client = WorkspaceClient(config=Config(http_timeout_seconds=180))
    payload = client.api_client.do(
        "POST",
        url=f"{_langchain_agent_url()}/api/invocations",
        body={"input": message},
    )
    if not isinstance(payload, dict):
        raise RuntimeError("LangChain Agent returned a non-object response.")
    return payload


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
