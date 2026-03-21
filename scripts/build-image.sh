#!/usr/bin/env bash
# build-image.sh — Raspberry Pi OS Trixie arm64 lite image builder
#
# Downloads a pinned Pi OS image, resizes it, chroots via QEMU to run
# provision.sh, then compresses the result. Runs inside a privileged
# Docker container (Ubuntu 24.04) — no sudo needed on the host.
#
# Usage:
#   docker compose run --rm build
#   COMPRESS=false docker compose run --rm build   # skip xz for dev cycles
#   DEBUG=1 docker compose run --rm build          # verbose trace
#
# Env vars (all optional):
#   TAILSCALE_AUTHKEY   — passed into chroot for Tailscale auth
#   WIFI_SSID           — WiFi network name
#   WIFI_PASSWORD       — WiFi passphrase
#   WIFI_COUNTRY        — ISO 3166-1 alpha-2 (default: US)
#   COMPRESS            — set to "false" to skip xz compression
#   DEBUG               — set to "1" to enable set -x

set -euo pipefail
[[ "${DEBUG:-}" == "1" ]] && set -x

# --- Paths ---
IMAGE_DATE="2025-12-04"
IMAGE_NAME="${IMAGE_DATE}-raspios-trixie-arm64-lite"
IMAGE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-${IMAGE_DATE}/${IMAGE_NAME}.img.xz"

EXPAND_GB=5
COMPRESS="${COMPRESS:-true}"

BUILD_DIR="/build"
DATA_DIR="${BUILD_DIR}/data"
IMAGE_XZ="${DATA_DIR}/${IMAGE_NAME}.img.xz"
IMAGE_WORK="${DATA_DIR}/${IMAGE_NAME}-work.img"
IMAGE_OUT="${DATA_DIR}/pi-sdr-base.img"

MOUNT_DIR="/mnt/pi"

# Optional env vars
TAILSCALE_AUTHKEY="${TAILSCALE_AUTHKEY:-}"
WIFI_SSID="${WIFI_SSID:-}"
WIFI_PASSWORD="${WIFI_PASSWORD:-}"
WIFI_COUNTRY="${WIFI_COUNTRY:-US}"
OPENWEBRX_ADMIN_PASSWORD="${OPENWEBRX_ADMIN_PASSWORD:-}"

# Track state for cleanup
LOOP_DEV=""
KPARTX_MAPPED=false
CHROOT_MOUNTS=()

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
    local preload="${MOUNT_DIR}/etc/ld.so.preload"
    if [[ -f "$preload" ]]; then
        echo "  Restoring ld.so.preload"
        sed -i 's/^#//' "$preload" || true
    fi

    # Restore resolv.conf
    local resolv="${MOUNT_DIR}/etc/resolv.conf"
    local resolv_bak="${MOUNT_DIR}/etc/resolv.conf.bak"
    if [[ -f "$resolv_bak" ]]; then
        echo "  Restoring resolv.conf from backup"
        mv "$resolv_bak" "$resolv" || true
    elif [[ -e "$resolv" && ! -L "$resolv" ]]; then
        echo "  Restoring resolv.conf symlink"
        rm -f "$resolv" || true
        ln -s /run/systemd/resolve/stub-resolv.conf "$resolv" 2>/dev/null || true
    fi

    # Remove provision assets and QEMU binary
    rm -rf "${MOUNT_DIR}/opt/provision" || true
    rm -f "${MOUNT_DIR}/usr/bin/qemu-aarch64-static" || true

    # NOW unmount boot and root
    if mountpoint -q "${MOUNT_DIR}/boot/firmware" 2>/dev/null; then
        umount "${MOUNT_DIR}/boot/firmware" || true
    fi
    if mountpoint -q "${MOUNT_DIR}" 2>/dev/null; then
        umount "${MOUNT_DIR}" || true
    fi

    # Tear down kpartx and loop device
    if [[ "$KPARTX_MAPPED" == true && -n "$LOOP_DEV" ]]; then
        kpartx -dv "$LOOP_DEV" || true
        KPARTX_MAPPED=false
    fi
    if [[ -n "$LOOP_DEV" ]]; then
        losetup -d "$LOOP_DEV" || true
        LOOP_DEV=""
    fi

    echo "  Done."
}
trap cleanup EXIT

# --- Preflight checks ---
echo "=== Preflight checks ==="

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo or privileged container)."
    exit 1
fi

for cmd in wget xz parted e2fsck resize2fs losetup kpartx; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found. Install the required package."
        exit 1
    fi
done

if [[ ! -x /usr/bin/qemu-aarch64-static ]]; then
    echo "ERROR: /usr/bin/qemu-aarch64-static not found or not executable."
    echo "Install: sudo apt install qemu-user-static"
    echo "Register: docker run --rm --privileged multiarch/qemu-user-static --reset -p yes"
    exit 1
