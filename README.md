# pi-sdr

Minimal Raspberry Pi 5 image builder for Software-Defined Radio. Produces a flashable image with RTL-SDR V4 drivers and Tailscale pre-installed.

## Quick start

### Download pre-built image

Grab `pi-sdr.img.xz` from the [latest release](../../releases/latest), then flash with [Raspberry Pi Imager](https://www.raspberrypi.com/software/) (recommended) or:

```bash
xz -d pi-sdr.img.xz
sudo dd if=pi-sdr.img of=/dev/sdX bs=4M status=progress
```

### Build from source

```bash
# Optional: configure WiFi and Tailscale
cp .env.example .env
# Edit .env with your settings

# Build (~20 min on first run)
docker compose run --rm build

# Output: data/pi-sdr.img.xz
```

## What's included

| Component | Version | Notes |
|-----------|---------|-------|
| Raspberry Pi OS | Trixie arm64 lite | Debian 13, pinned release |
| rtl-sdr | 2.0.2 | RTL-SDR Blog V4 support included |
| SoapySDR | 0.8.1 | Universal SDR API |
| SoapyRTLSDR | — | SoapySDR module for RTL-SDR |
| Python 3 SoapySDR | — | `import SoapySDR` works |
| Tailscale | latest stable | SSH from anywhere, no port forwarding |

Also: DVB kernel module blacklist, udev rules for non-root USB access.

## Testing without flashing

Forward your RTL-SDR USB dongle into the image via Docker:

```bash
./scripts/test-with-usb.sh
```

Then inside the chroot:

```bash
# Detect dongle
rtl_test -t

# List SDR devices
SoapySDRUtil --find

# Tune to a frequency and record 10s of IQ samples
./test-scripts/tune.sh 462.5625e6 10

# Quick scanner test (requires rtl_airband — not included, install separately)
# rtl_airband -t -c /path/to/config.conf
```

## Configuration

Copy `.env.example` to `.env` and set:

```bash
# WiFi (optional)
WIFI_SSID=MyNetwork
WIFI_PASSWORD=secret
WIFI_COUNTRY=US

# Tailscale (optional — authenticate on first boot)
TAILSCALE_AUTHKEY=tskey-auth-...
```

## Building downstream projects

This image is designed as a foundation. Install additional software on top:

```bash
# SSH in via Tailscale
ssh pi@pi-sdr

# Example: install rtl_airband for scanning
sudo apt install libfftw3-dev libpulse-dev libconfig++-dev
git clone https://github.com/rtl-airband/RTLSDR-Airband.git
cd RTLSDR-Airband && mkdir build && cd build
cmake .. -DNFM=ON -DSOAPYSDR=ON && make -j4 && sudo make install

# Example: install PipeWire audio stack
sudo apt install pipewire pipewire-pulse wireplumber
```

See [pi-radio-station](https://github.com/mihow/pi-radio-station) for a full monitoring station build.
