#!/usr/bin/env bash
# test-with-usb.sh — Mount a built Pi image and chroot into it with USB passthrough.
#
# Mounts the latest .img from data/, sets up QEMU arm64 emulation, forwards
# /dev/bus/usb so the RTL-SDR dongle is accessible inside the chroot.
#
# Usage:
#   sudo ./scripts/test-with-usb.sh              # interactive shell
#   sudo ./scripts/test-with-usb.sh rtl_test -t  # run a specific command
#   sudo ./scripts/test-with-usb.sh check-sdr.sh # run a check script
#
# USB notes:
#   If you get "device busy" errors, detach the host DVB driver first:
#     sudo modprobe -r dvb_usb_rtl28xxu rtl2832 rtl2832_sdr
#
# Requires: kpartx, qemu-user-static registered with binfmt_misc

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$PROJECT_DIR/data"
MOUNT_DIR="/mnt/pi"

# --- Preflight: root check ---
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root."
    echo "  sudo $0 $*"
    exit 1
fi

# --- Preflight: required commands ---
for cmd in losetup kpartx mount chroot; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found. Install the required package."
        exit 1
    fi
done

if [[ ! -x /usr/bin/qemu-aarch64-static ]]; then
    echo "ERROR: /usr/bin/qemu-aarch64-static not found or not executable."
    echo "Install:  sudo apt install qemu-user-static"
    echo "Register: sudo docker run --rm --privileged multiarch/qemu-user-static --reset -p yes"
    exit 1
fi

# --- Find latest .img in data/ ---
IMAGE=""
if [[ -d "$DATA_DIR" ]]; then
    # Pick the most recently modified .img file
    IMAGE=$(find "$DATA_DIR" -maxdepth 1 -name "*.img" ! -name "*.img.xz" -printf '%T@ %p\n' \
        2>/dev/null | sort -rn | head -1 | awk '{print $2}') || true
fi

if [[ -z "$IMAGE" ]]; then
    # Check for compressed images and guide the user
    XZ_COUNT=$(find "$DATA_DIR" -maxdepth 1 -name "*.img.xz" 2>/dev/null | wc -l) || true
    if [[ "$XZ_COUNT" -gt 0 ]]; then
        echo "ERROR: No .img file found in $DATA_DIR"
        echo "Found .img.xz — decompress first:"
        echo "  xz --decompress --keep $DATA_DIR/*.img.xz"
    else
        echo "ERROR: No .img file found in $DATA_DIR"
        echo "Build the image first:"
        echo "  docker compose run --rm build"
        echo "  # Set COMPRESS=false to skip xz and keep a raw .img"
    fi
    exit 1
fi

echo "Image: $IMAGE"

# --- Track state for cleanup ---
LOOP_DEV=""
KPARTX_MAPPED=false
CHROOT_MOUNTS=()
QEMU_INSTALLED=false
PRELOAD_DISABLED=false
RESOLV_BACKED_UP=false

# --- Cleanup trap ---
cleanup() {
    echo ""
    echo "=== Cleanup ==="

    # Unmount bind mounts in reverse order
    for mp in "${CHROOT_MOUNTS[@]+"${CHROOT_MOUNTS[@]}"}"; do
        if mountpoint -q "$mp" 2>/dev/null; then
            echo "  umount $mp"
            umount "$mp" || true
        fi
    done

    # Restore files BEFORE unmounting root

    # Restore ld.so.preload
    if [[ "$PRELOAD_DISABLED" == true ]]; then
        local preload="${MOUNT_DIR}/etc/ld.so.preload"
        if [[ -f "$preload" ]]; then
            echo "  Restoring ld.so.preload"
            sed -i 's/^#//' "$preload" || true
        fi
    fi

    # Restore resolv.conf
    if [[ "$RESOLV_BACKED_UP" == true ]]; then
        local resolv="${MOUNT_DIR}/etc/resolv.conf"
        local resolv_bak="${MOUNT_DIR}/etc/resolv.conf.bak"
        if [[ -f "$resolv_bak" ]]; then
            echo "  Restoring resolv.conf from backup"
            mv "$resolv_bak" "$resolv" || true
        fi
    fi

    # Remove QEMU binary
    if [[ "$QEMU_INSTALLED" == true ]]; then
        echo "  Removing qemu-aarch64-static"
        rm -f "${MOUNT_DIR}/usr/bin/qemu-aarch64-static" || true
    fi

    # NOW unmount boot and root filesystems
    if mountpoint -q "${MOUNT_DIR}/boot/firmware" 2>/dev/null; then
        echo "  umount ${MOUNT_DIR}/boot/firmware"
        umount "${MOUNT_DIR}/boot/firmware" || true
    fi
    if mountpoint -q "${MOUNT_DIR}" 2>/dev/null; then
        echo "  umount ${MOUNT_DIR}"
        umount "${MOUNT_DIR}" || true
    fi

    # Tear down kpartx and loop device
    if [[ "$KPARTX_MAPPED" == true && -n "$LOOP_DEV" ]]; then
        echo "  kpartx -dv $LOOP_DEV"
        kpartx -dv "$LOOP_DEV" || true
        KPARTX_MAPPED=false
    fi
    if [[ -n "$LOOP_DEV" ]]; then
        echo "  losetup -d $LOOP_DEV"
        losetup -d "$LOOP_DEV" || true
        LOOP_DEV=""
    fi

    echo "  Done."
}
trap cleanup EXIT