fi

# --- Prepare data directory ---
mkdir -p "$DATA_DIR"
mkdir -p "$MOUNT_DIR"

# --- Check for existing output ---
if [[ -f "$IMAGE_OUT" ]]; then
    echo "ERROR: Output image already exists: $IMAGE_OUT"
    echo "Remove it to rebuild: rm $IMAGE_OUT"
    exit 1
fi

# --- Download image ---
echo ""
echo "=== Download ==="
if [[ ! -f "$IMAGE_XZ" ]]; then
    echo "Downloading ${IMAGE_NAME}.img.xz ..."
    wget --show-progress -O "$IMAGE_XZ" "$IMAGE_URL"
else
    echo "Cached: $IMAGE_XZ"
fi

# --- Copy to working file, decompress ---
echo ""
echo "=== Decompress ==="
# Clean up any leftover work files from previous failed builds
rm -f "$IMAGE_WORK" "$IMAGE_WORK.xz"
echo "Copying to work image..."
cp "$IMAGE_XZ" "$IMAGE_WORK.xz"
echo "Decompressing..."
xz --decompress "$IMAGE_WORK.xz"
# xz removes the .xz suffix in-place; result is $IMAGE_WORK

# --- Expand image ---
echo ""
echo "=== Expand image +${EXPAND_GB}G ==="
truncate -s "+${EXPAND_GB}G" "$IMAGE_WORK"

# --- Loop device setup ---
echo ""
echo "=== Loop mount ==="
LOOP_DEV=$(losetup --find --show "$IMAGE_WORK")
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

# --- Expand partition 2 and filesystem ---
echo ""
echo "=== Expand partition 2 to 100% ==="
# Must fsck before resize
e2fsck -f -y "$PART2"
parted -s "$LOOP_DEV" resizepart 2 100%
# Refresh partition table in device mapper after resize
kpartx -u "$LOOP_DEV"
resize2fs "$PART2"

# --- Mount filesystems ---
echo ""
echo "=== Mount filesystems ==="
mount "$PART2" "$MOUNT_DIR"
mount "$PART1" "${MOUNT_DIR}/boot/firmware"

# --- Copy provision files ---
echo ""
echo "=== Copy provision assets ==="
mkdir -p "${MOUNT_DIR}/opt/provision"
cp "${BUILD_DIR}/scripts/provision.sh" "${MOUNT_DIR}/opt/provision/provision.sh"
chmod +x "${MOUNT_DIR}/opt/provision/provision.sh"

if [[ -d "${BUILD_DIR}/config" ]]; then
    cp -r "${BUILD_DIR}/config/" "${MOUNT_DIR}/opt/provision/config/"
fi

if [[ -d "${BUILD_DIR}/test-scripts" ]]; then
    cp -r "${BUILD_DIR}/test-scripts/" "${MOUNT_DIR}/opt/provision/test-scripts/"
fi

# --- Pre-pull OpenWebRX+ Docker image for offline first boot ---
echo ""
echo "=== Pre-pull OpenWebRX+ Docker image (arm64) ==="
OWRX_IMAGE="slechev/openwebrxplus-softmbe:latest"
mkdir -p "${MOUNT_DIR}/opt/openwebrx"
docker pull --platform linux/arm64 "$OWRX_IMAGE"
echo "Saving image to ${MOUNT_DIR}/opt/openwebrx/openwebrxplus-softmbe-arm64.tar ..."
docker save "$OWRX_IMAGE" > "${MOUNT_DIR}/opt/openwebrx/openwebrxplus-softmbe-arm64.tar"
echo "Saved ($(du -sh "${MOUNT_DIR}/opt/openwebrx/openwebrxplus-softmbe-arm64.tar" | cut -f1))"

# --- Disable ld.so.preload ---
echo ""
echo "=== Disable ld.so.preload ==="
if [[ -f "${MOUNT_DIR}/etc/ld.so.preload" ]]; then
    sed -i 's/^/#/' "${MOUNT_DIR}/etc/ld.so.preload"
    echo "  Commented out."
else
    echo "  Not present — skipping."
fi

# --- Copy QEMU binary into chroot ---
echo ""
echo "=== Install qemu-aarch64-static ==="
cp /usr/bin/qemu-aarch64-static "${MOUNT_DIR}/usr/bin/qemu-aarch64-static"

# --- Bind-mount virtual filesystems ---
echo ""
echo "=== Bind-mount /proc /sys /dev /dev/pts ==="
mount --bind /proc "${MOUNT_DIR}/proc"
CHROOT_MOUNTS=("${MOUNT_DIR}/dev/pts" "${MOUNT_DIR}/dev" "${MOUNT_DIR}/sys" "${MOUNT_DIR}/proc")
mount --bind /sys "${MOUNT_DIR}/sys"
mount --bind /dev "${MOUNT_DIR}/dev"
mount --bind /dev/pts "${MOUNT_DIR}/dev/pts"

