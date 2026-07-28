"""Validate folder-defined agents and compose their Databricks App resources."""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

# prod-agent- is 11 characters and Databricks App names are limited to 30.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,18}$")
MODULE_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$",
)
FUNCTION_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MANIFEST_FIELDS = {"name", "model", "entrypoint"}
RESERVED_NAMES = {"langchain-agent", "mcp-tools", "omnigent"}
RESERVED_PATHS = {
    "_agent_runtime",
    "_agent_runtime.py",
    "agent_sdk",
    "agent_sdk.py",
}
PLATFORM_PACKAGES = {"fastapi", "mlflow", "pydantic", "uvicorn"}
SECRET_SUFFIXES = {".key", ".p12", ".pem"}
MAX_FILE_BYTES = 1_000_000
MAX_AGENT_BYTES = 5_000_000


class ContractError(ValueError):
    """Raised when an author-facing agent contract is invalid."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ContractError(f"Duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Validated agent input used to produce platform-owned files."""

    name: str
    model_alias: str
    model_endpoint: str
    entrypoint: str
    source: Path
    requirements: tuple[str, ...]

    @property
    def resource_key(self) -> str:
        return f"generated_agent_{self.name.replace('-', '_')}"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ContractError(f"Cannot read valid YAML from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain a YAML mapping.")
    return value


def _load_policy(root: Path) -> dict[str, str]:
    path = root / "agent_platform" / "policy.yaml"
    policy = _load_yaml(path)
    unknown = set(policy) - {"models"}
    if unknown:
        raise ContractError(f"Unknown platform policy fields: {sorted(unknown)}")
    models = policy.get("models")
    if not isinstance(models, dict) or not models:
        raise ContractError("agent_platform/policy.yaml must define non-empty models.")
    if not all(
        isinstance(alias, str) and isinstance(endpoint, str) and endpoint
        for alias, endpoint in models.items()
    ):
        raise ContractError("Every model alias and endpoint must be a non-empty string.")
    return models


def _validate_tree(folder: Path) -> None:
    total_size = 0
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise ContractError(f"Symlinks are not allowed: {relative}")
        if any(part.startswith(".") for part in relative.parts):
            raise ContractError(f"Hidden files and directories are not allowed: {relative}")
        if relative.parts[0] in RESERVED_PATHS:
            raise ContractError(f"Path is reserved by the platform: {relative}")
        if path.suffix.lower() in SECRET_SUFFIXES:
            raise ContractError(f"Credential files are not allowed: {relative}")
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ContractError(f"Agent file exceeds {MAX_FILE_BYTES} bytes: {relative}")
        total_size += size
    if total_size > MAX_AGENT_BYTES:
        raise ContractError(f"{folder.name} exceeds the {MAX_AGENT_BYTES}-byte limit.")


def _validate_python(folder: Path, entrypoint: str) -> None:
    module_name, separator, function_name = entrypoint.partition(":")
    if (
        not separator
        or not MODULE_PATTERN.fullmatch(module_name)
        or not FUNCTION_PATTERN.fullmatch(function_name)
    ):
        raise ContractError("entrypoint must use importable module:function syntax.")

    entrypoint_path = folder / f"{module_name.replace('.', '/')}.py"
    if not entrypoint_path.is_file():
        raise ContractError(f"Entrypoint module does not exist: {entrypoint_path}")

    entrypoint_tree: ast.Module | None = None
    for path in sorted(folder.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeError) as exc:
            raise ContractError(f"Invalid Python in {path}: {exc}") from exc
        if path == entrypoint_path:
            entrypoint_tree = tree

    if entrypoint_tree is None:
        raise ContractError(f"Could not parse entrypoint module: {entrypoint_path}")
    candidate = next(
        (
            node
            for node in entrypoint_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ),
        None,
    )
    if candidate is None:
        raise ContractError(f"Entrypoint function does not exist: {entrypoint}")
    arguments = candidate.args
    positional = [*arguments.posonlyargs, *arguments.args]
    if (
        [argument.arg for argument in positional] != ["message", "context"]
        or arguments.vararg
        or arguments.kwarg
        or arguments.kwonlyargs
        or arguments.defaults
    ):
        raise ContractError(
            "Entrypoint must be invoke(message, context) with no extra arguments.",
        )


