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

from agent_sdk import AgentContext

logger = logging.getLogger(__name__)
Invoker = Callable[[str, AgentContext], str | Awaitable[str]]
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
    if len(parameters) != 2:
        raise RuntimeError("Agent entrypoints must accept message and context.")
    return candidate


@lru_cache(maxsize=1)
def _context() -> AgentContext:
    return AgentContext(
        name=os.environ["AGENT_NAME"],
        model_endpoint=os.environ["MODEL_ENDPOINT"],
        deployment_env=os.environ["DEPLOYMENT_ENV"],
    )


@lru_cache(maxsize=1)
def _configure_tracing() -> None:
    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "databricks"))
    mlflow.set_experiment(experiment_id=os.environ["MLFLOW_EXPERIMENT_ID"])
    try:
        mlflow.langchain.autolog()
    except (AttributeError, ImportError):
        logger.info("LangChain autologging is not installed for this agent.")


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
    _invoker()
    return {"status": "ok"}


@app.post("/api/invocations", response_model=InvocationResponse)
async def invoke(request: InvocationRequest) -> InvocationResponse:
    try:
        _configure_tracing()
        with mlflow.start_span(
            name=f"generated_agent.{_context().name}",
            span_type="CHAIN",
        ) as span:
            span.set_inputs({"message": request.input})
            invoker = _invoker()
            with anyio.fail_after(INVOCATION_TIMEOUT_SECONDS):
                if inspect.iscoroutinefunction(invoker):
                    result: Any = await invoker(request.input, _context())
                else:
                    result = await anyio.to_thread.run_sync(
                        invoker,
                        request.input,
                        _context(),
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
