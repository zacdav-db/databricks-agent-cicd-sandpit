from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_langchain_cost_tool() -> None:
    module = _load("langchain_agent", ROOT / "src" / "langchain_agent" / "agent.py")
    assert module.estimate_delivery_cost.invoke(
        {"hours": 8, "hourly_rate": 125, "contingency_percent": 10},
    ) == 1100


def test_mcp_cost_helper() -> None:
    module = _load("mcp_server", ROOT / "src" / "mcp_server" / "server.py")
    assert module._delivery_cost(8, 125, 10) == 1100
    with pytest.raises(ValueError, match="non-negative"):
        module._delivery_cost(-1, 125, 10)


def test_omnigent_cost_policy_asks_at_each_new_dollar() -> None:
    module = _load(
        "agent_policies",
        ROOT / "src" / "omnigent_app" / "sandpit_supervisor" / "agent_policies.py",
    )
    policy = module.every_dollar_cost_gate()
    event = {
        "type": "request",
        "context": {"usage": {"total_cost_usd": 2.25}},
        "session_state": {"approved_cost_checkpoint_usd": 1.0},
    }
    response = policy(event)
    assert response["result"] == "ASK"
    assert response["state_updates"][0]["value"] == 2.0

    event["session_state"]["approved_cost_checkpoint_usd"] = 2.0
    assert policy(event) == {"result": "ALLOW"}

    with pytest.raises(ValueError, match="positive"):
        module.every_dollar_cost_gate(0)
