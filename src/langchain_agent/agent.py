"""Small LangChain agent using custom and managed MCP tools."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
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
from langchain_core.messages import AIMessageChunk
from langchain_core.tools import StructuredTool

SYSTEM_PROMPT = (
    "You are a concise delivery-planning assistant. "
    "Use the custom MCP tools for text transformations and diagnostics. "
    "Always call the uppercase tool when the user asks to uppercase text. "
    "Always call get_current_identity when the user asks for the custom "
    "MCP identity. "
    "Use the governed Unity Catalog tools for current time and cost estimates. "
    "State assumptions and never invent a tool result."
)


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


def _custom_mcp_url(workspace_client: WorkspaceClient) -> str:
    """Resolve the custom MCP App resource to its Streamable HTTP endpoint."""
    app_name = os.environ["CUSTOM_MCP_APP_NAME"]
    app = workspace_client.apps.get(name=app_name)
    if not app.url:
        raise RuntimeError(f"Databricks App {app_name} does not have a URL.")
    return f"{app.url.rstrip('/')}/mcp"


def _mcp_client(workspace_client: WorkspaceClient) -> DatabricksMultiServerMCPClient:
    function_names = {
        "project-cost": os.environ["UC_COST_FUNCTION_FULL_NAME"],
        "current-time": os.environ["UC_TIME_FUNCTION_FULL_NAME"],
    }
    servers = [
        DatabricksMCPServer(
            name="custom-tools",
            url=_custom_mcp_url(workspace_client),
            workspace_client=workspace_client,
        ),
    ]
    servers.extend(
        DatabricksMCPServer(
            name=name,
            url=_function_mcp_url(workspace_client.config.host, function_name),
            workspace_client=workspace_client,
        )
        for name, function_name in function_names.items()
    )
    return DatabricksMultiServerMCPClient(
        servers,
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


def _plain_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return {key: _plain_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain_value(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _tool_result_text(value: Any) -> str:
    """Convert rich MCP content blocks to model-portable tool result text."""
    if isinstance(value, str):
        return value
    return json.dumps(_plain_value(value), separators=(",", ":"), sort_keys=True)


def _plain_text_tool(mcp_tool: Any) -> StructuredTool:
    async def invoke_mcp_tool(**arguments: Any) -> str:
        result = await mcp_tool.ainvoke(arguments)
        return _tool_result_text(result)

    return StructuredTool.from_function(
        coroutine=invoke_mcp_tool,
        name=mcp_tool.name,
        description=mcp_tool.description,
        args_schema=mcp_tool.args_schema,
        infer_schema=False,
    )


async def _create_tool_agent() -> Any:
    workspace_client = WorkspaceClient()
    mcp_client = _mcp_client(workspace_client)
    tools = [_plain_text_tool(tool) for tool in await mcp_client.get_tools()]
    return create_agent(
        model=get_model(),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
    )


def _chunk_text(message: Any) -> str:
    if not isinstance(message, AIMessageChunk):
        return ""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


async def invoke_agent(message: str) -> tuple[str, str]:
    """Invoke the agent and return its final text and MLflow trace ID."""
    configure_tracing()
    with mlflow.start_span(name="sandpit_langchain_request", span_type="CHAIN") as span:
        span.set_inputs({"message": message})
        agent = await _create_tool_agent()
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": message}]},
        )
        output = _message_text(result["messages"][-1])
        span.set_outputs({"output": output})
        return output, span.trace_id


async def stream_agent(message: str) -> AsyncGenerator[str, None]:
    """Stream model text while preserving one trace across tools and inference."""
    configure_tracing()
    output: list[str] = []
    with mlflow.start_span(name="sandpit_langchain_request", span_type="CHAIN") as span:
        span.set_inputs({"message": message})
        agent = await _create_tool_agent()
        async for message_chunk, _metadata in agent.astream(
            {"messages": [{"role": "user", "content": message}]},
            stream_mode="messages",
        ):
            text = _chunk_text(message_chunk)
            if not text:
                continue
            output.append(text)
            yield text
        if not output:
            raise RuntimeError("LangChain returned no streamed text.")
        span.set_outputs({"output": "".join(output)})
