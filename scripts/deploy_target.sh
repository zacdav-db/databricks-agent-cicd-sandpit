#!/usr/bin/env bash
set -euo pipefail

target="${1:?Usage: deploy_target.sh <dev|prod>}"
python_bin="${PYTHON_BIN:-python}"
export PYTHON_BIN="${python_bin}"

if [[ "${target}" != "dev" && "${target}" != "prod" ]]; then
  echo "Target must be dev or prod." >&2
  exit 1
fi
if [[ ! -x "${python_bin}" ]]; then
  echo "Python executable not found at ${python_bin}." >&2
  exit 1
fi

printf 'Bootstrapping isolated %s Unity Catalog resources\n' "${target}"
bootstrap_json="$(
  "${python_bin}" scripts/bootstrap_resources.py --target "${target}"
)"
catalog="$(jq -er '.catalog' <<<"${bootstrap_json}")"
schema="$(jq -er '.schema' <<<"${bootstrap_json}")"
cost_function_name="$(jq -er '.cost_function_name' <<<"${bootstrap_json}")"
time_function_name="$(jq -er '.time_function_name' <<<"${bootstrap_json}")"
table_prefix="$(jq -er '.table_prefix' <<<"${bootstrap_json}")"
experiment_id="$(jq -er '.experiment_id' <<<"${bootstrap_json}")"
export UC_CATALOG="${catalog}"
export UC_SCHEMA="${schema}"
export UC_COST_FUNCTION="${cost_function_name}"
export UC_TIME_FUNCTION="${time_function_name}"
export UC_TRACE_TABLE_PREFIX="${table_prefix}"

bundle_args=(
  -t "${target}"
  --var "experiment_id=${experiment_id}"
  --var "catalog=${catalog}"
  --var "schema=${schema}"
  --var "uc_function_name=${cost_function_name}"
  --var "uc_time_function_name=${time_function_name}"
  --var "trace_table_prefix=${table_prefix}"
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
