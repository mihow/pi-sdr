# pi-sdr

Minimal Raspberry Pi 5 image builder for Software-Defined Radio. Produces a flashable `.img.xz` with RTL-SDR V4 drivers, SoapySDR, and Tailscale pre-installed. No audio stack, no scanner config — just the SDR foundation.

## Prerequisites

- Docker with compose plugin
- ~10 GB free disk space (for image download + build)

That's it. The Docker container handles all other dependencies (QEMU, parted, kpartx, etc).

## Quick start

### Build the image

```bash
git clone https://github.com/mihow/pi-sdr.git && cd pi-sdr

# Optional: configure WiFi and Tailscale
cp .env.example .env
# Edit .env with your settings

# Build the image
docker compose run --rm build
```

Output: `data/raspios-trixie-arm64-lite-provisioned.img.xz` (~694 MB)

For faster dev cycles, skip compression:

```bash
COMPRESS=false docker compose run --rm build
```

### Flash to SD card

```bash
xz -d data/raspios-trixie-arm64-lite-provisioned.img.xz
sudo dd if=data/raspios-trixie-arm64-lite-provisioned.img of=/dev/sdX bs=4M status=progress
```

Or use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) (recommended) — select "Use custom" and pick the `.img.xz` directly. It handles decompression, shows only removable drives, verifies the write, and lets you set hostname/SSH keys/locale through its settings menu.

Pre-built images are available from the [latest release](../../releases/latest).

## What's included

| Component | Version | Notes |
|-----------|---------|-------|
| Raspberry Pi OS | Trixie arm64 lite | Debian 13, pinned 2025-12-04 |
| rtl-sdr | 2.0.2 | RTL-SDR Blog V4 support (packaged, no source build) |
| SoapySDR | 0.8.1 | Universal SDR API |
| SoapyRTLSDR | 0.3.3 | SoapySDR driver for RTL-SDR |
| python3-soapysdr | 0.8.1 | `import SoapySDR` works out of the box |
| Tailscale | latest stable | Remote access without port forwarding |

Also includes: DVB kernel module blacklist, udev rules (from `librtlsdr` package), test scripts at `/usr/local/bin/`.

## Testing with a real dongle (no flashing needed)

Plug in your RTL-SDR and test inside the built arm64 image via Docker:

```bash
# Interactive shell
docker run --rm -it --privileged --device=/dev/bus/usb \
  -v ./data:/build/data -v ./scripts:/build/scripts \
  --entrypoint /build/scripts/test-with-usb.sh \
  $(docker compose config --images 2>/dev/null | tail -1)

# Run a specific command
docker run --rm --privileged --device=/dev/bus/usb \
  -v ./data:/build/data -v ./scripts:/build/scripts -v ./test-scripts:/build/test-scripts \
  --entrypoint /build/scripts/test-with-usb.sh \
  $(docker compose config --images 2>/dev/null | tail -1) \
  check-sdr.sh
```

Inside the chroot:

```bash
rtl_test -t                        # Detect dongle, check tuner
SoapySDRUtil --find                # List SDR devices via SoapySDR
check-sdr.sh                       # Full device/driver check
tune.sh 462.5625e6 -d 5            # Capture 5s of IQ from GMRS ch1
scan.sh 88e6 108e6 -o fm_band.csv  # Sweep FM broadcast band
```

## Test scripts

These are installed to `/usr/local/bin/` on the Pi image:

| Script | Purpose |
|--------|---------|
| `check-sdr.sh` | Checks USB device, kernel modules, rtl-sdr, SoapySDR, Python bindings |
| `tune.sh <freq> [-s rate] [-d sec] [-o file]` | Tune to a frequency and capture raw IQ samples |
| `scan.sh <start> <end> [-b bin] [-n sweeps] [-o file]` | Sweep a frequency range with `rtl_power`, output CSV |

## Configuration

Copy `.env.example` to `.env`:

```bash
# WiFi (optional — leave blank for ethernet only)
WIFI_SSID=MyNetwork
WIFI_PASSWORD=secret
WIFI_COUNTRY=US

# Tailscale (optional — authenticate manually on first boot if blank)
TAILSCALE_AUTHKEY=tskey-auth-...
```

Build-time options (env vars passed to `docker compose run`):

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPRESS` | `true` | Set `false` to skip xz compression |
| `DEBUG` | — | Set `1` for verbose build output (`set -x`) |

## How the build works

1. Docker container (Ubuntu 24.04) with QEMU user-mode emulation
2. Downloads pinned Raspberry Pi OS Trixie arm64 lite image (cached after first download)
3. Expands the root partition by 2 GB
4. Mounts via `kpartx` and chroots with QEMU aarch64
5. `apt install rtl-sdr soapysdr-tools ...` (no source compilation needed — Trixie's rtl-sdr 2.0.2 includes V4 support)
6. Installs Tailscale, DVB blacklist, WiFi config, test scripts
7. Cleans up and compresses to `.img.xz`

To rebuild, remove the output image and re-run. The downloaded base image is cached:

```bash
rm data/raspios-trixie-arm64-lite-provisioned.img
docker compose run --rm build
```

### Docker quirks addressed

- **No `/dev/loopXpN` in Docker** — `losetup -P` doesn't create partition devices inside containers. Solved with `kpartx` which creates `/dev/mapper/` entries.
- **dpkg hangs on service restarts** — post-install scripts try to start services in the chroot. Solved with `policy-rc.d` returning exit code 101.
- **initramfs fails** — `mkinitramfs` can't detect root device in chroot. Solved with `MODULES=most` in initramfs config.
- **ld.so.preload breaks QEMU** — Pi OS ships a preload that fails under emulation. Commented out during build, restored after.

## CI/CD

GitHub Actions builds and publishes `.img.xz` to Releases on tag push:

```bash
git tag v0.1.0 && git push --tags
```

## Downstream projects

This image is a foundation. Install additional software on top:

- [OpenWebRX+](https://www.openwebrx.de/) — web-based SDR receiver with waterfall
- [trunk-recorder](https://github.com/robotastic/trunk-recorder) — trunked radio system recorder
- [rtl_airband](https://github.com/rtl-airband/RTLSDR-Airband) — aviation scanner
- [signal-logs](https://github.com/mihow/signal-logs) — passive radio monitoring with AI-powered transcription and summarization
- [pi-radio-station](https://github.com/mihow/pi-radio-station) — full monitoring station with audio mixing
