"""FastAPI surface for the LangChain agent."""

from __future__ import annotations

from agent import invoke_agent
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(
    title="Sandpit LangChain Agent",
    version="1.0.0",
    description="A bundle-deployed LangChain agent with MLflow tracing.",
)


class InvocationRequest(BaseModel):
    input: str = Field(min_length=1, max_length=20_000)


class InvocationResponse(BaseModel):
    output: str
    trace_id: str


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "sandpit-langchain-agent",
        "invoke": "/api/invocations",
        "health": "/api/health",
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/invocations", response_model=InvocationResponse)
def invoke(request: InvocationRequest) -> InvocationResponse:
    try:
        output, trace_id = invoke_agent(request.input)
        return InvocationResponse(output=output, trace_id=trace_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Agent invocation failed: {exc}") from exc
