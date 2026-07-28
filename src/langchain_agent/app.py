"""Standards-compatible HTTP surface for the LangChain agent."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from typing import Any
from uuid import uuid4

from agent import configure_tracing, invoke_agent, stream_agent
from fastapi import HTTPException, Request
from mlflow.genai.agent_server import (
    AgentServer,
    invoke as agent_invoke,
    stream as agent_stream,
)
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    create_text_delta,
    create_text_output_item,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class InvocationRequest(BaseModel):
    input: str = Field(min_length=1, max_length=20_000)


class InvocationResponse(BaseModel):
    output: str
    trace_id: str


def _request_message(request: ResponsesAgentRequest) -> str:
    for item in reversed(request.input):
        if getattr(item, "role", None) != "user":
            continue
        content = getattr(item, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            text = "\n".join(
                value
                for part in content
                if isinstance(
                    value := (
                        part.get("text")
                        if isinstance(part, dict)
                        else getattr(part, "text", None)
                    ),
                    str,
                )
                and value
            )
            if text:
                return text
    raise ValueError("A non-empty user message is required.")


@agent_invoke()
async def responses_invoke(
    request: ResponsesAgentRequest,
) -> ResponsesAgentResponse:
    output, _ = await invoke_agent(_request_message(request))
    item_id = f"msg_{uuid4().hex}"
    return ResponsesAgentResponse(
        output=[create_text_output_item(output, item_id)],
    )


@agent_stream()
async def responses_stream(
    request: ResponsesAgentRequest,
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    item_id = f"msg_{uuid4().hex}"
    output: list[str] = []
    async for chunk in stream_agent(_request_message(request)):
        output.append(chunk)
        yield ResponsesAgentStreamEvent(**create_text_delta(chunk, item_id))
    yield ResponsesAgentStreamEvent(
        type="response.output_item.done",
        item=create_text_output_item("".join(output), item_id),
    )


agent_server = AgentServer("ResponsesAgent")
app = agent_server.app
app.title = "Sandpit LangChain Agent"
app.description = "Bundle-deployed LangChain agent with SSE and MLflow tracing."


@app.middleware("http")
async def configure_request_tracing(
    request: Request,
    call_next: Callable[..., Any],
) -> Any:
    configure_tracing()
    return await call_next(request)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "sandpit-langchain-agent",
        "responses": "/responses",
        "invoke": "/api/invocations",
        "health": "/api/health",
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    configure_tracing()
    return {"status": "ok"}


@app.post("/api/invocations", response_model=InvocationResponse)
async def legacy_invoke(request: InvocationRequest) -> InvocationResponse:
    try:
        output, trace_id = await invoke_agent(request.input)
        return InvocationResponse(output=output, trace_id=trace_id)
    except Exception as exc:
        logger.exception("Agent invocation failed.")
        raise HTTPException(
            status_code=502,
            detail="Agent invocation failed.",
        ) from exc
