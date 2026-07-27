"""Small Omnigent policies specific to the example supervisor."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

PolicyResponse = dict[str, Any]
_APPROVED_CHECKPOINT_KEY = "approved_cost_checkpoint_usd"
_GATED_PHASES = {"request", "tool_call"}


def every_dollar_cost_gate(interval_usd: float = 1.0) -> Callable[[dict[str, Any]], PolicyResponse]:
    """Ask once whenever cumulative session cost reaches a new interval."""
    if interval_usd <= 0:
        raise ValueError("interval_usd must be positive.")

    def evaluate(event: dict[str, Any]) -> PolicyResponse:
        if event.get("type") not in _GATED_PHASES:
            return {"result": "ALLOW"}

        context = event.get("context") or {}
        usage = context.get("usage") or {}
        cost = float(usage.get("total_cost_usd") or 0.0)
        checkpoint = int(cost / interval_usd) * interval_usd

        session_state = event.get("session_state") or {}
        approved = float(session_state.get(_APPROVED_CHECKPOINT_KEY) or 0.0)
        if checkpoint <= approved or checkpoint < interval_usd:
            return {"result": "ALLOW"}

        return {
            "result": "ASK",
            "reason": (
                f"Session cost ${cost:.2f} crossed the "
                f"${checkpoint:.2f} checkpoint. Continue?"
            ),
            "state_updates": [
                {
                    "key": _APPROVED_CHECKPOINT_KEY,
                    "action": "set",
                    "value": checkpoint,
                },
            ],
        }

    return evaluate
