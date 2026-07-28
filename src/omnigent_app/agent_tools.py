"""Deployment-owned tools used by the Omnigent supervisor."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TypedDict

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config


class AgentResponse(TypedDict):
    output: str
    trace_id: str


@lru_cache(maxsize=1)
def _workspace_client() -> WorkspaceClient:
    return WorkspaceClient(
        config=Config(
            profile=os.environ["DATABRICKS_CONFIG_PROFILE"],
            http_timeout_seconds=180,
        ),
    )


def invoke_langchain_agent(message: str) -> AgentResponse:
    """Invoke the LangChain App directly and return its answer and trace ID."""
    payload = _workspace_client().api_client.do(
        "POST",
        url=f"{os.environ['LANGCHAIN_AGENT_URL'].rstrip('/')}/api/invocations",
        body={"input": message},
    )
    if not isinstance(payload, dict):
        raise RuntimeError("LangChain App returned a non-object response.")
    output = payload.get("output")
    trace_id = payload.get("trace_id")
    if not isinstance(output, str) or not isinstance(trace_id, str):
        raise RuntimeError(f"LangChain App returned an invalid response: {payload}")
    return {"output": output, "trace_id": trace_id}
