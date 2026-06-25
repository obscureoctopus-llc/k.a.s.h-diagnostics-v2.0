#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

readonly SCRIPT_NAME="$(basename "$0")"
readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly APP_BRAND="k.a.s.h-diagnostics-v3"
readonly APP_BRAND_ISO="${APP_BRAND}.iso"
readonly DEFAULT_OUTPUT_DIR="${PROJECT_ROOT}/output"
readonly DEFAULT_WORK_BASE="${PROJECT_ROOT}/.build"
readonly DEFAULT_CODENAME="noble"
readonly DEFAULT_ARCH="amd64"
readonly DEFAULT_MIRROR="http://archive.ubuntu.com/ubuntu"

LOG_TS_FORMAT='+%Y-%m-%dT%H:%M:%SZ'
WORK_DIR=""
CHROOT_DIR=""
IMAGE_DIR=""
OUTPUT_DIR="${DEFAULT_OUTPUT_DIR}"
CODENAME="${DEFAULT_CODENAME}"
ARCH="${DEFAULT_ARCH}"
MIRROR="${DEFAULT_MIRROR}"
PRECHECK_ONLY=0
FORCE=0

readonly REQUIRED_CMDS=(
  debootstrap
  mksquashfs
  xorriso
  grub-mkstandalone
  mkfs.vfat
  mcopy
  mmd
  chroot
  mount
  umount
  rsync
  sha256sum
)

log() {
  local level="$1"
  local message="$2"
  printf '%s level=%s script=%s msg=%q\n' "$(date -u "$LOG_TS_FORMAT")" "$level" "$SCRIPT_NAME" "$message"
}

die() {
  log "ERROR" "$1"
  exit 1
}

usage() {
  cat <<USAGE
Usage: sudo ${SCRIPT_NAME} [options]

Builds a bootable BIOS+UEFI ISO artifact branded as ${APP_BRAND_ISO}.

Options:
  --output-dir <path>     Output directory (default: ${DEFAULT_OUTPUT_DIR})
  --work-base <path>      Working directory base (default: ${DEFAULT_WORK_BASE})
  --codename <name>       Ubuntu codename (default: ${DEFAULT_CODENAME})
  --arch <arch>           Architecture (default: ${DEFAULT_ARCH})
  --mirror <url>          Ubuntu/Debian package mirror (default: ${DEFAULT_MIRROR})
  --precheck-only         Validate environment/inputs and exit
  --force                 Overwrite existing ISO output
  -h, --help              Show this help
USAGE
}

cleanup_mount() {
  local mount_path="$1"
  if mountpoint -q "$mount_path"; then
    umount "$mount_path"
  fi
}

cleanup() {
  local exit_code=$?
  set +e

  if [[ -n "${CHROOT_DIR}" && -d "${CHROOT_DIR}" ]]; then
    cleanup_mount "${CHROOT_DIR}/run"
    cleanup_mount "${CHROOT_DIR}/dev/pts"
    cleanup_mount "${CHROOT_DIR}/dev"
    cleanup_mount "${CHROOT_DIR}/proc"
    cleanup_mount "${CHROOT_DIR}/sys"
  fi

  if [[ -n "${WORK_DIR}" && -d "${WORK_DIR}" ]]; then
    rm -rf "${WORK_DIR}"
  fi

  if [[ ${exit_code} -ne 0 ]]; then
    log "ERROR" "ISO build failed"
  else
    log "INFO" "ISO build completed"
  fi

  exit "${exit_code}"
}
trap cleanup EXIT

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "This script must run as root. Use sudo ${SCRIPT_NAME}."
  fi
}

validate_mirror() {
  case "${MIRROR}" in
    http://archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|http://security.ubuntu.com/ubuntu|https://security.ubuntu.com/ubuntu|http://deb.debian.org/debian|https://deb.debian.org/debian)
      return 0
      ;;
    *)
      die "Unsupported mirror '${MIRROR}'. Use a trusted Ubuntu/Debian mirror."
      ;;
  esac
}

validate_arch() {
  case "${ARCH}" in
    amd64)
      return 0
      ;;
    *)
      die "Unsupported architecture '${ARCH}'. Supported: amd64."
      ;;
  esac
}

