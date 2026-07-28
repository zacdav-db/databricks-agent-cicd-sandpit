"""Tool for delegating a request to the deployed LangChain App."""

from __future__ import annotations

import os
from functools import lru_cache

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from omnigent_client import tool


@lru_cache(maxsize=1)
def _workspace_client() -> WorkspaceClient:
    return WorkspaceClient(
        config=Config(
            profile=os.environ["DATABRICKS_CONFIG_PROFILE"],
            http_timeout_seconds=180,
        ),
    )


@tool
def invoke_langchain_agent(message: str) -> dict[str, str]:
    """Invoke LangChain and return its answer and MLflow trace ID.

    Args:
        message: The complete question for the deployed LangChain agent.
    """
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
