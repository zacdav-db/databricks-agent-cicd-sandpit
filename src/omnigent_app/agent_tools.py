"""Deployment-owned tool for delegating to the LangChain App."""

from __future__ import annotations

import os
from functools import lru_cache

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from databricks_openai import DatabricksOpenAI


@lru_cache(maxsize=1)
def _workspace_client() -> WorkspaceClient:
    return WorkspaceClient(
        config=Config(
            profile=os.environ["DATABRICKS_CONFIG_PROFILE"],
            http_timeout_seconds=180,
        ),
    )


@lru_cache(maxsize=1)
def _responses_client() -> DatabricksOpenAI:
    return DatabricksOpenAI(workspace_client=_workspace_client())


def invoke_langchain_agent(message: str) -> dict[str, str]:
    """Invoke LangChain and return its answer and MLflow trace ID.

    Args:
        message: The complete question for the deployed LangChain agent.
    """
    response = _responses_client().responses.create(
        model=f"apps/{os.environ['LANGCHAIN_AGENT_APP_NAME']}",
        input=message,
        extra_headers={"x-mlflow-return-trace-id": "true"},
    )
    output = response.output_text
    metadata = response.metadata or {}
    trace_id = metadata.get("trace_id")
    if not isinstance(output, str) or not isinstance(trace_id, str):
        raise RuntimeError(
            f"LangChain App returned an invalid Responses API result: {response}",
        )
    return {"output": output, "trace_id": trace_id}