check_requirements() {
  local missing=()
  local cmd
  for cmd in "${REQUIRED_CMDS[@]}"; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
      missing+=("${cmd}")
    fi
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    die "Missing required command(s): ${missing[*]}. Install dependencies before running."
  fi

  if [[ ! -f /usr/lib/grub/i386-pc/cdboot.img ]]; then
    die "Missing /usr/lib/grub/i386-pc/cdboot.img. Install grub-pc-bin."
  fi
}

prepare_dirs() {
  mkdir -p "${OUTPUT_DIR}" "${WORK_BASE}"
  WORK_DIR="$(mktemp -d "${WORK_BASE%/}/iso-build.XXXXXX")"
  readonly WORK_DIR
  CHROOT_DIR="${WORK_DIR}/chroot"
  IMAGE_DIR="${WORK_DIR}/image"
  mkdir -p "${CHROOT_DIR}" "${IMAGE_DIR}/live" "${IMAGE_DIR}/boot/grub" "${IMAGE_DIR}/EFI"
}

mount_chroot_fs() {
  mount --bind /dev "${CHROOT_DIR}/dev"
  mount --bind /dev/pts "${CHROOT_DIR}/dev/pts"
  mount -t proc proc "${CHROOT_DIR}/proc"
  mount -t sysfs sysfs "${CHROOT_DIR}/sys"
  mount -t tmpfs tmpfs "${CHROOT_DIR}/run"
}

write_sources_list() {
  cat >"${CHROOT_DIR}/etc/apt/sources.list" <<APT

deb ${MIRROR} ${CODENAME} main universe multiverse restricted
deb ${MIRROR} ${CODENAME}-updates main universe multiverse restricted
deb http://security.ubuntu.com/ubuntu ${CODENAME}-security main universe multiverse restricted
APT
}

bootstrap_base_system() {
  log "INFO" "Bootstrapping base system"
  debootstrap --arch="${ARCH}" --variant=minbase "${CODENAME}" "${CHROOT_DIR}" "${MIRROR}"
}

