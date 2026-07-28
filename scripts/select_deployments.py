"""Select independently deployable Apps from a Git commit range."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

AGENT_PLATFORM_PATHS = (
    "agent_platform/",
    "scripts/compose_agents.py",
    "scripts/deploy_agent.sh",
)
RUNTIME_APP_PATHS = {
    "langchain": ("src/langchain_agent/",),
    "mcp": ("src/mcp_server/",),
    "omnigent": ("src/omnigent_app/",),
}


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
    deploy_all_agents = any(
        _matches(path, AGENT_PLATFORM_PATHS)
        for path in changed_paths
    )
    selected_agents = known_agents if deploy_all_agents else {
        parts[1]
        for path in changed_paths
        if len(parts := Path(path).parts) >= 2
        and parts[0] == "agents"
        and parts[1] in known_agents
    }
    selected_apps = sorted(
        app
        for app, prefixes in RUNTIME_APP_PATHS.items()
        if any(_matches(path, prefixes) for path in changed_paths)
    )
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
