#!/usr/bin/env bash
set -euo pipefail

replacement_app="${1:?Usage: retire_replaced_app.sh <replacement-app> <legacy-app>}"
legacy_app="${2:?Usage: retire_replaced_app.sh <replacement-app> <legacy-app>}"

replacement_json="$(
  databricks apps get "${replacement_app}" -o json |
    jq -e .
)"
replacement_name="$(jq -er '.name' <<<"${replacement_json}")"
if [[ "${replacement_name}" != "${replacement_app}" ]]; then
  echo "Replacement App lookup returned ${replacement_name}, expected ${replacement_app}." >&2
  exit 1
fi
replacement_app_state="$(jq -r '.app_status.state // empty' <<<"${replacement_json}")"
replacement_compute_state="$(
  jq -r '.compute_status.state // empty' <<<"${replacement_json}"
)"
replacement_deployment_state="$(
  jq -r '.active_deployment.status.state // empty' <<<"${replacement_json}"
)"
if [[ "${replacement_app_state}" != "RUNNING" ]] ||
  [[ "${replacement_compute_state}" != "ACTIVE" ]] ||
  [[ "${replacement_deployment_state}" != "SUCCEEDED" ]]; then
  echo "Replacement App ${replacement_app} is not fully running." >&2
  exit 1
fi

legacy_output=""
if legacy_output="$(databricks apps get "${legacy_app}" -o json 2>&1)"; then
  legacy_name="$(jq -er '.name' <<<"${legacy_output}")"
  if [[ "${legacy_name}" != "${legacy_app}" ]]; then
    echo "Legacy App lookup returned ${legacy_name}, expected ${legacy_app}." >&2
    exit 1
  fi
  printf 'Retiring replaced App %s\n' "${legacy_app}"
  databricks apps delete "${legacy_app}" --auto-approve
elif [[ "${legacy_output}" != *"does not exist or is deleted."* ]]; then
  printf '%s\n' "${legacy_output}" >&2
  exit 1
fi
