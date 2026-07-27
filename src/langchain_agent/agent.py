"""Small, conventional LangChain agent with MLflow tracing."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import mlflow
from databricks_langchain import ChatDatabricks
from langchain.agents import create_agent
from langchain_core.tools import tool


@tool
def current_utc_time() -> str:
    """Return the current UTC date and time in ISO-8601 format."""
    return datetime.now(UTC).isoformat()


@tool
def estimate_delivery_cost(
    hours: float,
    hourly_rate: float,
    contingency_percent: float = 10.0,
) -> float:
    """Estimate delivery cost from hours, hourly rate, and contingency percentage."""
    if min(hours, hourly_rate, contingency_percent) < 0:
        raise ValueError("Cost inputs must be non-negative.")
    return round(hours * hourly_rate * (1 + contingency_percent / 100), 2)


def configure_tracing() -> None:
    """Route LangChain traces to the bundle-provisioned MLflow experiment."""
    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "databricks"))
    experiment_id = os.environ["MLFLOW_EXPERIMENT_ID"]
    mlflow.set_experiment(experiment_id=experiment_id)
    mlflow.langchain.autolog()


@lru_cache(maxsize=1)
def get_agent() -> Any:
    """Build the process-wide agent once."""
    configure_tracing()
    model = ChatDatabricks(
        endpoint=os.getenv("MODEL_ENDPOINT", "databricks-claude-sonnet-4-5"),
        temperature=0.1,
        max_tokens=800,
    )
    return create_agent(
        model=model,
        tools=[current_utc_time, estimate_delivery_cost],
        system_prompt=(
            "You are a concise delivery-planning assistant. "
            "Use tools when the user asks for the current time or a cost estimate. "
            "State assumptions and never invent a tool result."
        ),
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


def invoke_agent(message: str) -> tuple[str, str]:
    """Invoke the agent and return its final text and MLflow trace ID."""
    with mlflow.start_span(name="sandpit_langchain_request", span_type="CHAIN") as span:
        span.set_inputs({"message": message})
        result = get_agent().invoke(
            {"messages": [{"role": "user", "content": message}]},
        )
        output = _message_text(result["messages"][-1])
        span.set_outputs({"output": output})
        return output, span.trace_id
