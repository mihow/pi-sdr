#!/usr/bin/env bash
# Stop and remove the sdrscan service. The data volume (clips, database) is kept unless
# --purge is given. --dry-run prints every step and changes nothing.
#
#   uninstall.sh [--dry-run] [--purge]
set -euo pipefail

DRY_RUN=0
PURGE=0
UNIT_DIR="${SDRSCAN_UNIT_DIR:-/etc/systemd/system}"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

run() {
  printf '+ %s\n' "$*"
  if [ "$DRY_RUN" -eq 0 ]; then "$@"; fi
}

run systemctl disable --now sdrscan.service || true
run rm -f "$UNIT_DIR/sdrscan.service"
run systemctl daemon-reload
run docker rm -f sdrscan || true
if [ "$PURGE" -eq 1 ]; then
  run docker volume rm sdrscan-data
else
  echo "kept the sdrscan-data volume; run with --purge to delete clips and the database"
fi
