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

# --- OpenWebRX+ ---
# The PPA only has bookworm packages; they work on Trixie.
echo "=== Install OpenWebRX+ ==="
apt-get install -y gnupg
curl -fsSL https://luarvique.github.io/ppa/openwebrx-plus.gpg \
    | gpg --yes --dearmor -o /etc/apt/trusted.gpg.d/openwebrx-plus.gpg
echo "deb [signed-by=/etc/apt/trusted.gpg.d/openwebrx-plus.gpg] https://luarvique.github.io/ppa/bookworm ./" \
    > /etc/apt/sources.list.d/openwebrx-plus.list
apt-get update
apt-get install -y openwebrx

# --- Digital mode decoders ---
echo "=== Install digital mode decoders ==="
apt-get install -y \
    codec2 \
    direwolf \
    wsjtx \
    m17-demod \
    multimon-ng

# --- Deploy OpenWebRX+ config ---
echo "=== Deploy OpenWebRX+ config ==="
cp /opt/provision/config/openwebrx/openwebrx.conf /etc/openwebrx/openwebrx.conf

# Settings (SDR profiles, receiver info) go to data directory
mkdir -p /var/lib/openwebrx
cp /opt/provision/config/openwebrx/settings.json /var/lib/openwebrx/settings.json

# Frequency bookmarks
if [[ -d /opt/provision/config/openwebrx/bookmarks.d ]]; then
    mkdir -p /etc/openwebrx/bookmarks.d
    cp /opt/provision/config/openwebrx/bookmarks.d/*.json /etc/openwebrx/bookmarks.d/
fi

# Fix ownership — openwebrx user is created by the package install
chown -R openwebrx:openwebrx /var/lib/openwebrx/

# --- OpenWebRX admin user ---
OPENWEBRX_ADMIN_PASSWORD="${OPENWEBRX_ADMIN_PASSWORD:-}"
if [[ -n "$OPENWEBRX_ADMIN_PASSWORD" ]]; then
    echo "=== Create OpenWebRX admin user ==="
    echo "$OPENWEBRX_ADMIN_PASSWORD" | openwebrx admin adduser --noninteractive admin
fi

# Enable the service at boot
echo "=== Enable OpenWebRX service ==="
systemctl enable openwebrx.service || ln -sf \
    /lib/systemd/system/openwebrx.service \
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
