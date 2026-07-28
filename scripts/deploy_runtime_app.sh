#!/usr/bin/env bash
set -euo pipefail

target="${1:?Usage: deploy_runtime_app.sh <dev|prod> <langchain|mcp|omnigent> <experiment-id>}"
component="${2:?Usage: deploy_runtime_app.sh <dev|prod> <langchain|mcp|omnigent> <experiment-id>}"
experiment_id="${3:?Usage: deploy_runtime_app.sh <dev|prod> <langchain|mcp|omnigent> <experiment-id>}"
python_bin="${PYTHON_BIN:-python}"

case "${component}" in
  langchain)
    bundle_dir="src/langchain_agent"
    resource_key="langchain_agent"
    bundle_args=(
      -t "${target}"
      --var "experiment_id=${experiment_id}"
      --var "catalog=${UC_CATALOG}"
      --var "schema=${UC_SCHEMA}"
      --var "trace_table_prefix=${UC_TRACE_TABLE_PREFIX}"
      --var "uc_function_name=${UC_COST_FUNCTION}"
      --var "uc_time_function_name=${UC_TIME_FUNCTION}"
    )
    ;;
  mcp)
    bundle_dir="src/mcp_server"
    resource_key="mcp_server"
    bundle_args=(-t "${target}")
    ;;
  omnigent)
    bundle_dir="src/omnigent_app"
    resource_key="omnigent"
    bundle_args=(-t "${target}")
    ;;
  *)
    echo "App must be langchain, mcp, or omnigent." >&2
    exit 1
    ;;
esac

app_name="$(
  cd "${bundle_dir}"
  databricks bundle summary "${bundle_args[@]}" -o json |
    jq -er --arg key "${resource_key}" '.resources.apps[$key].name'
)"
if [[ "${component}" == "mcp" && "${app_name}" != mcp-* ]]; then
  echo "Custom MCP App name must start with mcp-: ${app_name}" >&2
  exit 1
fi

scripts/migrate_app_bundle.sh \
  "${target}" \
  "${bundle_dir}" \
  "${resource_key}" \
  "${app_name}" \
  "${bundle_args[@]}"

printf 'Validating isolated %s bundle for %s\n' "${target}" "${component}"
(
  cd "${bundle_dir}"
  databricks bundle validate "${bundle_args[@]}"
)

printf 'Deploying only %s to %s\n' "${component}" "${target}"
(
  cd "${bundle_dir}"
  databricks bundle deploy "${bundle_args[@]}" --auto-approve --force-lock
  databricks bundle run "${resource_key}" "${bundle_args[@]}"
)

if [[ "${component}" == "langchain" || "${component}" == "omnigent" ]]; then
  printf 'Registering only the %s App in Unity AI Gateway\n' "${component}"
  "${python_bin}" scripts/register_uc_agent.py \
    --target "${target}" \
    --catalog "${UC_CATALOG}" \
    --schema "${UC_SCHEMA}" \
    --runtime-agent "${component}"
fi

printf 'Smoke testing only %s\n' "${component}"
"${python_bin}" scripts/smoke_runtime_app.py \
  --target "${target}" \
  --app "${component}" \
  --warehouse-id "${DATABRICKS_WAREHOUSE_ID}"
