"""Platform-owned HTTP, streaming, and tracing surface for folder agents."""

from __future__ import annotations

import importlib
import inspect
import logging
import os
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from functools import lru_cache
from typing import Any
from uuid import uuid4

import anyio
import mlflow
from _platform_tracing import configure_tracing
from fastapi import HTTPException, Request
from mlflow.genai.agent_server import AgentServer
from mlflow.genai.agent_server import invoke as agent_invoke
from mlflow.genai.agent_server import stream as agent_stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    create_text_delta,
    create_text_output_item,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
Invoker = Callable[[str], str | Awaitable[str]]
Streamer = Callable[[str], Any]
INVOCATION_TIMEOUT_SECONDS = 120.0


class InvocationRequest(BaseModel):
    input: str = Field(min_length=1, max_length=20_000)


class InvocationResponse(BaseModel):
    output: str
    trace_id: str


def _entrypoint() -> tuple[str, str]:
    value = os.environ["AGENT_ENTRYPOINT"]
    module_name, separator, function_name = value.partition(":")
    if not separator or not module_name or not function_name:
        raise RuntimeError("AGENT_ENTRYPOINT must use module:function syntax.")
    return module_name, function_name


def _validate_callable(candidate: Any, name: str) -> None:
    if not callable(candidate):
        raise RuntimeError(f"Agent entrypoint {name} is not callable.")
    parameters = list(inspect.signature(candidate).parameters.values())
    if len(parameters) != 1:
        raise RuntimeError(
            "Agent entrypoints must accept exactly one argument named message.",
        )
    parameter = parameters[0]
    if (
        parameter.name != "message"
        or parameter.kind not in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
        or parameter.default is not inspect.Parameter.empty
    ):
        raise RuntimeError(
            "Agent entrypoints must accept exactly one argument named message.",
        )


@lru_cache(maxsize=1)
def _author_module() -> Any:
    module_name, _ = _entrypoint()
    return importlib.import_module(module_name)


@lru_cache(maxsize=1)
def _invoker() -> Invoker:
    module_name, function_name = _entrypoint()
    candidate = getattr(_author_module(), function_name, None)
    _validate_callable(candidate, f"{module_name}:{function_name}")
    return candidate


@lru_cache(maxsize=1)
def _streamer() -> Streamer | None:
    module_name, function_name = _entrypoint()
    stream_name = f"{function_name}_stream"
    candidate = getattr(_author_module(), stream_name, None)
    if candidate is None:
        return None
    _validate_callable(candidate, f"{module_name}:{stream_name}")
    return candidate


def _validate_output(result: Any) -> str:
    if not isinstance(result, str) or not result.strip():
        raise TypeError("Agent entrypoints must return a non-empty string.")
    return result


def _validate_chunk(result: Any) -> str:
    if not isinstance(result, str) or not result:
        raise TypeError("Optional stream entrypoints must yield strings.")
    return result


async def _call_invoker(message: str) -> str:
    invoker = _invoker()
    if inspect.iscoroutinefunction(invoker):
        result: Any = await invoker(message)
    else:
        result = await anyio.to_thread.run_sync(
            invoker,
            message,
            abandon_on_cancel=True,
        )
        if inspect.isawaitable(result):
            result = await result
    return _validate_output(result)


def _next_chunk(iterator: Iterator[Any]) -> tuple[bool, Any]:
    try:
        return True, next(iterator)
    except StopIteration:
        return False, None


async def _author_chunks(message: str) -> AsyncGenerator[str, None]:
    streamer = _streamer()
    if streamer is None:
        yield await _call_invoker(message)
        return

    if inspect.isasyncgenfunction(streamer):
        stream_result: Any = streamer(message)
    elif inspect.iscoroutinefunction(streamer):
        stream_result = await streamer(message)
    else:
        stream_result = await anyio.to_thread.run_sync(
            streamer,
            message,
            abandon_on_cancel=True,
        )

    emitted = False
    if hasattr(stream_result, "__aiter__"):
        async for chunk in stream_result:
            if chunk == "":
                continue
            emitted = True
            yield _validate_chunk(chunk)
    elif hasattr(stream_result, "__iter__") and not isinstance(
        stream_result,
        (str, bytes, dict),
    ):
        iterator = iter(stream_result)
        while True:
            has_chunk, chunk = await anyio.to_thread.run_sync(
                _next_chunk,
                iterator,
                abandon_on_cancel=True,
            )
            if not has_chunk:
                break
            if chunk == "":
                continue
            emitted = True
            yield _validate_chunk(chunk)
    else:
        raise TypeError(
            "Optional stream entrypoints must return an iterator or async iterator.",
        )
    if not emitted:
        raise TypeError("Optional stream entrypoints must yield at least one string.")


async def _invoke_with_trace(message: str) -> tuple[str, str]:
    configure_tracing()
    with mlflow.start_span(
        name=f"generated_agent.{os.environ['AGENT_NAME']}",
        span_type="CHAIN",
    ) as span:
        span.set_inputs({"message": message})
        with anyio.fail_after(INVOCATION_TIMEOUT_SECONDS):
            result = await _call_invoker(message)
        span.set_outputs({"output": result})
        return result, span.trace_id


async def _stream_with_trace(message: str) -> AsyncGenerator[str, None]:
    configure_tracing()
    output: list[str] = []
    with mlflow.start_span(
        name=f"generated_agent.{os.environ['AGENT_NAME']}",
        span_type="CHAIN",
    ) as span:
        span.set_inputs({"message": message})
        with anyio.fail_after(INVOCATION_TIMEOUT_SECONDS):
            async for chunk in _author_chunks(message):
                output.append(chunk)
                yield chunk
        span.set_outputs({"output": "".join(output)})


def _request_message(request: ResponsesAgentRequest) -> str:
    for item in reversed(request.input):
        if getattr(item, "role", None) != "user":
            continue
        content = getattr(item, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = [
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
            ]
            if parts:
                return "\n".join(parts)
    raise ValueError("A non-empty user message is required.")


@agent_invoke()
async def responses_invoke(
    request: ResponsesAgentRequest,
) -> ResponsesAgentResponse:
    output, _ = await _invoke_with_trace(_request_message(request))
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
    async for chunk in _stream_with_trace(_request_message(request)):
        output.append(chunk)
        yield ResponsesAgentStreamEvent(**create_text_delta(chunk, item_id))
    yield ResponsesAgentStreamEvent(
        type="response.output_item.done",
        item=create_text_output_item("".join(output), item_id),
    )


agent_server = AgentServer("ResponsesAgent")
app = agent_server.app
app.title = os.getenv("AGENT_NAME", "Generated agent")
app.description = "Platform-generated Databricks App agent surface."


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
        "name": os.environ["AGENT_NAME"],
        "responses": "/responses",
        "invoke": "/api/invocations",
        "health": "/api/health",
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    configure_tracing()
    _invoker()
    _streamer()
    return {"status": "ok"}


@app.post("/api/invocations", response_model=InvocationResponse)
async def legacy_invoke(request: InvocationRequest) -> InvocationResponse:
    try:
        output, trace_id = await _invoke_with_trace(request.input)
        return InvocationResponse(output=output, trace_id=trace_id)
    except TimeoutError as exc:
        logger.warning("Generated agent invocation timed out.")
        raise HTTPException(status_code=504, detail="Agent invocation timed out.") from exc
    except Exception as exc:
        logger.exception("Generated agent invocation failed.")
        raise HTTPException(status_code=502, detail="Agent invocation failed.") from exc
