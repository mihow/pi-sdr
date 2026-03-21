# pi-sdr

Raspberry Pi 5 SDR image builder with a web-based receiver. Produces a flashable `.img.xz` with OpenWebRX+, RTL-SDR V4 drivers, digital mode decoders, and Tailscale. Boot it, open a browser, tune the waterfall.

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

Output: `data/pi-sdr-base.img.xz` (~694 MB)

For faster dev cycles, skip compression:

```bash
COMPRESS=false docker compose run --rm build
```

### Flash to SD card

```bash
xz -d data/pi-sdr-base.img.xz
sudo dd if=data/pi-sdr-base.img of=/dev/sdX bs=4M status=progress
```

Or use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) (recommended) — select "Use custom" and pick the `.img.xz` directly. It handles decompression, shows only removable drives, verifies the write, and lets you set hostname/SSH keys/locale through its settings menu.

Pre-built images are available from the [latest release](../../releases/latest).

## What's included

| Component | Version | Notes |
|-----------|---------|-------|
| Raspberry Pi OS | Trixie arm64 lite | Debian 13, pinned 2025-12-04 |
| OpenWebRX+ | latest | Web-based SDR receiver with waterfall and decoders |
| rtl-sdr | 2.0.2 | RTL-SDR Blog V4 support (packaged, no source build) |
| SoapySDR | 0.8.1 | Universal SDR API |
| Tailscale | latest stable | Remote access without port forwarding |
| codec2 | — | FreeDV, digital voice codec |
| direwolf | — | APRS/AX.25 packet decoding |
| wsjtx | — | FT8, FT4, JT65, WSPR, Q65 weak signal modes |
| m17-demod | — | M17 digital voice |
| multimon-ng | — | POCSAG paging, EAS, DTMF, and more |

Also includes: DVB kernel module blacklist, udev rules, frequency bookmarks for US radio services, test scripts at `/usr/local/bin/`.

## Web UI (OpenWebRX+)

OpenWebRX+ runs as a Docker container on the Pi (the Bookworm apt packages aren't compatible with Trixie's Python 3.13). On first boot, the container image pulls automatically (~1 GB download). After that, it starts on port 8073:

- **Local network:** `http://<pi-ip>:8073`
- **Via Tailscale:** `http://<pi-tailscale-name>:8073`

Anyone on the network can tune and listen. The admin panel (settings, bookmarks) requires login.

### First-time setup

1. Log in to the admin panel at `http://<pi-ip>:8073/settings`
2. Set your receiver name, location, and GPS coordinates
3. Adjust RTL-SDR gain for your antenna and environment
4. Add local repeater frequencies to bookmarks

If you set `OPENWEBRX_ADMIN_PASSWORD` in `.env` before building, the admin user is pre-created. Otherwise create one via SSH:

```bash
docker exec -it openwebrx openwebrx admin adduser admin
```

Config files live at `/opt/openwebrx/` on the Pi and are volume-mounted into the container.

### Pre-configured band profiles

| Profile | Frequency range | Modulation |
|---------|----------------|------------|
| FM Broadcast | 88–108 MHz | WFM |
| NOAA Weather | 162.4–162.55 MHz | NFM |
| 2m Ham | 144–148 MHz | NFM |
| 70cm Ham | 420–450 MHz | NFM |
| GMRS/FRS | 462–467 MHz | NFM |
| Air Band | 118–137 MHz | AM |
| Marine VHF | 156–162 MHz | NFM |
| APRS | 144.39 MHz | Packet |

### Digital modes

FT8/FT4/WSPR/JT65 (wsjtx), APRS (direwolf), DMR/D-STAR/NXDN/P25 (digiham), M17 (m17-demod), POCSAG/FLEX (multimon-ng), FreeDV (codec2), CW, SSTV, AIS, NAVTEX.

## Testing with a real dongle (no flashing needed)

Plug in your RTL-SDR and test inside the built arm64 image. No sudo needed — the script launches itself inside a privileged Docker container:

```bash
# Interactive shell
./scripts/test-with-usb.sh

# Run a specific command
./scripts/test-with-usb.sh rtl_test -t

# Run the full check script
./scripts/test-with-usb.sh check-sdr.sh
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

# OpenWebRX+ admin (optional — create manually if blank)
OPENWEBRX_ADMIN_PASSWORD=changeme
```

Build-time options (env vars passed to `docker compose run`):

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPRESS` | `true` | Set `false` to skip xz compression |
| `DEBUG` | — | Set `1` for verbose build output (`set -x`) |

## How the build works

1. Docker container (Ubuntu 24.04) with QEMU user-mode emulation
2. Downloads pinned Raspberry Pi OS Trixie arm64 lite image (cached after first download)
3. Expands the root partition by 4 GB
4. Mounts via `kpartx` and chroots with QEMU aarch64
5. `apt install rtl-sdr soapysdr-tools openwebrx ...` (no source compilation)
6. Installs Tailscale, decoders, DVB blacklist, WiFi config, bookmarks, test scripts
7. Cleans up and compresses to `.img.xz`

To rebuild, remove the output image and re-run. The downloaded base image is cached:

```bash
rm data/pi-sdr-base.img
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

## Related projects

- [signal-logs](https://github.com/mihow/signal-logs) — passive radio monitoring with AI-powered transcription and summarization
- [pi-radio-station](https://github.com/mihow/pi-radio-station) — full monitoring station with audio mixing
- [trunk-recorder](https://github.com/robotastic/trunk-recorder) — trunked radio system recorder
- [OpenWebRX+](https://www.openwebrx.de/) — the web SDR receiver included in this image
