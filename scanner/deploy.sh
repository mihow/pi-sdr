#!/usr/bin/env bash
# Deploy scanner to Raspberry Pi via SSH.
# Usage: ./scanner/deploy.sh <pi-host> [user]
set -euo pipefail

PI_HOST="${1:?Usage: deploy.sh <pi-host> [user]}"
PI_USER="${2:-pi}"
REMOTE_DIR="/home/${PI_USER}/scanner"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Deploying scanner to ${PI_USER}@${PI_HOST}:${REMOTE_DIR} ==="

# Create remote directory and copy files
ssh "${PI_USER}@${PI_HOST}" "mkdir -p ${REMOTE_DIR}"
scp -r "${SCRIPT_DIR}/"*.py "${SCRIPT_DIR}/requirements.txt" "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/"

echo "=== Installing dependencies ==="
ssh "${PI_USER}@${PI_HOST}" "cd ${REMOTE_DIR} && pip install --break-system-packages -r requirements.txt 2>/dev/null || pip install -r requirements.txt"

echo ""
echo "=== Deployed! Run with: ==="
echo "  ssh ${PI_USER}@${PI_HOST} 'cd ${REMOTE_DIR} && python -m scanner --auto-start'"
echo ""
echo "  Dashboard: http://${PI_HOST}:8080"
echo "  OpenWebRX: http://${PI_HOST}:8073"
