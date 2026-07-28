"""Tests for path-based, per-App deployment selection."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_selector():
    path = ROOT / "scripts" / "select_deployments.py"
    spec = importlib.util.spec_from_file_location("select_deployments", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


select_deployments = _load_selector().select_deployments
AGENTS = [
    "claude-assistant",
    "gemini-assistant",
    "langchain-assistant",
    "openai-assistant",
]


@pytest.mark.parametrize("agent", AGENTS)
def test_agent_change_selects_only_that_agent(agent: str) -> None:
    assert select_deployments(
        [f"agents/{agent}/agent.py"],
        AGENTS,
    ) == {
        "apps": [],
        "agents": [agent],
    }


def test_platform_change_fans_out_only_to_folder_agents() -> None:
    assert select_deployments(
        ["agent_platform/runtime.py"],
        AGENTS,
    ) == {
        "apps": [],
        "agents": AGENTS,
    }


def test_runtime_app_change_selects_only_that_app() -> None:
    assert select_deployments(
        ["src/mcp_server/server.py"],
        AGENTS,
    ) == {
        "apps": ["mcp"],
        "agents": [],
    }


@pytest.mark.parametrize(
    "path",
    [
        "scripts/bootstrap_resources.py",
        "scripts/migrate_app_bundle.sh",
        "scripts/select_deployments.py",
        "scripts/smoke_test.py",
    ],
)
def test_deployment_platform_change_selects_every_unit(path: str) -> None:
    assert select_deployments(
        [path],
        AGENTS,
    ) == {
        "apps": ["mcp", "langchain", "omnigent"],
        "agents": AGENTS,
    }


def test_runtime_dependencies_deploy_in_call_order() -> None:
    assert select_deployments(
        [
            "src/omnigent_app/config.yaml",
            "src/langchain_agent/agent.py",
            "src/mcp_server/server.py",
        ],
        AGENTS,
    )["apps"] == ["mcp", "langchain", "omnigent"]


def test_docs_and_tests_do_not_trigger_deployment() -> None:
    assert select_deployments(
        ["README.md", "tests/test_deployment_selection.py"],
        AGENTS,
    ) == {
        "apps": [],
        "agents": [],
    }