def _read_requirements(folder: Path) -> tuple[str, ...]:
    path = folder / "requirements.txt"
    if not path.exists():
        return ()
    requirements: list[str] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") or " #" in line:
            raise ContractError(
                f"{path}:{line_number} must contain one exact package pin.",
            )
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise ContractError(f"Invalid requirement at {path}:{line_number}.") from exc
        specifiers = list(requirement.specifier)
        if (
            requirement.url
            or requirement.marker
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise ContractError(
                f"{path}:{line_number} must be an exact registry pin using ==.",
            )
        package_name = canonicalize_name(requirement.name)
        if package_name in PLATFORM_PACKAGES:
            raise ContractError(f"{requirement.name} is owned by the agent platform.")
        requirements.append(str(requirement))
    return tuple(sorted(requirements, key=str.casefold))


def discover_agents(root: Path) -> list[AgentDefinition]:
    """Read and validate every direct child under agents/."""
    models = _load_policy(root)
    agents_root = root / "agents"
    definitions: list[AgentDefinition] = []
    seen_resources: set[str] = set()
    for folder in sorted(
        (path for path in agents_root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    ):
        if folder.name.startswith("."):
            continue
        manifest_path = folder / "agent.yaml"
        if not manifest_path.is_file():
            raise ContractError(f"Agent folder is missing agent.yaml: {folder}")
        _validate_tree(folder)
        manifest = _load_yaml(manifest_path)
        unknown = set(manifest) - MANIFEST_FIELDS
        missing = MANIFEST_FIELDS - set(manifest)
        if unknown or missing:
            raise ContractError(
                f"{manifest_path} fields must be exactly {sorted(MANIFEST_FIELDS)}; "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}.",
            )
        if not all(isinstance(manifest[field], str) for field in MANIFEST_FIELDS):
            raise ContractError(f"All fields in {manifest_path} must be strings.")
        name = manifest["name"]
        if not NAME_PATTERN.fullmatch(name):
            raise ContractError(f"Invalid agent name: {name!r}")
        if name != folder.name:
            raise ContractError(f"Folder {folder.name!r} must match agent name {name!r}.")
        if name in RESERVED_NAMES:
            raise ContractError(f"Agent name is reserved: {name}")
        model_alias = manifest["model"]
        if model_alias not in models:
            raise ContractError(
                f"Unknown model alias {model_alias!r}; choose one of {sorted(models)}.",
            )
        entrypoint = manifest["entrypoint"]
        _validate_python(folder, entrypoint)
        definition = AgentDefinition(
            name=name,
            model_alias=model_alias,
            model_endpoint=models[model_alias],
            entrypoint=entrypoint,
            source=folder,
            requirements=_read_requirements(folder),
        )
        if definition.resource_key in seen_resources:
            raise ContractError(f"Generated resource key collision: {definition.resource_key}")
        seen_resources.add(definition.resource_key)
        definitions.append(definition)
    return definitions


def _trace_resources(model_endpoint: str) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = [
        {
            "name": "llm",
            "serving_endpoint": {
                "name": model_endpoint,
                "permission": "CAN_QUERY",
            },
        },
        {
            "name": "trace_experiment",
            "experiment": {
                "experiment_id": "${var.experiment_id}",
                "permission": "CAN_EDIT",
            },
        },
        {
            "name": "trace_warehouse",
            "sql_warehouse": {
                "id": "${var.warehouse_id}",
                "permission": "CAN_USE",
            },
        },
    ]
    for table in ("annotations", "logs", "metrics", "spans"):
        for permission in ("SELECT", "MODIFY"):
            resources.append(
                {
                    "name": f"trace_{table}_{permission.lower()}",
                    "uc_securable": {
                        "securable_full_name": (
                            "${var.catalog}.${var.schema}."
                            f"${{var.trace_table_prefix}}_otel_{table}"
                        ),
                        "securable_type": "TABLE",
                        "permission": permission,
                    },
                },
            )
    return resources


def _app_resource(agent: AgentDefinition) -> dict[str, Any]:
    return {
        "name": f"${{var.resource_prefix}}-agent-{agent.name}",
        "description": (
            f"Folder-defined {agent.name} agent generated by the platform contract."
        ),
        "source_code_path": f"../agents/{agent.name}",
        "config": {
            "command": [
                "uvicorn",
                "_agent_runtime:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
            ],
            "env": [
                {"name": "MLFLOW_TRACKING_URI", "value": "databricks"},
                {"name": "MLFLOW_EXPERIMENT_ID", "value_from": "trace_experiment"},
                {
                    "name": "MLFLOW_TRACING_SQL_WAREHOUSE_ID",
                    "value_from": "trace_warehouse",
                },
                {"name": "MODEL_ENDPOINT", "value_from": "llm"},
                {"name": "AGENT_NAME", "value": agent.name},
                {"name": "AGENT_ENTRYPOINT", "value": agent.entrypoint},
                {"name": "DEPLOYMENT_ENV", "value": "${bundle.target}"},
            ],
        },
        "resources": _trace_resources(agent.model_endpoint),
        "permissions": [{"level": "CAN_USE", "group_name": "users"}],
    }


def compose(root: Path) -> dict[str, Any]:
    """Generate App sources, DAB resources, and a deployment index atomically."""
    root = root.resolve()
    output = root / ".generated"
    agents = discover_agents(root)

    temporary = Path(tempfile.mkdtemp(prefix=".agent-build-", dir=root))
    try:
        bundle_dir = temporary / "bundle"
        generated_agents_dir = temporary / "agents"
        bundle_dir.mkdir()
        generated_agents_dir.mkdir()
        platform_requirements = (
            root / "agent_platform" / "requirements.txt"
        ).read_text(encoding="utf-8").strip()

        resources: dict[str, Any] = {}
        index_agents: list[dict[str, str]] = []
        for agent in agents:
            destination = generated_agents_dir / agent.name
            shutil.copytree(
                agent.source,
                destination,
                ignore=shutil.ignore_patterns(
                    "agent.yaml",
                    "requirements.txt",
                    "__pycache__",
                    "*.pyc",
                ),
            )
            shutil.copy2(
                root / "agent_platform" / "runtime.py",
                destination / "_agent_runtime.py",
            )
            shutil.copytree(root / "agent_sdk", destination / "agent_sdk")
            dependency_lines = [platform_requirements, *agent.requirements]
            (destination / "requirements.txt").write_text(
                "\n".join(line for line in dependency_lines if line).rstrip() + "\n",
                encoding="utf-8",
            )
            resources[agent.resource_key] = _app_resource(agent)
            index_agents.append(
                {
                    "name": agent.name,
                    "resource_key": agent.resource_key,
                    "app_name": f"${{var.resource_prefix}}-agent-{agent.name}",
                    "model_alias": agent.model_alias,
                    "model_endpoint": agent.model_endpoint,
                },
            )

        bundle_payload = {"resources": {"apps": resources}}
        (bundle_dir / "generated_agents.yml").write_text(
            yaml.safe_dump(bundle_payload, sort_keys=False),
            encoding="utf-8",
        )
        index = {"contract_version": 1, "agents": index_agents}
        (temporary / "agent-index.json").write_text(
            json.dumps(index, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if output.exists():
            shutil.rmtree(output)
        temporary.replace(output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    index = compose(args.root)
    print(json.dumps(index, sort_keys=True))


if __name__ == "__main__":
    main()
