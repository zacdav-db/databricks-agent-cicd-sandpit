#!/usr/bin/env bash
set -euo pipefail

target="${1:?Usage: deploy_target.sh <dev|prod> <base-sha> <head-sha>}"
base_sha="${2:?Usage: deploy_target.sh <dev|prod> <base-sha> <head-sha>}"
head_sha="${3:?Usage: deploy_target.sh <dev|prod> <base-sha> <head-sha>}"
python_bin="${PYTHON_BIN:-python}"
export PYTHON_BIN="${python_bin}"

databricks_app_exists() {
  local app_name="$1"
  local output
  if output="$(databricks apps get "${app_name}" -o json 2>&1)"; then
    return 0
  fi
  if [[ "${output}" == *"does not exist or is deleted."* ]]; then
    return 1
  fi
  printf '%s\n' "${output}" >&2
  return 2
}

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

if [[ "$(jq -r '.apps | length' <<<"${selection}")" -gt 0 ]] &&
  ! jq -e '.apps | index("omnigent") != null' <<<"${selection}" >/dev/null; then
  printf 'Smoke testing the unchanged downstream runtime consumers\n'
  "${python_bin}" scripts/smoke_runtime_app.py \
    --target "${target}" \
    --app topology \
    --warehouse-id "${DATABRICKS_WAREHOUSE_ID}"
fi

if jq -e '.apps | index("mcp") != null' <<<"${selection}" >/dev/null; then
  replacement_app="mcp-${target}-sandpit-tools"
  legacy_app="${target}-sandpit-mcp-tools"
  if databricks_app_exists "${legacy_app}"; then
    databricks apps get "${replacement_app}" -o json >/dev/null
    printf 'Retiring replaced MCP App %s\n' "${legacy_app}"
    databricks apps delete "${legacy_app}" --auto-approve
  else
    app_lookup_status=$?
    if [[ "${app_lookup_status}" -ne 1 ]]; then
      exit "${app_lookup_status}"
    fi
  fi
fi
