#!/usr/bin/env bash
# provision.sh — runs inside arm64 chroot (QEMU user-mode emulation)
# Installs SDR packages from Trixie apt, Tailscale, and configures the system.
# Called by build-image.sh via: chroot /mnt/pi env -i ... /opt/provision/provision.sh
#
# Env vars (passed in via env -i from build-image.sh):
#   DEBIAN_FRONTEND      — set to noninteractive
#   PATH                 — /usr/sbin:/usr/bin:/sbin:/bin
#   TAILSCALE_AUTHKEY    — optional; if set, creates a first-boot auth service
#   OPENWEBRX_ADMIN_PASSWORD — optional; if set, creates an admin user for the web UI

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# --- Guard: must run as root ---
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: provision.sh must run as root (inside chroot)."
    exit 1
fi

echo "=== Starting provisioning ==="

# --- Prevent service starts inside chroot ---
# apt triggers post-install service starts; without policy-rc.d returning 101
# those calls hang indefinitely (no real init running in chroot).
cat > /usr/sbin/policy-rc.d << 'POLICY'
#!/bin/sh
exit 101
POLICY
chmod +x /usr/sbin/policy-rc.d

# --- Fix initramfs-tools ---
# mkinitramfs inside chroot can't determine the root device.
# MODULES=most avoids the "failed to determine device for /" error on first boot.
sed -i 's/^MODULES=.*/MODULES=most/' /etc/initramfs-tools/initramfs.conf

# --- dpkg recovery ---
# Interrupted dpkg runs leave locks/partial state; fix before apt-get update.
echo "=== dpkg recovery ==="
dpkg --configure -a

# --- System update ---
echo "=== apt-get update ==="
apt-get update

# --- SDR packages ---
# Trixie ships rtl-sdr 2.0.2 which includes RTL-SDR V4 support natively.
# No source compilation needed.
echo "=== Install SDR packages ==="
apt-get install -y \
    rtl-sdr \
    soapysdr-tools \
    soapysdr-module-rtlsdr \
    python3-soapysdr

# --- Enable SSH ---
echo "=== Enable SSH ==="
apt-get install -y openssh-server
systemctl enable ssh.service || ln -sf \
    /lib/systemd/system/ssh.service \
    /etc/systemd/system/multi-user.target.wants/ssh.service

# Set pi user password (matches userconf.txt on boot partition)
echo "pi:picketfencing" | chpasswd

# --- TCP MSS clamp for CGNAT environments ---
# T-Mobile home internet and other CGNAT providers have reduced path MTU (~1424)
# but silently drop oversized packets without sending ICMP "too big" responses.
# This causes TLS handshakes (including Tailscale control plane) to hang.
# Clamping TCP MSS to PMTU lets the kernel negotiate correct segment sizes.
echo "=== Configure TCP MSS clamp ==="
cat > /etc/networkd-dispatcher/routable.d/50-mss-clamp << 'MSSCLAMP'
#!/bin/sh
iptables -t mangle -C POSTROUTING -p tcp --tcp-flags SYN,RST SYN -o "$IFACE" -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || \
iptables -t mangle -A POSTROUTING -p tcp --tcp-flags SYN,RST SYN -o "$IFACE" -j TCPMSS --clamp-mss-to-pmtu
MSSCLAMP
chmod +x /etc/networkd-dispatcher/routable.d/50-mss-clamp

# --- DVB kernel module blacklist ---
# DVB modules claim the RTL-SDR chip at boot; blacklisting hands it to rtl-sdr.
echo "=== Install DVB blacklist ==="
cp /opt/provision/config/system/blacklist-sdr.conf /etc/modprobe.d/blacklist-sdr.conf

# --- Tailscale ---
echo "=== Install Tailscale ==="
# The official install.sh detects codename automatically and sets up the apt repo.
curl -fsSL https://tailscale.com/install.sh | sh

# If an auth key was provided, create a first-boot service to authenticate.
TAILSCALE_AUTHKEY="${TAILSCALE_AUTHKEY:-}"
if [[ -n "$TAILSCALE_AUTHKEY" ]]; then
    echo "=== Writing Tailscale first-boot auth service ==="
    cat > /etc/systemd/system/tailscale-firstboot.service << EOF
[Unit]
Description=Tailscale first-boot authentication
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/tailscale up --authkey=${TAILSCALE_AUTHKEY}
ExecStartPost=/bin/systemctl disable tailscale-firstboot.service
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl enable tailscale-firstboot.service || ln -sf \
        /etc/systemd/system/tailscale-firstboot.service \
        /etc/systemd/system/multi-user.target.wants/tailscale-firstboot.service
fi

# --- Tailscale HTTPS cert for OpenWebRX+ ---
# Generates a real Let's Encrypt cert via Tailscale on first boot.
# Browsers require HTTPS for web audio — without this, no sound.
cat > /etc/systemd/system/tailscale-cert.service << 'CERTSERVICE'
[Unit]
Description=Generate Tailscale HTTPS certificate for OpenWebRX+
After=tailscale-firstboot.service tailscaled.service
Wants=network-online.target

