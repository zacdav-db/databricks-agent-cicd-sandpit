"""Tests for guarded App naming and replacement."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _write_databricks_stub(directory: Path, body: str) -> Path:
    executable = directory / "databricks"
    executable.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n" + body,
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_bundle_migration_unbinds_a_replaced_app(tmp_path: Path) -> None:
    legacy = tmp_path / ".generated" / "legacy-shared-bundle"
    bundle = tmp_path / "bundle"
    legacy.mkdir(parents=True)
    bundle.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.log"
    _write_databricks_stub(
        fake_bin,
        """
printf '%s|%s\n' "${PWD}" "$*" >> "${DATABRICKS_CALL_LOG}"
if [[ "$1 $2" == "bundle summary" ]]; then
  if [[ "${PWD}" == *"legacy-shared-bundle" ]]; then
    printf '{"resources":{"apps":{"mcp_server":{}}}}\n'
  else
    printf '{"resources":{"apps":{"mcp_server":{"id":"dev-sandpit-mcp-tools"}}}}\n'
  fi
elif [[ "$1 $2" == "apps get" ]]; then
  exit 1
fi
""",
    )
    environment = {
        **os.environ,
        "DATABRICKS_CALL_LOG": str(calls),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    subprocess.run(
        [
            ROOT / "scripts" / "migrate_app_bundle.sh",
            "dev",
            "bundle",
            "mcp_server",
            "mcp-dev-sandpit-tools",
            "-t",
            "dev",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
    )

    log = calls.read_text(encoding="utf-8")
    assert "bundle|bundle deployment unbind mcp_server -t dev --force-lock" in log


def test_runtime_deployment_rejects_a_non_prefixed_mcp_name(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_databricks_stub(
        fake_bin,
        """
if [[ "$1 $2" == "bundle summary" ]]; then
  printf '{"resources":{"apps":{"mcp_server":{"name":"dev-mcp-tools"}}}}\n'
fi
""",
    )
    environment = {
        **os.environ,
        "UC_CATALOG": "catalog",
        "UC_SCHEMA": "schema",
        "UC_TRACE_TABLE_PREFIX": "traces",
        "UC_COST_FUNCTION": "estimate_cost",
        "UC_TIME_FUNCTION": "current_time",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [ROOT / "scripts" / "deploy_runtime_app.sh", "dev", "mcp", "experiment"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Custom MCP App name must start with mcp-" in result.stderr


def test_runtime_deployment_rejects_a_non_prefixed_agent_name(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_databricks_stub(
        fake_bin,
        """
if [[ "$1 $2" == "bundle summary" ]]; then
  printf '{"resources":{"apps":{"langchain_agent":{"name":"dev-agent"}}}}\n'
fi
""",
    )
    environment = {
        **os.environ,
        "UC_CATALOG": "catalog",
        "UC_SCHEMA": "schema",
        "UC_TRACE_TABLE_PREFIX": "traces",
        "UC_COST_FUNCTION": "estimate_cost",
        "UC_TIME_FUNCTION": "current_time",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [
            ROOT / "scripts" / "deploy_runtime_app.sh",
            "dev",
            "langchain",
            "experiment",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "ResponsesAgent App name must start with agent-" in result.stderr
