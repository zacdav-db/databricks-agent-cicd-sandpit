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
    agent = tmp_path / "agents" / "small-agent"
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text(
        manifest
        or "name: small-agent\nmodel: default\nentrypoint: agent:invoke\n",
        encoding="utf-8",
    )
    (agent / "agent.py").write_text(
        source or "def invoke(message):\n    return f'Received: {message}'\n",
        encoding="utf-8",
    )
    if requirements is not None:
        (agent / "requirements.txt").write_text(requirements, encoding="utf-8")
    return tmp_path, agent


def test_compose_agents_is_deterministic_and_platform_owned(tmp_path: Path) -> None:
    root, agent = _repository(tmp_path)
    author_source = (agent / "agent.py").read_bytes()
    git_marker = root / ".git" / "keep"
    git_marker.parent.mkdir()
    git_marker.write_text("repository metadata", encoding="utf-8")

    index = compose_agents.compose(root)
    bundle_path = root / ".generated/bundles/small-agent/databricks.yml"
    first_bundle = bundle_path.read_bytes()
    first_app = (
        root / ".generated/bundles/small-agent/app/_agent_runtime.py"
    ).read_bytes()
    first_index = (root / ".generated/agent-index.json").read_bytes()
    compose_agents.compose(root)

    assert first_bundle == bundle_path.read_bytes()
    assert first_app == (
        root / ".generated/bundles/small-agent/app/_agent_runtime.py"
    ).read_bytes()
    assert first_index == (root / ".generated/agent-index.json").read_bytes()
    assert git_marker.read_text(encoding="utf-8") == "repository metadata"
    assert (root / "agents/small-agent/agent.py").is_file()
    assert (root / "agents/small-agent/agent.py").read_bytes() == author_source
    assert index["contract_version"] == 5
    assert index["agents"][0]["resource_key"] == "generated_agent_small_agent"
    assert index["agents"][0]["bundle_path"] == (
        ".generated/bundles/small-agent"
    )

    bundle = yaml.safe_load(first_bundle)
    assert bundle["bundle"]["name"] == "sandpit-folder-agent-small-agent"
    assert bundle["targets"]["dev"]["workspace"]["root_path"] == (
        "/Workspace/Users/${workspace.current_user.userName}"
        "/.bundle/${bundle.name}/${bundle.target}"
    )
    app = bundle["resources"]["apps"]["generated_agent_small_agent"]
    assert app["name"] == "agent-${var.resource_prefix}-small-agent"
    assert index["agents"][0]["app_name"] == (
        "agent-${var.resource_prefix}-small-agent"
    )
    assert app["source_code_path"] == "./app"
    assert app["config"]["command"][1] == "_agent_runtime:app"
    assert app["resources"][0]["serving_endpoint"]["name"] == (
        "databricks-claude-sonnet-4-5"
    )
    env_names = {item["name"] for item in app["config"]["env"]}
    assert env_names == {
        "AGENT_ENTRYPOINT",
        "AGENT_NAME",
        "MLFLOW_EXPERIMENT_ID",
        "MLFLOW_TRACING_SQL_WAREHOUSE_ID",
        "MLFLOW_TRACKING_URI",
        "MODEL_ENDPOINT",
    }
    generated = root / ".generated/bundles/small-agent/app"
    assert (generated / "_agent_runtime.py").is_file()
    assert (generated / "_platform_tracing.py").is_file()
    assert not (generated / "agent_sdk").exists()
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
        "mlflow-tracing==3.14.0\n",
        "anyio==4.10.0\n",
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
        "def invoke(message, secret=None):\n"
        "    return message\n",
    )
    with pytest.raises(compose_agents.ContractError, match="exactly one argument"):
        compose_agents.compose(root)


def test_optional_stream_signature_is_validated_without_manifest_field(
    tmp_path: Path,
) -> None:
    root, _ = _repository(
        tmp_path,
        source=(
            "def invoke(message):\n"
            "    return message\n\n"
            "def invoke_stream(message, options=None):\n"
            "    yield message\n"
        ),
    )
    with pytest.raises(
        compose_agents.ContractError,
        match="Optional stream function invoke_stream",
    ):
        compose_agents.compose(root)


def test_symlinks_are_rejected(tmp_path: Path) -> None:
    root, agent = _repository(tmp_path)
    (agent / "linked.py").symlink_to(agent / "agent.py")
    with pytest.raises(compose_agents.ContractError, match="Symlinks"):
        compose_agents.compose(root)


@pytest.mark.parametrize(
    "reserved_path",
    ["_agent_runtime.py", "_platform_tracing.py"],
)
def test_platform_runtime_paths_are_reserved(
    tmp_path: Path,
    reserved_path: str,
) -> None:
    root, agent = _repository(tmp_path)
    (agent / reserved_path).write_text("author override", encoding="utf-8")
    with pytest.raises(compose_agents.ContractError, match="reserved"):
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
            "name": "claude-assistant",
            "model": "claude",
            "entrypoint": "agent:invoke",
        },
        {
            "name": "gemini-assistant",
            "model": "gemini",
            "entrypoint": "agent:invoke",
        },
        {
            "name": "langchain-assistant",
            "model": "default",
            "entrypoint": "agent:invoke",
        },
        {
            "name": "openai-assistant",
            "model": "openai",
            "entrypoint": "agent:invoke",
        },
    ]
    assert {
        definition.model_alias: definition.model_endpoint
        for definition in definitions
    } == {
        "claude": "databricks-claude-haiku-4-5",
        "default": "databricks-claude-sonnet-4-5",
        "gemini": "databricks-gemini-3-1-flash-lite",
        "openai": "databricks-gpt-5-mini",
    }


def test_every_app_has_one_unique_bundle_state() -> None:
    compose_agents.compose(ROOT)
    bundle_paths = [
        ROOT / "src/langchain_agent/databricks.yml",
        ROOT / "src/mcp_server/databricks.yml",
        ROOT / "src/omnigent_app/databricks.yml",
        *sorted((ROOT / ".generated/bundles").glob("*/databricks.yml")),
    ]
    bundles = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in bundle_paths
    ]
    bundle_names = [bundle["bundle"]["name"] for bundle in bundles]
    assert len(bundle_names) == 7
    assert len(set(bundle_names)) == len(bundle_names)
    assert all(len(bundle["resources"]["apps"]) == 1 for bundle in bundles)
    assert all(
        next(iter(bundle["resources"]["apps"].values()))["name"].startswith(
            ("agent-", "mcp-", "${var.resource_prefix}-sandpit-omnigent"),
        )
        for bundle in bundles
    )
    assert all(
        bundle["targets"]["dev"]["workspace"]["root_path"].endswith(
            "/.bundle/${bundle.name}/${bundle.target}",
        )
        for bundle in bundles
    )


@pytest.mark.parametrize(
    "name",
    ["claude-assistant", "gemini-assistant", "openai-assistant"],
)
def test_provider_examples_have_no_langchain_or_platform_sdk_dependency(
    name: str,
) -> None:
    folder = ROOT / "agents" / name
    author_surface = (
        (folder / "agent.py").read_text(encoding="utf-8")
        + (folder / "requirements.txt").read_text(encoding="utf-8")
    ).casefold()
    assert "langchain" not in author_surface
    assert "agent_sdk" not in author_surface
