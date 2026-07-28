from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load_composer():
    name = "folder_agent_composer"
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "scripts" / "compose_agents.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load scripts/compose_agents.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compose_agents = _load_composer()


def _repository(
    tmp_path: Path,
    *,
    manifest: str | None = None,
    source: str | None = None,
    requirements: str | None = "example-package==1.2.3\n",
) -> tuple[Path, Path]:
    shutil.copytree(ROOT / "agent_platform", tmp_path / "agent_platform")
    shutil.copytree(ROOT / "agent_sdk", tmp_path / "agent_sdk")
    agent = tmp_path / "agents" / "small-agent"
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text(
        manifest
        or "name: small-agent\nmodel: default\nentrypoint: agent:invoke\n",
        encoding="utf-8",
    )
    (agent / "agent.py").write_text(
        source
        or "def invoke(message, context):\n    return f'{context.name}: {message}'\n",
        encoding="utf-8",
    )
    if requirements is not None:
        (agent / "requirements.txt").write_text(requirements, encoding="utf-8")
    return tmp_path, agent


def test_compose_agents_is_deterministic_and_platform_owned(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path)
    git_marker = root / ".git" / "keep"
    git_marker.parent.mkdir()
    git_marker.write_text("repository metadata", encoding="utf-8")

    index = compose_agents.compose(root)
    first_bundle = (root / ".generated/bundle/generated_agents.yml").read_bytes()
    first_index = (root / ".generated/agent-index.json").read_bytes()
    compose_agents.compose(root)

    assert first_bundle == (
        root / ".generated/bundle/generated_agents.yml"
    ).read_bytes()
    assert first_index == (root / ".generated/agent-index.json").read_bytes()
    assert git_marker.read_text(encoding="utf-8") == "repository metadata"
    assert (root / "agents/small-agent/agent.py").is_file()
    assert index["agents"][0]["resource_key"] == "generated_agent_small_agent"

    bundle = yaml.safe_load(first_bundle)
    app = bundle["resources"]["apps"]["generated_agent_small_agent"]
    assert app["name"] == "${var.resource_prefix}-agent-small-agent"
    assert app["source_code_path"] == "../agents/small-agent"
    assert app["config"]["command"][1] == "_agent_runtime:app"
    assert app["resources"][0]["serving_endpoint"]["name"] == (
        "databricks-claude-sonnet-4-5"
    )
    env_names = {item["name"] for item in app["config"]["env"]}
    assert env_names == {
        "AGENT_ENTRYPOINT",
        "AGENT_NAME",
        "DEPLOYMENT_ENV",
        "MLFLOW_EXPERIMENT_ID",
        "MLFLOW_TRACING_SQL_WAREHOUSE_ID",
        "MLFLOW_TRACKING_URI",
        "MODEL_ENDPOINT",
    }
    generated = root / ".generated/agents/small-agent"
    assert (generated / "_agent_runtime.py").is_file()
    assert (generated / "agent_sdk/contract.py").is_file()
    assert not (generated / "agent.yaml").exists()
    assert "example-package==1.2.3" in (
        generated / "requirements.txt"
    ).read_text(encoding="utf-8")
    assert "fastapi==0.115.14" in (
        generated / "requirements.txt"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("manifest", "error"),
    [
        (
            "name: small-agent\nname: repeated\nmodel: default\n"
            "entrypoint: agent:invoke\n",
            "Duplicate YAML key",
        ),
        (
            "name: small-agent\nmodel: default\nentrypoint: agent:invoke\n"
            "env: unsafe\n",
            "fields must be exactly",
        ),
        (
            "name: different-agent\nmodel: default\nentrypoint: agent:invoke\n",
            "must match agent name",
        ),
        (
            "name: small-agent\nmodel: arbitrary-endpoint\n"
            "entrypoint: agent:invoke\n",
            "Unknown model alias",
        ),
        (
            "name: abcdefghijklmnopqrst\nmodel: default\n"
            "entrypoint: agent:invoke\n",
            "Invalid agent name",
        ),
    ],
)
def test_manifest_contract_is_strict(
    tmp_path: Path,
    manifest: str,
    error: str,
) -> None:
    root, _ = _repository(tmp_path, manifest=manifest)
    with pytest.raises(compose_agents.ContractError, match=error):
        compose_agents.compose(root)


@pytest.mark.parametrize(
    "requirements",
    [
        "example-package>=1.2\n",
        "example-package @ https://example.test/package.whl\n",
        "-r another-file.txt\n",
        "mlflow==3.14.0\n",
    ],
)
def test_requirements_reject_unsafe_or_platform_owned_entries(
    tmp_path: Path,
    requirements: str,
) -> None:
    root, _ = _repository(tmp_path, requirements=requirements)
    with pytest.raises(compose_agents.ContractError):
        compose_agents.compose(root)


def test_entrypoint_signature_is_validated_without_importing_code(
    tmp_path: Path,
) -> None:
    root, _ = _repository(
        tmp_path,
        source="raise RuntimeError('must not import')\n"
        "def invoke(message, context, secret=None):\n"
        "    return message\n",
    )
    with pytest.raises(compose_agents.ContractError, match="no extra arguments"):
        compose_agents.compose(root)


def test_symlinks_are_rejected(tmp_path: Path) -> None:
    root, agent = _repository(tmp_path)
    (agent / "linked.py").symlink_to(agent / "agent.py")
    with pytest.raises(compose_agents.ContractError, match="Symlinks"):
        compose_agents.compose(root)


def test_repository_example_composes() -> None:
    definitions = compose_agents.discover_agents(ROOT)
    assert [
        {
            "name": definition.name,
            "model": definition.model_alias,
            "entrypoint": definition.entrypoint,
        }
        for definition in definitions
    ] == [
        {
            "name": "minimal-assistant",
            "model": "default",
            "entrypoint": "agent:invoke",
        },
    ]
