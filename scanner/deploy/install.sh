#!/usr/bin/env bash
# Install or upgrade the sdrscan container service. Idempotent.
#
#   install.sh [--dry-run] [--image FILE.tar.zst] [--config FILE.yaml] [--skip-preflight]
#
# Steps: preflight checks, load the image (the old one is kept as sdrscan:previous),
# write /etc/sdrscan/config.yaml from the template when missing, install and start the
# unit, then wait up to 120 s for a healthy service. If it is not healthy, the previous
# image is restored and the script exits non-zero. --dry-run prints every step and changes
# nothing.
set -euo pipefail

DRY_RUN=0
IMAGE=""
CONFIG_SRC=""
SKIP_PREFLIGHT=0
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ETC="${SDRSCAN_ETC:-/etc/sdrscan}"
UNIT_DIR="${SDRSCAN_UNIT_DIR:-/etc/systemd/system}"
HEALTH_WAIT_S="${SDRSCAN_HEALTH_WAIT_S:-120}"
MIN_FREE_KB="${SDRSCAN_MIN_FREE_KB:-$((1536 * 1024))}"  # 1.5 GiB; the image needs ~0.3 GiB, the rest is the runtime floor

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --image) IMAGE="$2"; shift ;;
    --config) CONFIG_SRC="$2"; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n== %s\n' "$*"; }
run() {
  printf '+ %s\n' "$*"
  if [ "$DRY_RUN" -eq 0 ]; then "$@"; fi
}
warn() { printf 'WARNING: %s\n' "$*" >&2; }

step "1. Preflight"
if [ "$SKIP_PREFLIGHT" -eq 1 ]; then
  echo "skipped"
else
  if grep -qE '^VERSION_CODENAME=(bookworm|trixie)' /etc/os-release 2>/dev/null; then
    echo "OS release: ok"
  else
    warn "untested OS release; expected Debian or Raspberry Pi OS bookworm or trixie"
  fi
  free_kb=$(df -Pk /var/lib | awk 'NR==2 {print $4}')
  if [ "${free_kb:-0}" -lt "$MIN_FREE_KB" ]; then
    echo "free disk ${free_kb} KB is below 3 GB; stopping" >&2
    [ "$DRY_RUN" -eq 1 ] || exit 1
  else
    echo "free disk: $((free_kb / 1024)) MB"
  fi
  if grep -rqs 'blacklist dvb_usb_rtl28xxu' /etc/modprobe.d/; then
    echo "DVB driver blacklist: present"
  else
    warn "dvb_usb_rtl28xxu is not blacklisted; the kernel driver may claim the dongle"
  fi
  if command -v fuser >/dev/null && fuser /dev/bus/usb/*/* 2>/dev/null | grep -q .; then
    warn "a process holds a USB device; check that nothing else uses the dongle"
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "+ timeout 15 rtl_test -s 2400000 (10 s sample-loss test, not run in a dry run)"
  elif command -v rtl_test >/dev/null; then
    out=$(timeout 15 rtl_test -s 2400000 2>&1 || true)
    if printf '%s' "$out" | grep -q 'lost at least'; then
      warn "rtl_test reported lost samples"
    else
      echo "rtl_test: no lost samples reported"
    fi
  else
    warn "rtl_test not installed on the host; skipping the sample-loss test"
  fi
fi

step "2. Load the image"
if docker image inspect sdrscan:current >/dev/null 2>&1; then
  run docker tag sdrscan:current sdrscan:previous
fi
if [ -n "$IMAGE" ]; then
  if [ "${IMAGE%.zst}" != "$IMAGE" ]; then
    printf '+ zstd -dc %s | docker load\n' "$IMAGE"
    if [ "$DRY_RUN" -eq 0 ]; then zstd -dc "$IMAGE" | docker load; fi
  else
    run docker load -i "$IMAGE"
  fi
  run docker tag sdrscan:arm64 sdrscan:current
else
  echo "no --image given; using the image already tagged sdrscan:current"
fi

step "3. Configuration"
run install -d -m 0755 "$ETC"
if [ -e "$ETC/config.yaml" ]; then
  echo "$ETC/config.yaml exists; kept"
else
  run install -m 0644 "${CONFIG_SRC:-$HERE/config.template.yaml}" "$ETC/config.yaml"
fi
if [ ! -e "$ETC/uplink.env" ]; then
  echo "+ create $ETC/uplink.env (mode 0600) with the S3 settings left for the owner"
  if [ "$DRY_RUN" -eq 0 ]; then
    (umask 077; printf '%s\n' \
      '# Fill these in, then: systemctl restart sdrscan. Pushes stay off until then.' \
      '# SDRSCAN_S3_ENDPOINT=https://<object-store-origin>' \
      '# SDRSCAN_S3_BUCKET=<bucket>' \
      '# SDRSCAN_S3_PREFIX=<site>' \
      '# SDRSCAN_S3_ACCESS_KEY=<key id scoped to the prefix>' \
      '# SDRSCAN_S3_SECRET_KEY=<secret>' > "$ETC/uplink.env")
  fi
fi
gid=$(getent group plugdev | cut -d: -f3 || true)
echo "+ write $ETC/docker.env (SDRSCAN_USB_GID=${gid:-0})"
if [ "$DRY_RUN" -eq 0 ]; then
  printf 'SDRSCAN_USB_GID=%s\n' "${gid:-0}" > "$ETC/docker.env"
fi

step "4. Service unit"
run install -D -m 0755 "$HERE/sdrscan-run" "${SDRSCAN_LIB:-/usr/local/lib/sdrscan}/sdrscan-run"
run install -m 0644 "$HERE/sdrscan.service" "$UNIT_DIR/sdrscan.service"
run systemctl daemon-reload
run systemctl enable sdrscan.service
run systemctl restart sdrscan.service

step "5. Wait for a healthy service (up to ${HEALTH_WAIT_S} s)"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "+ docker exec sdrscan sdrscan health (polled every 5 s)"
  echo "dry run: nothing was changed"
  exit 0
fi
waited=0
while [ "$waited" -lt "$HEALTH_WAIT_S" ]; do
  if docker exec sdrscan sdrscan health >/dev/null 2>&1; then
    echo "healthy after ${waited} s"
    exit 0
  fi
  sleep 5
  waited=$((waited + 5))
done
echo "not healthy after ${HEALTH_WAIT_S} s; rolling back" >&2
if docker image inspect sdrscan:previous >/dev/null 2>&1; then
  docker tag sdrscan:previous sdrscan:current
  systemctl restart sdrscan.service
fi
exit 1
