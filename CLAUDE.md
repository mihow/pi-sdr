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

## Downstream projects
- [pi-radio-station](https://github.com/mihow/pi-radio-station) — full SDR monitoring station with Liquidsoap audio mixing, AirPlay, web dashboard
