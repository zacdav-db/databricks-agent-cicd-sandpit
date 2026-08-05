from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cloudpickle
import pytest
from mlflow.types.responses import ResponsesAgentRequest

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scripts_path = str(ROOT / "scripts")
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)
register_uc_model = _load_module(
    "register_uc_model",
    ROOT / "scripts" / "register_uc_model.py",
)
app_proxy_model = _load_module(
    "app_proxy_model",
    ROOT / "agent_platform" / "app_proxy_model.py",
)
DatabricksAppResponsesAgent = app_proxy_model.DatabricksAppResponsesAgent


def test_deployment_environment_can_access_uc_model_artifacts() -> None:
    requirements = (ROOT / "requirements-deploy.txt").read_text().splitlines()

    assert any(requirement.startswith("boto3") for requirement in requirements)


class _FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if not kwargs["stream"]:
            payload = {
                "object": "response",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_1",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello"}],
                    },
                ],
            }
            return SimpleNamespace(model_dump=lambda **_kwargs: payload)
        events = [
            {
                "type": "response.output_text.delta",
                "item_id": "msg_1",
                "delta": "hel",
            },
            {
                "type": "response.output_text.delta",
                "item_id": "msg_1",
                "delta": "lo",
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello"}],
                },
            },
        ]
        return [
            SimpleNamespace(model_dump=lambda event=event, **_kwargs: event)
            for event in events
        ]


def _proxy() -> tuple[Any, _FakeResponses]:
    responses = _FakeResponses()
    proxy = DatabricksAppResponsesAgent("agent-dev-example")
    proxy._openai_client = SimpleNamespace(responses=responses)
    return proxy, responses


def test_app_proxy_preserves_responses_contract() -> None:
    proxy, responses = _proxy()
    request = ResponsesAgentRequest(
        input=[{"role": "user", "content": "hello"}],
    )

    response = proxy.predict(request)

    assert response.output[0].content[0]["text"] == "hello"
    assert responses.calls[0]["model"] == "apps/agent-dev-example"
    assert responses.calls[0]["stream"] is False


def test_app_proxy_preserves_stream_events() -> None:
    proxy, responses = _proxy()
    request = ResponsesAgentRequest(
        input=[{"role": "user", "content": "hello"}],
    )

    events = list(proxy.predict_stream(request))

    assert [event.type for event in events] == [
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_item.done",
    ]
    assert events[0].delta == "hel"
    assert responses.calls[0]["model"] == "apps/agent-dev-example"
    assert responses.calls[0]["stream"] is True


def test_app_proxy_excludes_live_sdk_client_from_model_artifact() -> None:
    proxy, _ = _proxy()
    request = ResponsesAgentRequest(
        input=[{"role": "user", "content": "hello"}],
    )
    proxy.predict(request)

    restored = cloudpickle.loads(cloudpickle.dumps(proxy))

    assert restored.app_name == "agent-dev-example"
    assert restored._workspace_client is None
    assert restored._openai_client is None


def test_app_proxy_rejects_non_agent_app_names() -> None:
    with pytest.raises(ValueError, match="start with agent-"):
        DatabricksAppResponsesAgent("plain-app")


def test_registration_names_are_target_specific() -> None:
    generated = register_uc_model.model_registration(
        "dev",
        agent="openai-assistant",
        git_sha="abc123",
    )
    langchain = register_uc_model.model_registration(
        "prod",
        git_sha="def456",
    )

    assert generated.app_name == "agent-dev-openai-assistant"
    assert generated.model_name == "dev_agent_openai_assistant_model"
    assert generated.full_model_name("catalog", "schema") == (
        "catalog.schema.dev_agent_openai_assistant_model"
    )
    assert langchain.app_name == "agent-prod-sandpit-langchain"
    assert langchain.model_name == "prod_sandpit_langchain_agent_model"


def test_model_artifact_verification_requires_signature_streaming_and_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = SimpleNamespace(
        signature=SimpleNamespace(
            to_dict=lambda: {
                "inputs": '[{"name": "input"}]',
                "outputs": '[{"name": "output"}]',
            },
        ),
        flavors={"python_function": {"streamable": True}},
        resources={"databricks": {"app": [{"name": "agent-dev-example"}]}},
    )
    monkeypatch.setattr(
        register_uc_model.Model,
        "load",
        lambda _uri: model,
    )

    register_uc_model._verify_model_artifact(
        "catalog.schema.model",
        "1",
        "agent-dev-example",
    )

    model.flavors["python_function"]["streamable"] = False
    with pytest.raises(RuntimeError, match="not streamable"):
        register_uc_model._verify_model_artifact(
            "catalog.schema.model",
            "1",
            "agent-dev-example",
        )


def test_registration_reuses_the_same_commit_and_updates_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = register_uc_model.model_registration(
        "dev",
        git_sha="abc123",
    )
    existing = SimpleNamespace(
        version="7",
        status="READY",
        tags={
            "source_git_commit": "abc123",
            "databricks_app": registration.app_name,
        },
    )

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.tags: dict[str, str] = {}
            self.alias: tuple[str, str, str] | None = None

        def search_model_versions(self, _filter: str) -> list[object]:
            return [existing]

        def get_model_version(self, _name: str, _version: str) -> object:
            return existing

        def set_model_version_tag(
            self,
            _name: str,
            _version: str,
            key: str,
            value: str,
        ) -> None:
            self.tags[key] = value

        def set_registered_model_alias(
            self,
            name: str,
            alias: str,
            version: str,
        ) -> None:
            self.alias = (name, alias, version)

        def get_model_version_by_alias(
            self,
            _name: str,
            _alias: str,
        ) -> object:
            return SimpleNamespace(version="7", tags=self.tags)

    fake_client = FakeClient()
    monkeypatch.setattr(
        register_uc_model,
        "MlflowClient",
        lambda **_kwargs: fake_client,
    )
    monkeypatch.setattr(
        register_uc_model,
        "_log_model_version",
        lambda *_args, **_kwargs: pytest.fail("must reuse the existing version"),
    )
    monkeypatch.setattr(
        register_uc_model,
        "_verify_model_artifact",
        lambda *_args, **_kwargs: None,
    )

    result = register_uc_model.register_model(
        registration,
        catalog="catalog",
        schema="schema",
        experiment_id="experiment",
    )

    assert result["version"] == "7"
    assert fake_client.alias == (
        "catalog.schema.dev_sandpit_langchain_agent_model",
        "deployed",
        "7",
    )


def test_registration_ignores_interrupted_model_versions() -> None:
    registration = register_uc_model.model_registration(
        "dev",
        git_sha="abc123",
    )
    pending = SimpleNamespace(
        version="1",
        status="PENDING_REGISTRATION",
        tags={
            "source_git_commit": registration.git_sha,
            "databricks_app": registration.app_name,
        },
    )
    ready = SimpleNamespace(
        version="2",
        status="READY",
        tags=pending.tags,
    )

    class FakeClient:
        def search_model_versions(self, _filter: str) -> list[object]:
            return [
                SimpleNamespace(version="1", tags=lambda: None),
                SimpleNamespace(version="2", tags=lambda: None),
            ]

        def get_model_version(self, _name: str, version: str) -> object:
            return {"1": pending, "2": ready}[version]

    assert register_uc_model._matching_version(
        FakeClient(),
        "catalog.schema.model",
        registration,
    ) is ready
