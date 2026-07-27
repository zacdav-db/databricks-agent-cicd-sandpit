#!/usr/bin/env bash
set -euo pipefail

target="${1:?Usage: deploy_target.sh <dev|prod> <experiment-id>}"
experiment_id="${2:?Usage: deploy_target.sh <dev|prod> <experiment-id>}"
python_bin="${PYTHON_BIN:-python}"
export PYTHON_BIN="${python_bin}"
export UC_CATALOG="${UC_CATALOG:-zacdav_sandpit_catalog}"
export UC_SCHEMA="${UC_SCHEMA:-default}"
export UC_COST_FUNCTION="${UC_COST_FUNCTION:-estimate_project_cost}"
export UC_TIME_FUNCTION="${UC_TIME_FUNCTION:-current_utc_timestamp}"
export UC_TRACE_TABLE_PREFIX="${UC_TRACE_TABLE_PREFIX:-sandpit_agent_cicd}"

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
  --var "catalog=${UC_CATALOG}"
  --var "schema=${UC_SCHEMA}"
  --var "uc_function_name=${UC_COST_FUNCTION}"
  --var "uc_time_function_name=${UC_TIME_FUNCTION}"
  --var "trace_table_prefix=${UC_TRACE_TABLE_PREFIX}"
)

printf 'Validating %s bundle\n' "${target}"
databricks bundle validate "${bundle_args[@]}"

printf 'Deploying %s bundle\n' "${target}"
databricks bundle deploy \
  "${bundle_args[@]}" \
  --auto-approve \
  --force-lock

for app in mcp_server langchain_agent; do
  printf 'Starting %s in %s\n' "${app}" "${target}"
  databricks bundle run "${app}" "${bundle_args[@]}"
done

printf 'Registering the %s agent in Unity Catalog\n' "${target}"
databricks bundle run register_uc_agent "${bundle_args[@]}"

printf 'Starting omnigent in %s\n' "${target}"
databricks bundle run omnigent "${bundle_args[@]}"

printf 'Smoke testing %s\n' "${target}"
databricks bundle run smoke_test "${bundle_args[@]}"
