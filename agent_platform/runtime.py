"""Platform-owned FastAPI and tracing surface for folder-defined agents."""

from __future__ import annotations

import importlib
import inspect
import logging
import os
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

import anyio
import mlflow
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from _platform_tracing import configure_tracing

logger = logging.getLogger(__name__)
Invoker = Callable[[str], str | Awaitable[str]]
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


@lru_cache(maxsize=1)
def _invoker() -> Invoker:
    module_name, function_name = _entrypoint()
    candidate = getattr(importlib.import_module(module_name), function_name, None)
    if not callable(candidate):
        raise RuntimeError(f"Agent entrypoint {module_name}:{function_name} is not callable.")
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
    return candidate


app = FastAPI(
    title=os.getenv("AGENT_NAME", "Generated agent"),
    version="1.0.0",
    description="Platform-generated Databricks App agent surface.",
)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": os.environ["AGENT_NAME"],
        "invoke": "/api/invocations",
        "health": "/api/health",
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    configure_tracing()
    _invoker()
    return {"status": "ok"}


@app.post("/api/invocations", response_model=InvocationResponse)
async def invoke(request: InvocationRequest) -> InvocationResponse:
    try:
        configure_tracing()
        with mlflow.start_span(
            name=f"generated_agent.{os.environ['AGENT_NAME']}",
            span_type="CHAIN",
        ) as span:
            span.set_inputs({"message": request.input})
            invoker = _invoker()
            with anyio.fail_after(INVOCATION_TIMEOUT_SECONDS):
                if inspect.iscoroutinefunction(invoker):
                    result: Any = await invoker(request.input)
                else:
                    result = await anyio.to_thread.run_sync(
                        invoker,
                        request.input,
                        abandon_on_cancel=True,
                    )
                    if inspect.isawaitable(result):
                        result = await result
            if not isinstance(result, str) or not result.strip():
                raise TypeError("Agent entrypoints must return a non-empty string.")
            span.set_outputs({"output": result})
            return InvocationResponse(output=result, trace_id=span.trace_id)
    except TimeoutError as exc:
        logger.warning("Generated agent invocation timed out.")
        raise HTTPException(status_code=504, detail="Agent invocation timed out.") from exc
    except Exception as exc:
        logger.exception("Generated agent invocation failed.")
        raise HTTPException(status_code=502, detail="Agent invocation failed.") from exc
