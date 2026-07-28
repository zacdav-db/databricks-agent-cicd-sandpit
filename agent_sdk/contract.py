"""Values supplied by the generated agent runtime."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Platform-owned context passed to every agent invocation."""

    name: str
    model_endpoint: str
    deployment_env: str