provision_chroot() {
  log "INFO" "Provisioning chroot"
  cp /etc/resolv.conf "${CHROOT_DIR}/etc/resolv.conf"
  write_sources_list

  cat >"${CHROOT_DIR}/tmp/provision.sh" <<'PROVISION'
#!/usr/bin/env bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  linux-image-generic \
  initramfs-tools \
  systemd-sysv \
  live-boot \
  casper \
  python3 \
  python3-pip \
  ca-certificates

rm -rf /var/lib/apt/lists/*
PROVISION
  chmod 700 "${CHROOT_DIR}/tmp/provision.sh"
  chroot "${CHROOT_DIR}" /tmp/provision.sh
  rm -f "${CHROOT_DIR}/tmp/provision.sh"

  mkdir -p "${CHROOT_DIR}/opt/kash-diagnostics"
  rsync -a --delete \
    --exclude '.git' \
    --exclude '.github' \
    --exclude '.build' \
    --exclude 'output' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    "${PROJECT_ROOT}/" "${CHROOT_DIR}/opt/kash-diagnostics/"

  chroot "${CHROOT_DIR}" python3 -m pip install --no-cache-dir -r /opt/kash-diagnostics/requirements.txt

  cat >"${CHROOT_DIR}/usr/local/bin/kash-diagnostics-launcher" <<'LAUNCHER'
#!/usr/bin/env bash
set -Eeuo pipefail
cd /opt/kash-diagnostics
exec python3 kash_diagnostics.py
LAUNCHER
  chmod 755 "${CHROOT_DIR}/usr/local/bin/kash-diagnostics-launcher"

  cat >"${CHROOT_DIR}/etc/systemd/system/kash-diagnostics.service" <<'SERVICE'
[Unit]
Description=K.A.S.H Diagnostics Web Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/kash-diagnostics-launcher
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

  chroot "${CHROOT_DIR}" systemctl enable kash-diagnostics.service
  chroot "${CHROOT_DIR}" update-initramfs -c -k all
}

copy_live_boot_assets() {
  local kernel
  local initrd

  kernel="$(ls -1 "${CHROOT_DIR}/boot/vmlinuz-"* 2>/dev/null | sort | tail -n1 || true)"
  initrd="$(ls -1 "${CHROOT_DIR}/boot/initrd.img-"* 2>/dev/null | sort | tail -n1 || true)"

  [[ -n "${kernel}" ]] || die "No kernel found in chroot /boot."
  [[ -n "${initrd}" ]] || die "No initrd found in chroot /boot."

  cp "${kernel}" "${IMAGE_DIR}/live/vmlinuz"
  cp "${initrd}" "${IMAGE_DIR}/live/initrd"

  mksquashfs "${CHROOT_DIR}" "${IMAGE_DIR}/live/filesystem.squashfs" -e boot
}

write_grub_cfg() {
  cat >"${IMAGE_DIR}/boot/grub/grub.cfg" <<'GRUBCFG'
search --set=root --file /live/vmlinuz
set default=0
set timeout=5

menuentry "K.A.S.H Diagnostics v3 (Live)" {
    linux /live/vmlinuz boot=casper quiet splash ---
    initrd /live/initrd
}
GRUBCFG
}

build_bios_boot_image() {
  log "INFO" "Building BIOS boot image"
  grub-mkstandalone \
    -O i386-pc \
    -o "${WORK_DIR}/core.img" \
    --install-modules="linux normal iso9660 biosdisk memdisk search" \
    --modules="linux normal iso9660 biosdisk search" \
    --locales='' \
    --fonts='' \
    "boot/grub/grub.cfg=${IMAGE_DIR}/boot/grub/grub.cfg"

  cat /usr/lib/grub/i386-pc/cdboot.img "${WORK_DIR}/core.img" > "${IMAGE_DIR}/boot/grub/bios.img"
}

build_uefi_boot_image() {
  log "INFO" "Building UEFI boot image"
  grub-mkstandalone \
    -O x86_64-efi \
    -o "${WORK_DIR}/bootx64.efi" \
    --install-modules="linux normal iso9660 search" \
    --modules="linux normal iso9660 search" \
    --locales='' \
    --fonts='' \
    "boot/grub/grub.cfg=${IMAGE_DIR}/boot/grub/grub.cfg"

  dd if=/dev/zero of="${WORK_DIR}/efiboot.img" bs=1M count=20 status=none
  mkfs.vfat "${WORK_DIR}/efiboot.img" >/dev/null
  mmd -i "${WORK_DIR}/efiboot.img" ::EFI ::EFI/BOOT
  mcopy -i "${WORK_DIR}/efiboot.img" "${WORK_DIR}/bootx64.efi" ::EFI/BOOT/BOOTX64.EFI

  cp "${WORK_DIR}/efiboot.img" "${IMAGE_DIR}/EFI/efiboot.img"
}

build_iso() {
  local iso_path="${OUTPUT_DIR}/${APP_BRAND_ISO}"
  local checksum_path="${iso_path}.sha256"

  if [[ -e "${iso_path}" && "${FORCE}" -ne 1 ]]; then
    die "Output ISO already exists at ${iso_path}. Re-run with --force to overwrite."
  fi

  log "INFO" "Generating ISO ${iso_path}"
  xorriso -as mkisofs \
    -iso-level 3 \
    -full-iso9660-filenames \
    -volid "KASH_DIAG_V3" \
    -eltorito-boot boot/grub/bios.img \
      -no-emul-boot \
      -boot-load-size 4 \
      -boot-info-table \
    -eltorito-alt-boot \
    -e EFI/efiboot.img \
      -no-emul-boot \
    -isohybrid-gpt-basdat \
    -output "${iso_path}" \
    "${IMAGE_DIR}"

  sha256sum "${iso_path}" > "${checksum_path}"
  log "INFO" "ISO artifact ready: ${iso_path}"
  log "INFO" "Checksum ready: ${checksum_path}"
}

parse_args() {
  WORK_BASE="${DEFAULT_WORK_BASE}"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --output-dir)
        OUTPUT_DIR="$2"
        shift 2
        ;;
      --work-base)
        WORK_BASE="$2"
        shift 2
        ;;
      --codename)
        CODENAME="$2"
        shift 2
        ;;
      --arch)
        ARCH="$2"
        shift 2
        ;;
      --mirror)
        MIRROR="$2"
        shift 2
        ;;
      --precheck-only)
        PRECHECK_ONLY=1
        shift
        ;;
      --force)
        FORCE=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
  done
}

main() {
  parse_args "$@"
  validate_arch
  validate_mirror
  check_requirements

  if [[ "${PRECHECK_ONLY}" -eq 1 ]]; then
    log "INFO" "Precheck complete"
    return 0
  fi

  require_root

  prepare_dirs
  bootstrap_base_system
  mount_chroot_fs
  provision_chroot
  copy_live_boot_assets
  write_grub_cfg
  build_bios_boot_image
  build_uefi_boot_image
  build_iso
}

main "$@"
