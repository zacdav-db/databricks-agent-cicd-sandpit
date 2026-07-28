from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_platform_tracing():
    name = "platform_tracing_under_test"
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "agent_platform" / "_platform_tracing.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the platform tracing module.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_platform_runtime(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agent_platform"))
    name = "platform_runtime_under_test"
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "agent_platform" / "runtime.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the platform runtime module.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_configures_destination_and_installed_integrations(monkeypatch) -> None:
    tracing = _load_platform_tracing()
    calls: list[tuple[str, object]] = []
    installed = {"langchain", "openai", "anthropic", "google.genai"}

    monkeypatch.setenv("MLFLOW_TRACKING_URI", "databricks")
    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "experiment-123")
    monkeypatch.delenv("MLFLOW_EXPERIMENT_NAME", raising=False)
    monkeypatch.setattr(
        tracing,
        "mlflow",
        SimpleNamespace(
            set_tracking_uri=lambda value: calls.append(("tracking", value)),
            set_experiment=lambda **value: calls.append(("experiment", value)),
        ),
    )
    monkeypatch.setattr(
        tracing.importlib.util,
        "find_spec",
        lambda name: object() if name in installed else None,
    )
    monkeypatch.setattr(
        tracing.importlib,
        "import_module",
        lambda name: SimpleNamespace(
            autolog=lambda: calls.append(("autolog", name)),
        ),
    )

    assert tracing.configure_tracing() == (
        "langchain",
        "openai",
        "anthropic",
        "google.genai",
    )
    assert calls == [
        ("tracking", "databricks"),
        ("experiment", {"experiment_id": "experiment-123"}),
        ("autolog", "mlflow.langchain"),
        ("autolog", "mlflow.openai"),
        ("autolog", "mlflow.anthropic"),
        ("autolog", "mlflow.gemini"),
    ]

    tracing.configure_tracing()
    assert len(calls) == 6


def test_external_deployment_can_select_experiment_by_name(monkeypatch) -> None:
    tracing = _load_platform_tracing()
    calls: list[dict[str, str]] = []

    monkeypatch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)
    monkeypatch.setenv(
        "MLFLOW_EXPERIMENT_NAME",
        "/Shared/external-openai-agent-traces",
    )
    monkeypatch.setattr(
        tracing,
        "mlflow",
        SimpleNamespace(
            set_tracking_uri=lambda _value: None,
            set_experiment=lambda **value: calls.append(value),
        ),
    )
    monkeypatch.setattr(tracing.importlib.util, "find_spec", lambda _name: None)

    assert tracing.configure_tracing() == ()
    assert calls == [
        {"experiment_name": "/Shared/external-openai-agent-traces"},
    ]


def test_experiment_selection_is_fail_closed(monkeypatch) -> None:
    tracing = _load_platform_tracing()
    monkeypatch.setattr(
        tracing,
        "mlflow",
        SimpleNamespace(set_tracking_uri=lambda _value: None),
    )
    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "123")
    monkeypatch.setenv("MLFLOW_EXPERIMENT_NAME", "/Shared/also-set")

    with pytest.raises(RuntimeError, match="only one"):
        tracing.configure_tracing()


def test_installed_integration_failure_is_fail_closed(monkeypatch) -> None:
    tracing = _load_platform_tracing()

    def incompatible_autolog() -> None:
        raise ImportError("incompatible")

    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "123")
    monkeypatch.delenv("MLFLOW_EXPERIMENT_NAME", raising=False)
    monkeypatch.setattr(
        tracing,
        "mlflow",
        SimpleNamespace(
            set_tracking_uri=lambda _value: None,
            set_experiment=lambda **_value: None,
        ),
    )
    monkeypatch.setattr(
        tracing.importlib.util,
        "find_spec",
        lambda name: object() if name == "openai" else None,
    )
    monkeypatch.setattr(
        tracing.importlib,
        "import_module",
        lambda _name: SimpleNamespace(
            autolog=incompatible_autolog,
        ),
    )

    with pytest.raises(RuntimeError, match="installed provider 'openai'"):
        tracing.configure_tracing()


def test_health_configures_tracing_before_author_import(monkeypatch) -> None:
    runtime = _load_platform_runtime(monkeypatch)
    calls: list[str] = []

    monkeypatch.setenv("AGENT_ENTRYPOINT", "author_agent:invoke")
    monkeypatch.setattr(
        runtime,
        "configure_tracing",
        lambda: calls.append("configure"),
    )
    monkeypatch.setattr(
        runtime.importlib,
        "import_module",
        lambda _name: (
            calls.append("author_import")
            or SimpleNamespace(invoke=lambda message: message)
        ),
    )
    runtime._invoker.cache_clear()

    assert runtime.health() == {"status": "ok"}
    assert calls == ["configure", "author_import"]
    assert "/responses" in {route.path for route in runtime.app.routes}

    async def invoke_stream(message: str):
        yield f"{message} "
        yield "streamed"

    monkeypatch.setattr(
        runtime.importlib,
        "import_module",
        lambda _name: SimpleNamespace(
            invoke=lambda message: message,
            invoke_stream=invoke_stream,
        ),
    )
    runtime._author_module.cache_clear()
    runtime._invoker.cache_clear()
    runtime._streamer.cache_clear()

    async def collect() -> list[str]:
        return [chunk async for chunk in runtime._author_chunks("response")]

    assert asyncio.run(collect()) == ["response ", "streamed"]

    spans: list[SimpleNamespace] = []

    class FakeSpan:
        def __init__(self, name: str) -> None:
            self.name = name
            self.outputs: dict[str, str] | None = None

        def __enter__(self):
            spans.append(self)
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def set_inputs(self, _inputs: dict[str, str]) -> None:
            return None

        def set_outputs(self, outputs: dict[str, str]) -> None:
            self.outputs = outputs

    monkeypatch.setenv("AGENT_NAME", "test-agent")
    monkeypatch.setattr(
        runtime.mlflow,
        "start_span",
        lambda *, name, **_kwargs: FakeSpan(name),
    )

    async def collect_traced() -> list[str]:
        return [chunk async for chunk in runtime._stream_with_trace("response")]

    assert asyncio.run(collect_traced()) == ["response ", "streamed"]
    assert [span.name for span in spans] == [
        "generated_agent.test-agent",
        "generated_agent.test-agent.stream",
    ]
    assert [span.outputs for span in spans] == [
        {"output": "response streamed"},
        {"output": "response streamed"},
    ]


def test_external_author_has_no_platform_tracing_dependency() -> None:
    author_source = (
        ROOT / "examples" / "external-agent" / "author_agent.py"
    ).read_text(encoding="utf-8")
    imports = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(ast.parse(author_source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (
            node.names
            if isinstance(node, ast.Import)
            else [ast.alias(name=node.module or "")]
        )
    }

    assert not {"mlflow", "databricks", "_platform_tracing"} & imports