# --- Copy resolv.conf ---
echo ""
echo "=== Copy resolv.conf ==="
# Back up the original (may be a symlink on Trixie — back up the target)
if [[ -e "${MOUNT_DIR}/etc/resolv.conf" ]]; then
    cp "${MOUNT_DIR}/etc/resolv.conf" "${MOUNT_DIR}/etc/resolv.conf.bak" || true
fi
# Replace with the host's real resolv.conf (works inside Docker)
if [[ -L "${MOUNT_DIR}/etc/resolv.conf" ]]; then
    rm "${MOUNT_DIR}/etc/resolv.conf"
fi
cp /etc/resolv.conf "${MOUNT_DIR}/etc/resolv.conf"

# --- Enable SSH ---
echo ""
echo "=== Enable SSH ==="
touch "${MOUNT_DIR}/boot/firmware/ssh"
echo "  Created /boot/firmware/ssh"

# --- Set pi user password ---
echo ""
echo "=== Set pi user password ==="
# Generate hashed password and write to userconf.txt for first-boot user setup.
# Pi OS Trixie uses this file to set up the default user on first boot.
PI_PASSWORD="picketfencing"
PI_HASH=$(openssl passwd -6 "$PI_PASSWORD")
echo "pi:${PI_HASH}" > "${MOUNT_DIR}/boot/firmware/userconf.txt"
echo "  Written to /boot/firmware/userconf.txt (pi:picketfencing)"

# --- WiFi configuration ---
if [[ -n "$WIFI_SSID" ]]; then
    echo ""
    echo "=== Configure WiFi (SSID: $WIFI_SSID, country: $WIFI_COUNTRY) ==="
    NM_CONN_DIR="${MOUNT_DIR}/etc/NetworkManager/system-connections"
    mkdir -p "$NM_CONN_DIR"
    cat > "${NM_CONN_DIR}/wifi.nmconnection" << EOF
[connection]
id=wifi
type=wifi
autoconnect=true

[wifi]
mode=infrastructure
ssid=${WIFI_SSID}

[wifi-security]
auth-alg=open
key-mgmt=wpa-psk
psk=${WIFI_PASSWORD}

[ipv4]
method=auto

[ipv6]
method=auto
EOF
    chmod 0600 "${NM_CONN_DIR}/wifi.nmconnection"
    echo "  Written to $NM_CONN_DIR/wifi.nmconnection"
else
    echo "=== WiFi not configured (WIFI_SSID not set) ==="
fi

# --- Build chroot environment ---
CHROOT_ENV=(
    "DEBIAN_FRONTEND=noninteractive"
    "PATH=/usr/sbin:/usr/bin:/sbin:/bin"
)
[[ -n "$TAILSCALE_AUTHKEY" ]] && CHROOT_ENV+=("TAILSCALE_AUTHKEY=${TAILSCALE_AUTHKEY}")
[[ -n "${OPENWEBRX_ADMIN_PASSWORD:-}" ]] && CHROOT_ENV+=("OPENWEBRX_ADMIN_PASSWORD=${OPENWEBRX_ADMIN_PASSWORD}")

# --- Run provision.sh in chroot ---
echo ""
echo "=== Provisioning (chroot) ==="
chroot "${MOUNT_DIR}" env -i "${CHROOT_ENV[@]+"${CHROOT_ENV[@]}"}" /opt/provision/provision.sh

# --- Successful provisioning — rename work image to output ---
echo ""
echo "=== Provisioning complete — finalising image ==="

# Cleanup trap handles unmounting, restoring preload/resolv, removing qemu and provision dirs.
# We need cleanup to run before compression, so we do it explicitly here and reset state so the
# trap is a no-op.
cleanup
# Disable trap (state already reset inside cleanup)
trap - EXIT

# --- Rename work image ---
mv "$IMAGE_WORK" "$IMAGE_OUT"

# --- Compress ---
if [[ "$COMPRESS" != "false" ]]; then
    echo ""
    echo "=== Compress with xz ==="
    xz -T0 -6 --keep "$IMAGE_OUT"
    echo "Compressed: ${IMAGE_OUT}.xz"
else
    echo "=== Compression skipped (COMPRESS=false) ==="
fi

echo ""
echo "=== SUCCESS ==="
echo "Output image : $IMAGE_OUT"
if [[ "$COMPRESS" != "false" ]]; then
    echo "Compressed   : ${IMAGE_OUT}.xz"
fi
echo ""
echo "Flash with:"
echo "  sudo dd if=$IMAGE_OUT of=/dev/sdX bs=4M status=progress"
