"""Select independently deployable Apps from a Git commit range."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

AGENT_PLATFORM_PATHS = (
    "agent_platform/_platform_tracing.py",
    "agent_platform/policy.yaml",
    "agent_platform/requirements.txt",
    "agent_platform/runtime.py",
    "scripts/compose_agents.py",
)
AGENT_MODEL_PLATFORM_PATHS = (
    "agent_platform/__init__.py",
    "agent_platform/app_proxy_model.py",
    "scripts/register_uc_model.py",
)
DEPLOYMENT_PLATFORM_PATHS = (
    "requirements-deploy-core.txt",
    "requirements-deploy.txt",
    "scripts/app_names.py",
    "scripts/bootstrap_resources.py",
    "scripts/deploy_agent.sh",
    "scripts/deploy_runtime_app.sh",
    "scripts/deploy_target.sh",
    "scripts/migrate_app_bundle.sh",
    "scripts/register_uc_agent.py",
    "scripts/retire_replaced_app.sh",
    "scripts/select_deployments.py",
    "scripts/smoke_agent.py",
    "scripts/smoke_runtime_app.py",
    "scripts/smoke_test.py",
)
RUNTIME_APP_PATHS = {
    "mcp": ("src/mcp_server/",),
    "langchain": ("src/langchain_agent/",),
    "omnigent": ("src/omnigent_app/",),
}
RUNTIME_APP_ORDER = ("mcp", "langchain", "omnigent")

if set(RUNTIME_APP_ORDER) != set(RUNTIME_APP_PATHS):
    raise RuntimeError("RUNTIME_APP_ORDER must contain every runtime App exactly once.")


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in prefixes
    )


def select_deployments(
    changed_paths: list[str],
    agent_names: list[str],
) -> dict[str, object]:
    """Map changed repository paths to isolated deployment units."""
    known_agents = set(agent_names)
    deploy_all_units = any(
        _matches(path, DEPLOYMENT_PLATFORM_PATHS)
        for path in changed_paths
    )
    deploy_all_agents = deploy_all_units or any(
        _matches(path, AGENT_PLATFORM_PATHS)
        for path in changed_paths
    )
    deploy_all_agent_models = any(
        _matches(path, AGENT_MODEL_PLATFORM_PATHS)
        for path in changed_paths
    )
    deploy_all_agents = deploy_all_agents or deploy_all_agent_models
    selected_agents = known_agents if deploy_all_agents else {
        parts[1]
        for path in changed_paths
        if len(parts := Path(path).parts) >= 2
        and parts[0] == "agents"
        and parts[1] in known_agents
    }
    selected_apps: list[str] = (
        list(RUNTIME_APP_ORDER)
        if deploy_all_units
        else [
            app
            for app in RUNTIME_APP_ORDER
            if any(
                _matches(path, RUNTIME_APP_PATHS[app])
                for path in changed_paths
            )
        ]
    )
    if deploy_all_agent_models and "langchain" not in selected_apps:
        selected_apps.append("langchain")
        selected_apps.sort(key=RUNTIME_APP_ORDER.index)
    return {
        "apps": selected_apps,
        "agents": sorted(selected_agents),
    }


def _changed_paths(root: Path, base: str, head: str) -> list[str]:
    if not base or set(base) == {"0"}:
        return [path.as_posix() for path in root.rglob("*") if path.is_file()]
    result = subprocess.run(
        ["git", "diff", "--name-only", base, head],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _agent_names(root: Path) -> list[str]:
    return sorted(
        path.parent.name
        for path in (root / "agents").glob("*/agent.yaml")
        if path.is_file()
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    selection = select_deployments(
        _changed_paths(root, args.base, args.head),
        _agent_names(root),
    )
    print(json.dumps(selection, sort_keys=True))


if __name__ == "__main__":
    main()
