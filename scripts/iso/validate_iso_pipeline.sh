#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

readonly ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly BUILD_SCRIPT="${ROOT_DIR}/scripts/iso/build_iso.sh"

if [[ ! -x "${BUILD_SCRIPT}" ]]; then
  echo "ERROR: Build script missing or not executable: ${BUILD_SCRIPT}" >&2
  exit 1
fi

bash -n "${BUILD_SCRIPT}"
"${BUILD_SCRIPT}" --precheck-only

echo "ISO pipeline validation checks passed."
