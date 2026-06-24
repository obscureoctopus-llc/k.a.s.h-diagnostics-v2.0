#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: sudo ./create_kde_clone_image.sh <source_block_device> <image_name>

Example:
  sudo ./create_kde_clone_image.sh /dev/mmcblk0 kde-kash-arm64

Creates:
  ./images/<image_name>.img
  ./images/<image_name>.img.sha256
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -ne 2 ]]; then
  usage
  exit 1
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: Run this script with sudo/root."
  exit 1
fi

SOURCE_DEVICE="$1"
IMAGE_NAME="$2"

if [[ ! -b "${SOURCE_DEVICE}" ]]; then
  echo "ERROR: ${SOURCE_DEVICE} is not a valid block device."
  exit 1
fi

if [[ -z "${IMAGE_NAME// }" ]]; then
  echo "ERROR: image_name cannot be empty."
  exit 1
fi

mkdir -p images
OUTPUT_IMAGE="images/${IMAGE_NAME}.img"
OUTPUT_HASH="${OUTPUT_IMAGE}.sha256"

if [[ -e "${OUTPUT_IMAGE}" ]]; then
  echo "ERROR: ${OUTPUT_IMAGE} already exists. Pick a new name or remove it."
  exit 1
fi

echo "Creating disk image from ${SOURCE_DEVICE}..."
dd if="${SOURCE_DEVICE}" of="${OUTPUT_IMAGE}" bs=4M status=progress conv=fsync
sync

sha256sum "${OUTPUT_IMAGE}" > "${OUTPUT_HASH}"

echo "Done."
echo "Image: ${OUTPUT_IMAGE}"
echo "SHA256: ${OUTPUT_HASH}"
