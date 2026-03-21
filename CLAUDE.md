# CLAUDE.md — pi-sdr

## Project Overview

Minimal Raspberry Pi 5 image builder for SDR (Software-Defined Radio). Produces a flashable `.img.xz` image with RTL-SDR V4 drivers, SoapySDR, and Tailscale pre-installed. No audio stack, no scanner config — just the SDR foundation that other projects build on.

## Architecture

Docker-based image builder using QEMU user-mode emulation to cross-compile for arm64 inside a chroot. The build runs entirely in a privileged Docker container — no sudo needed on the host.

### What's in the image
- Raspberry Pi OS Trixie arm64 lite (pinned date)
- rtl-sdr 2.0.2 (Trixie package — includes V4 support)
- SoapySDR + SoapyRTLSDR + Python 3 bindings
- Tailscale (pre-installed, optional first-boot auth)
- WiFi config via env vars
- DVB kernel module blacklist
- Test scripts for verifying SDR functionality

### What's NOT in the image
- No audio stack (PipeWire, shairport-sync, etc.)
- No scanner (rtl_airband)
- No web UI
- These are added by downstream projects (e.g., pi-radio-station)

## Development

### Build
```bash
docker compose run --rm build
```

### Test (no flashing needed)
```bash
# Forward USB dongle into Docker chroot
./scripts/test-with-usb.sh

# Inside chroot:
rtl_test -t
SoapySDRUtil --find
```

### Release
GitHub Actions builds and publishes `.img.xz` to Releases on tag push.

## Conventions
- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`
- Python 3.10+ type annotations
- Shell scripts pass `shellcheck`

## Key learnings from prior work

From [pi-radio-monitoring-research](https://github.com/mihow/pi-radio-monitoring-research):

- **QEMU arm64 builds** need `make -j1` fallback (linker segfaults under parallel load)
- **dpkg in chroot** needs `--configure -a` recovery (ordering issues)
- **kpartx** required for loop device partitions in Docker (`losetup -P` doesn't create `/dev/loopXpN`)
- **policy-rc.d** returning 101 prevents service start hangs in chroot
- **initramfs** needs `MODULES=most` to avoid "can't determine root device" error
- **PipeWire TCP mode** (port 4713) required for audio from Docker containers — socket bind-mount doesn't work
- **Trixie rtl-sdr 2.0.2** includes V4 support — may not need custom Blog fork compile

From this project (pi-sdr, feat/openwebrx branch):

- **T-Mobile CGNAT path MTU** is ~1424 — silently drops oversized packets without ICMP "too big". TLS handshakes (including Tailscale control plane) hang on first boot. Fix: TCP MSS clamp (`iptables -t mangle -A POSTROUTING -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu`). Test with `ping -c1 -M do -s 1396 -4 8.8.8.8`.
- **docker load on SD card** takes >90s for a 1.1GB tar — exceeds systemd's default `TimeoutStartSec=90s`. Set `TimeoutStartSec=300` on services that run `docker load`.
- **Tailscale reusable keys** are required — one-time keys get consumed before first-boot reboot completes, leaving the device unable to re-authenticate.
- **OpenWebRX+ requires Docker** on Trixie — native apt install fails because `python3-csdr` requires Python < 3.12 but Trixie ships 3.13.
- **OpenWebRX+ auto-detects SSL** certs at `/etc/openwebrx/cert.pem` and `/etc/openwebrx/key.pem` — no config change needed beyond placing the files there.
- **SSH "too many auth failures"** happens when the client has many SSH keys loaded — the server's `MaxAuthTries` is exhausted before password auth is tried. Use `-o PubkeyAuthentication=no` or `ssh-copy-id` to add a specific key.
- **Docker bridge creation triggers Tailscale rebinding** — `LinkChange: major` events during first boot can race with Tailscale's control plane connection. After a successful connection + reboot, cached state prevents the issue.

## Downstream projects
- [pi-radio-station](https://github.com/mihow/pi-radio-station) — full SDR monitoring station with Liquidsoap audio mixing, AirPlay, web dashboard
