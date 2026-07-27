#!/usr/bin/env bash
set -euo pipefail

profile="${DATABRICKS_CONFIG_PROFILE:-sandpit}"
target="${1:-dev}"
python_bin="${PYTHON_BIN:-.venv/bin/python}"

if [[ ! -x "${python_bin}" ]]; then
  echo "Python environment not found at ${python_bin}." >&2
  echo "Create .venv as described in README.md or set PYTHON_BIN." >&2
  exit 1
fi

DATABRICKS_CONFIG_PROFILE="${profile}" \
PYTHON_BIN="${python_bin}" \
  scripts/deploy_target.sh "${target}"