# --- Loop device setup ---
echo ""
echo "=== Loop mount ==="
mkdir -p "$MOUNT_DIR"
LOOP_DEV=$(losetup --find --show "$IMAGE")
echo "Loop device: $LOOP_DEV"

# --- Create partition mappings ---
echo ""
echo "=== Create partition mappings (kpartx) ==="
kpartx -av "$LOOP_DEV"
KPARTX_MAPPED=true
sleep 1

LOOP_NAME=$(basename "$LOOP_DEV")
PART1="/dev/mapper/${LOOP_NAME}p1"
PART2="/dev/mapper/${LOOP_NAME}p2"

if [[ ! -b "$PART2" ]]; then
    echo "ERROR: $PART2 not found after kpartx. Cannot continue."
    exit 1
fi
echo "Partitions: $PART1 (boot)  $PART2 (root)"

# --- Mount filesystems ---
echo ""
echo "=== Mount filesystems ==="
mount "$PART2" "$MOUNT_DIR"
mount "$PART1" "${MOUNT_DIR}/boot/firmware"

# --- Disable ld.so.preload ---
echo ""
echo "=== Disable ld.so.preload ==="
if [[ -f "${MOUNT_DIR}/etc/ld.so.preload" ]]; then
    sed -i 's/^/#/' "${MOUNT_DIR}/etc/ld.so.preload"
    PRELOAD_DISABLED=true
    echo "  Commented out."
else
    echo "  Not present — skipping."
fi

# --- Copy QEMU binary into chroot ---
echo ""
echo "=== Install qemu-aarch64-static ==="
cp /usr/bin/qemu-aarch64-static "${MOUNT_DIR}/usr/bin/qemu-aarch64-static"
QEMU_INSTALLED=true

# --- Bind-mount USB and virtual filesystems ---
echo ""
echo "=== Bind-mount /dev/bus/usb /proc /sys /dev /dev/pts ==="
# Track in reverse order for cleanup (last mounted = first unmounted)
mount --bind /proc "${MOUNT_DIR}/proc"
CHROOT_MOUNTS=("${MOUNT_DIR}/dev/pts" "${MOUNT_DIR}/dev" "${MOUNT_DIR}/sys" "${MOUNT_DIR}/proc")
mount --bind /sys "${MOUNT_DIR}/sys"
mount --bind /dev "${MOUNT_DIR}/dev"
mount --bind /dev/pts "${MOUNT_DIR}/dev/pts"

# USB passthrough — create mount point in chroot if needed
if [[ -d /dev/bus/usb ]]; then
    mkdir -p "${MOUNT_DIR}/dev/bus/usb"
    mount --bind /dev/bus/usb "${MOUNT_DIR}/dev/bus/usb"
    # Prepend so it's unmounted first
    CHROOT_MOUNTS=("${MOUNT_DIR}/dev/bus/usb" "${CHROOT_MOUNTS[@]}")
    echo "  USB passthrough: /dev/bus/usb"
else
    echo "  WARNING: /dev/bus/usb not found on host — no USB passthrough"
fi

# --- Copy resolv.conf ---
echo ""
echo "=== Copy resolv.conf ==="
if [[ -e "${MOUNT_DIR}/etc/resolv.conf" ]]; then
    cp "${MOUNT_DIR}/etc/resolv.conf" "${MOUNT_DIR}/etc/resolv.conf.bak" || true
    RESOLV_BACKED_UP=true
fi
if [[ -L "${MOUNT_DIR}/etc/resolv.conf" ]]; then
    rm "${MOUNT_DIR}/etc/resolv.conf"
fi
cp /etc/resolv.conf "${MOUNT_DIR}/etc/resolv.conf"

# --- Enter chroot ---
echo ""
echo "=== Entering Pi image chroot ==="
echo "RTL-SDR USB devices are available. Try:"
echo "  rtl_test -t"
echo "  SoapySDRUtil --find"
echo ""

if [[ $# -gt 0 ]]; then
    CHROOT_CMD=("$@")
    chroot "${MOUNT_DIR}" env -i \
        HOME=/root \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        TERM="${TERM:-xterm}" \
        "${CHROOT_CMD[@]}"
else
    chroot "${MOUNT_DIR}" env -i \
        HOME=/root \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        TERM="${TERM:-xterm}" \
        bash --login
fi
