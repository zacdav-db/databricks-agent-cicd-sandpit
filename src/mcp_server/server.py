"""Custom MCP tools hosted as a Databricks App."""

from __future__ import annotations

import os

from databricks.sdk import WorkspaceClient
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "sandpit-tools",
    host="0.0.0.0",
    port=int(os.getenv("DATABRICKS_APP_PORT", "8000")),
    stateless_http=True,
)


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


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
