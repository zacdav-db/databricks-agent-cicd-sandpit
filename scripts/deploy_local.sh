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

bootstrap_json="$(
  "${python_bin}" scripts/bootstrap_resources.py \
    --profile "${profile}"
)"
experiment_id="$(jq -r '.experiment_id' <<<"${bootstrap_json}")"

databricks bundle validate \
  -t "${target}" \
  --var "experiment_id=${experiment_id}"
databricks bundle deploy \
  -t "${target}" \
  --auto-approve \
  --var "experiment_id=${experiment_id}"
databricks bundle run mcp_server \
  -t "${target}" \
  --var "experiment_id=${experiment_id}"
databricks bundle run langchain_agent \
  -t "${target}" \
  --var "experiment_id=${experiment_id}"
databricks bundle run omnigent \
  -t "${target}" \
  --var "experiment_id=${experiment_id}"

"${python_bin}" scripts/smoke_test.py \
  --profile "${profile}" \
  --target "${target}"
