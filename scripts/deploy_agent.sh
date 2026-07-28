#!/usr/bin/env bash
set -euo pipefail

target="${1:?Usage: deploy_agent.sh <dev|prod> <agent-name> <experiment-id>}"
agent_name="${2:?Usage: deploy_agent.sh <dev|prod> <agent-name> <experiment-id>}"
experiment_id="${3:?Usage: deploy_agent.sh <dev|prod> <agent-name> <experiment-id>}"
python_bin="${PYTHON_BIN:-python}"
bundle_dir=".generated/bundles/${agent_name}"

if [[ "${target}" != "dev" && "${target}" != "prod" ]]; then
  echo "Target must be dev or prod." >&2
  exit 1
fi
if [[ ! -f "${bundle_dir}/databricks.yml" ]]; then
  echo "Unknown generated agent: ${agent_name}" >&2
  exit 1
fi

bundle_args=(
  -t "${target}"
  --var "experiment_id=${experiment_id}"
  --var "catalog=${UC_CATALOG}"
  --var "schema=${UC_SCHEMA}"
  --var "trace_table_prefix=${UC_TRACE_TABLE_PREFIX}"
)
resource_key="$(
  jq -er --arg name "${agent_name}" \
    '.agents[] | select(.name == $name) | .resource_key' \
    .generated/agent-index.json
)"
app_name="${target}-agent-${agent_name}"

scripts/migrate_app_bundle.sh \
  "${target}" \
  "${bundle_dir}" \
  "${resource_key}" \
  "${app_name}" \
  "${bundle_args[@]}"

printf 'Validating isolated %s bundle for %s\n' "${target}" "${agent_name}"
(
  cd "${bundle_dir}"
  databricks bundle validate "${bundle_args[@]}"
)

printf 'Deploying only %s to %s\n' "${agent_name}" "${target}"
(
  cd "${bundle_dir}"
  databricks bundle deploy "${bundle_args[@]}" --auto-approve --force-lock
  databricks bundle run "${resource_key}" "${bundle_args[@]}"
)

printf 'Registering only %s in Unity AI Gateway\n' "${agent_name}"
"${python_bin}" scripts/register_uc_agent.py \
  --target "${target}" \
  --catalog "${UC_CATALOG}" \
  --schema "${UC_SCHEMA}" \
  --agent "${agent_name}"

printf 'Smoke testing only %s\n' "${agent_name}"
"${python_bin}" scripts/smoke_agent.py \
  --target "${target}" \
  --agent "${agent_name}" \
  --warehouse-id "${DATABRICKS_WAREHOUSE_ID}"
