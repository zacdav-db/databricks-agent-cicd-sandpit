"""Small LangChain agent using governed tools from Databricks managed MCP."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import mlflow
from databricks.sdk import WorkspaceClient
from databricks_langchain import (
    ChatDatabricks,
    DatabricksMCPServer,
    DatabricksMultiServerMCPClient,
)
from langchain.agents import create_agent


def configure_tracing() -> None:
    """Route LangChain traces to the bundle-provisioned MLflow experiment."""
    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "databricks"))
    experiment_id = os.environ["MLFLOW_EXPERIMENT_ID"]
    mlflow.set_experiment(experiment_id=experiment_id)
    mlflow.langchain.autolog()


@lru_cache(maxsize=1)
def get_model() -> ChatDatabricks:
    """Build the process-wide chat model once."""
    configure_tracing()
    return ChatDatabricks(
        endpoint=os.getenv("MODEL_ENDPOINT", "databricks-claude-sonnet-4-5"),
        temperature=0.1,
        max_tokens=800,
    )


def _function_mcp_url(host: str, function_full_name: str) -> str:
    """Return the managed MCP URL for one three-level UC function name."""
    parts = function_full_name.split(".")
    if len(parts) != 3 or any(not part.replace("_", "").isalnum() for part in parts):
        raise ValueError("Unity Catalog function names must be catalog.schema.function.")
    path = "/".join(parts)
    return f"{host.rstrip('/')}/api/2.0/mcp/functions/{path}"


def _mcp_client(workspace_client: WorkspaceClient) -> DatabricksMultiServerMCPClient:
    function_names = {
        "project-cost": os.environ["UC_COST_FUNCTION_FULL_NAME"],
        "current-time": os.environ["UC_TIME_FUNCTION_FULL_NAME"],
    }
    return DatabricksMultiServerMCPClient(
        [
            DatabricksMCPServer(
                name=name,
                url=_function_mcp_url(workspace_client.config.host, function_name),
                workspace_client=workspace_client,
            )
            for name, function_name in function_names.items()
        ],
    )


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", item)) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


async def invoke_agent(message: str) -> tuple[str, str]:
    """Invoke the agent and return its final text and MLflow trace ID."""
    model = get_model()
    with mlflow.start_span(name="sandpit_langchain_request", span_type="CHAIN") as span:
        span.set_inputs({"message": message})
        workspace_client = WorkspaceClient()
        mcp_client = _mcp_client(workspace_client)
        async with mcp_client:
            tools = await mcp_client.get_tools()
            agent = create_agent(
                model=model,
                tools=tools,
                system_prompt=(
                    "You are a concise delivery-planning assistant. "
                    "Use the governed Unity Catalog tools for current time and cost estimates. "
                    "State assumptions and never invent a tool result."
                ),
            )
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": message}]},
            )
        output = _message_text(result["messages"][-1])
        span.set_outputs({"output": output})
        return output, span.trace_id
