# pi-sdr — Implementation Planning Prompt

## Context

This is a new, clean repo for building minimal Raspberry Pi 5 SDR images. It's a stripped-down version of [pi-radio-monitoring-research](https://github.com/mihow/pi-radio-monitoring-research) — SDR drivers + Tailscale only, no audio stack.

The prior repo validated the Docker/QEMU image build pipeline with Bookworm. This project moves to **Trixie** and simplifies: Trixie's `rtl-sdr` 2.0.2 package includes V4 support, so we may not need the custom Blog fork compile.

## Goal

Build a working Docker-based image builder that produces a flashable `.img.xz` for Raspberry Pi 5 with:
- RTL-SDR V4 drivers (test Trixie's packaged rtl-sdr 2.0.2 first, fall back to Blog fork if needed)
- SoapySDR + SoapyRTLSDR + Python 3 bindings
- Tailscale (pre-installed, optional first-boot auth via env var)
- WiFi config via .env
- DVB kernel module blacklist
- Test scripts: `test-with-usb.sh` (Docker chroot with USB passthrough), `tune.sh` (tune to freq, record IQ), basic connectivity check
- Compressed `.img.xz` output
- GitHub Actions workflow to build + publish to Releases on tag push

## Key decisions to validate

1. **Does Trixie's packaged rtl-sdr 2.0.2 work with RTL-SDR Blog V4?**
   - Test: `apt install rtl-sdr` in a Trixie chroot, then `rtl_test -t` with a V4 dongle
   - If yes: skip the custom compile entirely, massive simplification
   - If no: compile the Blog fork like before

2. **Does Trixie's packaged SoapySDR work with the packaged rtl-sdr?**
   - Test: `apt install soapysdr-module-rtlsdr soapysdr-tools python3-soapysdr`
   - Verify: `SoapySDRUtil --find` detects the dongle, `python3 -c "import SoapySDR"` works

3. **Pin a Trixie image date**
   - Find the latest Pi OS Trixie lite arm64 image URL at raspberrypi.com
   - Pin it in build-image.sh like we did with Bookworm (2025-05-13)

## What to reuse from pi-radio-monitoring-research

Copy and adapt (don't copy verbatim — simplify for Trixie):
- `Dockerfile` — build container with host tools (qemu, parted, kpartx)
- `docker-compose.yml` — binfmt registration + build service
- `scripts/build-image.sh` — download, resize, mount, chroot, unmount
- `scripts/provision.sh` — SIMPLIFIED: just apt install + Tailscale + blacklist, no cmake builds if packaged versions work
- `scripts/test-with-usb.sh` — Docker chroot with USB passthrough
- `config/system/blacklist-sdr.conf` — DVB module blacklist

DO NOT copy:
- Anything audio-related (PipeWire, WirePlumber, shairport-sync, duck-daemon, espeak-ng)
- rtl_airband config and service
- Audio test scripts
- WirePlumber config files

## New things to add

- `.env.example` with documented variables
- `test-scripts/tune.sh` — tune to a frequency, record IQ samples, print signal info
- `test-scripts/scan.sh` — quick scan across a frequency range, report active signals
- GitHub Actions workflow (`.github/workflows/build.yml`) for CI/CD
- Proper `.gitignore` (data/, *.img, *.img.xz)

## Build pipeline (simplified for Trixie)

1. Download Pi OS Trixie arm64 lite
2. Resize image (+2GB — less space needed without audio stack)
3. Loop mount via kpartx
4. Chroot with QEMU user-mode
5. `apt update && apt install rtl-sdr soapysdr-tools soapysdr-module-rtlsdr python3-soapysdr`
6. If V4 doesn't work with packaged rtl-sdr: compile Blog fork
7. Install Tailscale from official repo
8. Deploy blacklist, WiFi config, test scripts
9. Cleanup, unmount, compress to .img.xz

## Test plan

- [ ] Build completes without errors
- [ ] Validation checks pass (rtl_test, SoapySDRUtil, python3 import)
- [ ] `test-with-usb.sh` detects a real V4 dongle
- [ ] `tune.sh` tunes to a known frequency and captures IQ
- [ ] Tailscale connects on first boot (if auth key provided)
- [ ] WiFi connects on first boot (if credentials provided)
- [ ] Image flashes and boots on a real Pi 5
- [ ] `.img.xz` is < 1GB compressed

## Hardware available

A real RTL-SDR Blog V4 dongle is plugged into the dev machine. This means we can:
- Test `rtl_test -t` inside the Docker chroot with USB passthrough
- Verify whether Trixie's packaged rtl-sdr 2.0.2 works with the V4
- Test SoapySDR device detection with real hardware
- Tune to actual frequencies and verify signal reception
- Run `test-with-usb.sh` for real validation, not just binary checks

This is the first time we have hardware in the loop — prioritize live testing over mock validation.

## References

- Prior repo: https://github.com/mihow/pi-radio-monitoring-research
- RTL-SDR Blog V4 info: https://www.rtl-sdr.com/v4/
- Trixie Pi OS images: https://downloads.raspberrypi.com/raspios_lite_arm64/images/
- Trixie rtl-sdr package: https://packages.debian.org/trixie/rtl-sdr
- SoapySDR Trixie: https://packages.debian.org/trixie/soapysdr-tools
