#!/usr/bin/env bash
set -euo pipefail

target="${1:?Usage: deploy_target.sh <dev|prod> <experiment-id>}"
experiment_id="${2:?Usage: deploy_target.sh <dev|prod> <experiment-id>}"
python_bin="${PYTHON_BIN:-python}"

if [[ "${target}" != "dev" && "${target}" != "prod" ]]; then
  echo "Target must be dev or prod." >&2
  exit 1
fi
if [[ ! -x "${python_bin}" ]]; then
  echo "Python executable not found at ${python_bin}." >&2
  exit 1
fi

bundle_args=(
  -t "${target}"
  --var "experiment_id=${experiment_id}"
)

printf 'Validating %s bundle\n' "${target}"
databricks bundle validate "${bundle_args[@]}"

printf 'Deploying %s bundle\n' "${target}"
databricks bundle deploy \
  "${bundle_args[@]}" \
  --auto-approve \
  --force-lock

for app in mcp_server langchain_agent omnigent; do
  printf 'Starting %s in %s\n' "${app}" "${target}"
  databricks bundle run "${app}" "${bundle_args[@]}"
done

smoke_args=(--target "${target}")
if [[ -n "${DATABRICKS_CONFIG_PROFILE:-}" ]]; then
  smoke_args+=(--profile "${DATABRICKS_CONFIG_PROFILE}")
fi

printf 'Smoke testing %s\n' "${target}"
"${python_bin}" scripts/smoke_test.py "${smoke_args[@]}"
