#!/usr/bin/env bash
set -euo pipefail

target="${1:?Usage: deploy_target.sh <dev|prod> <base-sha> <head-sha>}"
base_sha="${2:?Usage: deploy_target.sh <dev|prod> <base-sha> <head-sha>}"
head_sha="${3:?Usage: deploy_target.sh <dev|prod> <base-sha> <head-sha>}"
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

printf 'Composing folder-defined agent resources\n'
"${python_bin}" scripts/compose_agents.py

selection="$(
  "${python_bin}" scripts/select_deployments.py \
    --base "${base_sha}" \
    --head "${head_sha}"
)"
printf 'Selected deployments: %s\n' "${selection}"
if [[ "$(jq -r '.apps | length' <<<"${selection}")" == "0" ]] &&
  [[ "$(jq -r '.agents | length' <<<"${selection}")" == "0" ]]; then
  printf 'No deployable App changed; skipping Databricks deployment\n'
  exit 0
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
export DATABRICKS_WAREHOUSE_ID="f7a871ffa2a9ab80"

while IFS= read -r component; do
  scripts/deploy_runtime_app.sh \
    "${target}" \
    "${component}" \
    "${experiment_id}"
done < <(jq -r '.apps[]' <<<"${selection}")

while IFS= read -r agent_name; do
  scripts/deploy_agent.sh \
    "${target}" \
    "${agent_name}" \
    "${experiment_id}"
done < <(jq -r '.agents[]' <<<"${selection}")
