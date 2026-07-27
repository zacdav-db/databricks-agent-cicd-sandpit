#!/usr/bin/env bash
set -euo pipefail

profile="${DATABRICKS_CONFIG_PROFILE:-sandpit}"
target="${1:-dev}"

bootstrap_json="$(
  python scripts/bootstrap_resources.py \
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

python scripts/smoke_test.py \
  --profile "${profile}" \
  --target "${target}"
