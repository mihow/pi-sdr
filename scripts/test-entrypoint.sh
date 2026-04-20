#!/bin/sh
# test-entrypoint.sh — simulates Pi first boot inside a test container
# Starts Tailscale, generates HTTPS cert, deploys OpenWebRX+ config, starts SSH.
set -e

echo "=== Pi SDR Test Container ==="

# --- Deploy OpenWebRX+ config to shared volume ---
echo "Deploying OpenWebRX+ config..."
cp /config-src/openwebrx.conf /owrx-etc/openwebrx.conf
if [ -d /config-src/bookmarks.d ]; then
    cp -r /config-src/bookmarks.d /owrx-etc/bookmarks.d
fi

# --- Start Tailscale ---
echo "Starting tailscaled..."
tailscaled --state=/var/lib/tailscale/tailscaled.state &
sleep 2

echo "Authenticating with Tailscale..."
if [ -n "$TS_AUTHKEY" ]; then
    tailscale up --authkey="$TS_AUTHKEY" --hostname="${TS_HOSTNAME:-pi-sdr-test}"
else
    echo "ERROR: TS_AUTHKEY not set"
    exit 1
fi

# --- Get identity ---
echo ""
echo "=== Tailscale Status ==="
tailscale status
FQDN=$(tailscale status --self --json | grep -m1 '"DNSName"' | sed 's/.*"DNSName": *"\([^"]*\)".*/\1/' | sed 's/\.$//')
TS_IP=$(tailscale ip -4)
echo ""
echo "Tailscale FQDN: $FQDN"
echo "Tailscale IP:   $TS_IP"

# --- Generate HTTPS cert into the shared OpenWebRX config dir ---
echo ""
echo "=== Generating HTTPS cert ==="
CERT_OK=false
if [ -n "$FQDN" ]; then
    # Retry up to 3 times — ACME authorization can lag on new hostnames
    for attempt in 1 2 3; do
        if tailscale cert --cert-file /owrx-etc/cert.pem --key-file /owrx-etc/key.pem "$FQDN" 2>&1; then
            chmod 644 /owrx-etc/cert.pem /owrx-etc/key.pem
            echo "HTTPS cert generated for $FQDN"
            CERT_OK=true
            break
        fi
        echo "  Cert attempt $attempt failed, retrying in 5s..."
        sleep 5
    done
fi
if [ "$CERT_OK" = false ]; then
    echo "WARNING: HTTPS cert generation failed — OpenWebRX+ will use HTTP"
fi

# --- Set up SSH ---
if command -v sshd >/dev/null 2>&1 || apk add openssh 2>/dev/null; then
    echo ""
    echo "=== Setting up SSH ==="
    adduser -D -s /bin/sh pi 2>/dev/null || true
    echo "pi:picketfencing" | chpasswd 2>/dev/null || true
    # Alpine: sshd needs shadow group access to verify passwords
    addgroup sshd shadow 2>/dev/null || true
    ssh-keygen -A 2>/dev/null || true
    mkdir -p /etc/ssh
    cat > /etc/ssh/sshd_config <<SSHD
Port 22
PermitRootLogin no
PasswordAuthentication yes
SSHD
    /usr/sbin/sshd -e 2>&1 &
    echo "SSH ready"
fi

# --- Summary ---
echo ""
echo "========================================="
echo "  Pi SDR Test Container Ready"
echo "========================================="
echo ""
echo "  OpenWebRX+: https://$FQDN:8073"
echo "  SSH:        ssh pi@$FQDN"
echo "  Password:   picketfencing"
echo "  Tailscale:  $TS_IP"
echo ""
echo "========================================="

# Keep running
tail -f /dev/null
