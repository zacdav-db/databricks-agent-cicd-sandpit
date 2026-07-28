#!/usr/bin/env bash
set -euo pipefail

target="${1:?Usage: migrate_app_bundle.sh <target> <bundle-dir> <resource-key> <app-name> [bundle args...]}"
bundle_dir="${2:?Usage: migrate_app_bundle.sh <target> <bundle-dir> <resource-key> <app-name> [bundle args...]}"
resource_key="${3:?Usage: migrate_app_bundle.sh <target> <bundle-dir> <resource-key> <app-name> [bundle args...]}"
app_name="${4:?Usage: migrate_app_bundle.sh <target> <bundle-dir> <resource-key> <app-name> [bundle args...]}"
shift 4
legacy_dir=".generated/legacy-shared-bundle"
bundle_args=("$@")
if [[ "${#bundle_args[@]}" -lt 2 ]]; then
  echo "Bundle arguments must include -t <target>." >&2
  exit 1
fi

legacy_id="$(
  cd "${legacy_dir}"
  databricks bundle summary -t "${target}" -o json |
    jq -r --arg key "${resource_key}" '.resources.apps[$key].id // empty'
)"
isolated_id="$(
  cd "${bundle_dir}"
  databricks bundle summary "${bundle_args[@]}" -o json |
    jq -r --arg key "${resource_key}" '.resources.apps[$key].id // empty'
)"

if [[ -n "${legacy_id}" ]]; then
  printf 'Unbinding %s from the former shared bundle state\n' "${app_name}"
  (
    cd "${legacy_dir}"
    databricks bundle deployment unbind \
      "${resource_key}" \
      -t "${target}" \
      --force-lock
  )
fi

existing_app_name="$(
  databricks apps get "${app_name}" -o json 2>/dev/null |
    jq -r '.name // empty' || true
)"
if [[ -z "${isolated_id}" && -n "${existing_app_name}" ]]; then
  printf 'Binding existing %s to its isolated bundle state\n' "${app_name}"
  (
    cd "${bundle_dir}"
    databricks bundle deployment bind \
      "${resource_key}" \
      "${existing_app_name}" \
      "${bundle_args[@]}" \
      --auto-approve \
      --force-lock
  )
fi