[Service]
Type=oneshot
# Wait for Tailscale to be fully connected
ExecStartPre=/bin/sh -c 'until tailscale status --json | grep -q "BackendState.*Running"; do sleep 2; done'
ExecStart=/bin/sh -c 'FQDN=$(tailscale status --self --json | python3 -c "import sys,json; print(json.load(sys.stdin)[\"Self\"][\"DNSName\"].rstrip(\".\"))") && tailscale cert --cert-file /opt/openwebrx/etc/openwebrx/cert.pem --key-file /opt/openwebrx/etc/openwebrx/key.pem "$FQDN"'
ExecStartPost=/usr/bin/docker restart openwebrx 2>/dev/null
ExecStartPost=/bin/systemctl disable tailscale-cert.service
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
CERTSERVICE
systemctl enable tailscale-cert.service || ln -sf \
    /etc/systemd/system/tailscale-cert.service \
    /etc/systemd/system/multi-user.target.wants/tailscale-cert.service

# --- OpenWebRX+ (Docker) ---
# The OpenWebRX+ PPA requires Python < 3.12 (python3-csdr dependency), but
# Trixie ships Python 3.13. Instead of native install, we run OpenWebRX+ as a
# Docker container on the Pi. The slechev/openwebrxplus-softmbe image bundles
# all decoders (DMR, D-STAR, NXDN, P25, M17, WSJTX, APRS, etc.) for arm64.
echo "=== Install Docker for OpenWebRX+ ==="
# Install Docker from official repo — can't run the daemon in chroot,
# but we can install packages. The container image pulls on first boot.
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
usermod -aG docker pi || true

# Deploy OpenWebRX+ config to host paths that get volume-mounted into the container
echo "=== Deploy OpenWebRX+ config ==="
mkdir -p /opt/openwebrx/etc/openwebrx /opt/openwebrx/var
cp /opt/provision/config/openwebrx/openwebrx.conf /opt/openwebrx/etc/openwebrx/openwebrx.conf
cp /opt/provision/config/openwebrx/settings.json /opt/openwebrx/var/settings.json

if [[ -d /opt/provision/config/openwebrx/bookmarks.d ]]; then
    mkdir -p /opt/openwebrx/etc/openwebrx/bookmarks.d
    cp /opt/provision/config/openwebrx/bookmarks.d/*.json /opt/openwebrx/etc/openwebrx/bookmarks.d/
fi

# Create docker-compose.yml for OpenWebRX+
OPENWEBRX_ADMIN_PASSWORD="${OPENWEBRX_ADMIN_PASSWORD:-}"
cat > /opt/openwebrx/docker-compose.yml << 'COMPOSE'
services:
  openwebrx:
    image: slechev/openwebrxplus-softmbe:latest
    container_name: openwebrx
    restart: unless-stopped
    ports:
      - "8073:8073"
    devices:
      - /dev/bus/usb:/dev/bus/usb
    volumes:
      - /opt/openwebrx/etc/openwebrx:/etc/openwebrx
      - /opt/openwebrx/var:/var/lib/openwebrx
    tmpfs:
      - /tmp
COMPOSE

# Add admin user env vars if password was provided
if [[ -n "$OPENWEBRX_ADMIN_PASSWORD" ]]; then
    cat >> /opt/openwebrx/docker-compose.yml << EOF
    environment:
      - OPENWEBRX_ADMIN_USER=admin
      - OPENWEBRX_ADMIN_PASSWORD=${OPENWEBRX_ADMIN_PASSWORD}
EOF
fi

# Create systemd service to start OpenWebRX+ container on boot
cat > /etc/systemd/system/openwebrx.service << 'SERVICE'
[Unit]
Description=OpenWebRX+ Web SDR Receiver
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=/opt/openwebrx
# docker load of 1.1GB tar on SD card can take >90s; allow 5 minutes
TimeoutStartSec=300
# Load pre-saved image on first boot, then delete the tar to free ~1GB
ExecStartPre=/bin/sh -c 'test -f /opt/openwebrx/openwebrxplus-softmbe-arm64.tar && docker load < /opt/openwebrx/openwebrxplus-softmbe-arm64.tar && rm -f /opt/openwebrx/openwebrxplus-softmbe-arm64.tar || true'
ExecStart=/usr/bin/docker compose up --remove-orphans
ExecStop=/usr/bin/docker compose down
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE
systemctl enable openwebrx.service || ln -sf \
    /etc/systemd/system/openwebrx.service \
    /etc/systemd/system/multi-user.target.wants/openwebrx.service

# --- Deploy test scripts ---
echo "=== Deploy test scripts ==="
if [[ -d /opt/provision/test-scripts ]]; then
    cp /opt/provision/test-scripts/* /usr/local/bin/
    chmod +x /usr/local/bin/*
fi

# --- Cleanup ---
echo "=== Cleanup ==="
apt-get clean
rm -rf /var/lib/apt/lists/*
rm -f /usr/sbin/policy-rc.d

echo ""
echo "=== Provisioning complete ==="
