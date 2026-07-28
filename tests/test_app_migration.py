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


def test_replacement_must_exist_before_legacy_app_is_retired(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.log"
    _write_databricks_stub(
        fake_bin,
        """
printf '%s\n' "$*" >> "${DATABRICKS_CALL_LOG}"
if [[ "$1 $2" == "apps get" ]]; then
  echo "replacement unavailable" >&2
  exit 1
fi
""",
    )
    environment = {
        **os.environ,
        "DATABRICKS_CALL_LOG": str(calls),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [
            ROOT / "scripts" / "retire_replaced_app.sh",
            "agent-dev-new",
            "dev-agent-old",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "apps delete" not in calls.read_text(encoding="utf-8")


def test_exact_legacy_app_is_retired_after_replacement_exists(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.log"
    _write_databricks_stub(
        fake_bin,
        """
printf '%s\n' "$*" >> "${DATABRICKS_CALL_LOG}"
if [[ "$1 $2" == "apps get" ]]; then
  printf '{"name":"%s",' "$3"
  printf '"app_status":{"state":"RUNNING"},'
  printf '"compute_status":{"state":"ACTIVE"},'
  printf '"active_deployment":{"status":{"state":"SUCCEEDED"}}}\n'
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
            ROOT / "scripts" / "retire_replaced_app.sh",
            "agent-dev-new",
            "dev-agent-old",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
    )

    log = calls.read_text(encoding="utf-8")
    assert "apps get agent-dev-new -o json" in log
    assert "apps get dev-agent-old -o json" in log
    assert "apps delete dev-agent-old --auto-approve" in log


def test_legacy_lookup_error_never_deletes_the_app(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.log"
    _write_databricks_stub(
        fake_bin,
        """
printf '%s\n' "$*" >> "${DATABRICKS_CALL_LOG}"
if [[ "$1 $2 $3" == "apps get agent-dev-new" ]]; then
  printf '{"name":"agent-dev-new",'
  printf '"app_status":{"state":"RUNNING"},'
  printf '"compute_status":{"state":"ACTIVE"},'
  printf '"active_deployment":{"status":{"state":"SUCCEEDED"}}}\n'
elif [[ "$1 $2" == "apps get" ]]; then
  echo "authorization failed" >&2
  exit 1
fi
""",
    )
    environment = {
        **os.environ,
        "DATABRICKS_CALL_LOG": str(calls),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [
            ROOT / "scripts" / "retire_replaced_app.sh",
            "agent-dev-new",
            "dev-agent-old",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "authorization failed" in result.stderr
    assert "apps delete" not in calls.read_text(encoding="utf-8")


def test_absent_legacy_app_needs_no_delete(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.log"
    _write_databricks_stub(
        fake_bin,
        """
printf '%s\n' "$*" >> "${DATABRICKS_CALL_LOG}"
if [[ "$1 $2 $3" == "apps get agent-dev-new" ]]; then
  printf '{"name":"agent-dev-new",'
  printf '"app_status":{"state":"RUNNING"},'
  printf '"compute_status":{"state":"ACTIVE"},'
  printf '"active_deployment":{"status":{"state":"SUCCEEDED"}}}\n'
elif [[ "$1 $2" == "apps get" ]]; then
  echo "App does not exist or is deleted." >&2
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
            ROOT / "scripts" / "retire_replaced_app.sh",
            "agent-dev-new",
            "dev-agent-old",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
    )

    assert "apps delete" not in calls.read_text(encoding="utf-8")


def test_non_running_replacement_never_deletes_the_legacy_app(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.log"
    _write_databricks_stub(
        fake_bin,
        """
printf '%s\n' "$*" >> "${DATABRICKS_CALL_LOG}"
if [[ "$1 $2" == "apps get" ]]; then
  printf '{"name":"agent-dev-new",'
  printf '"app_status":{"state":"RUNNING"},'
  printf '"compute_status":{"state":"STARTING"},'
  printf '"active_deployment":{"status":{"state":"SUCCEEDED"}}}\n'
fi
""",
    )
    environment = {
        **os.environ,
        "DATABRICKS_CALL_LOG": str(calls),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [
            ROOT / "scripts" / "retire_replaced_app.sh",
            "agent-dev-new",
            "dev-agent-old",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "not fully running" in result.stderr
    assert "apps delete" not in calls.read_text(encoding="utf-8")


def test_retirement_runs_after_deployment_and_topology_smoke() -> None:
    source = (ROOT / "scripts" / "deploy_target.sh").read_text(encoding="utf-8")
    first_retirement = source.index("scripts/retire_replaced_app.sh")
    assert source.index("scripts/deploy_agent.sh") < first_retirement
    assert source.index("--app topology") < first_retirement
