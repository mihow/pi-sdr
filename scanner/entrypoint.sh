#!/bin/sh
set -e

# Start SDRplay API daemon if the binary exists (SDRplay device support).
# The daemon provides shared-memory IPC for libsdrplay_api clients.
if [ -x /opt/sdrplay/sdrplay_apiService ]; then
    echo "Starting sdrplay_apiService..."
    /opt/sdrplay/sdrplay_apiService &
    sleep 1  # let it bind before SoapySDR tries to connect
fi

# Rebuild ld cache for any runtime-mounted libraries
ldconfig 2>/dev/null || true

exec python3 -m scanner "$@"
