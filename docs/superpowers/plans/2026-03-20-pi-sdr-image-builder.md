# pi-sdr Image Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Docker-based image builder that produces a flashable `.img.xz` for Raspberry Pi 5 with RTL-SDR V4 drivers, SoapySDR, and Tailscale — all from Trixie packages (no source compilation).

**Architecture:** Docker container with QEMU user-mode emulation downloads a pinned Raspberry Pi OS Trixie arm64 lite image, expands it, chroots in, installs packages via apt, configures Tailscale/WiFi/blacklist, then compresses the result. The build pipeline is adapted from pi-radio-monitor-research but dramatically simplified — Trixie's packaged rtl-sdr works with the V4 dongle (validated on host with 2.0.1; Trixie ships 2.0.2), eliminating all source compilation.

**Tech Stack:** Docker, QEMU binfmt, kpartx, Raspberry Pi OS Trixie arm64, rtl-sdr, SoapySDR, Tailscale, GitHub Actions

**Prior art:** `/home/michael/Projects/Radio/pi-radio-monitor-research/` — Bookworm-based build with source compilation and full audio stack. We reuse the Docker/QEMU/kpartx patterns but strip everything else.

**Critical patterns from prior repo (do not omit):**
- Copy scripts/config into chroot before `chroot` call (chroot can't see Docker mounts)
- Copy host `/etc/resolv.conf` into chroot for DNS; restore symlink after provisioning
- `DEBIAN_FRONTEND=noninteractive` in provision.sh
- `ld.so.preload` restoration must be in the cleanup trap (not just happy path)

---

## File Structure

```
pi-sdr/
├── Dockerfile                          # Build container with host tools
├── docker-compose.yml                  # binfmt + build services
├── .env.example                        # (exists) Documented env vars
├── .gitignore                          # (exists) data/, *.img, *.img.xz, .env
├── scripts/
│   ├── build-image.sh                  # Download, resize, mount, chroot, compress
│   ├── provision.sh                    # apt install + Tailscale + blacklist + test scripts
│   ├── check-sdr.sh                    # (exists) Device/driver check
│   └── test-with-usb.sh               # Docker chroot with USB passthrough
├── config/
│   └── system/
│       └── blacklist-sdr.conf          # DVB kernel module blacklist
├── test-scripts/                       # Deployed into the Pi image at /usr/local/bin/
│   ├── tune.sh                         # Tune to freq, record IQ samples
│   ├── scan.sh                         # Quick frequency range scan
│   └── check-sdr.sh                    # Copy of scripts/check-sdr.sh for on-Pi use
└── .github/
    └── workflows/
        └── build.yml                   # CI: build + publish .img.xz on tag push
```

---

### Task 1: Blacklist config

**Files:**
- Create: `config/system/blacklist-sdr.conf`

Note: `.gitignore` and `.env.example` already exist and are correct.

- [ ] **Step 1: Create config/system/blacklist-sdr.conf**

```
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2832_sdr
blacklist sdr_msi3101
blacklist msi001
blacklist msi2500
```

- [ ] **Step 2: Commit**

```bash
git add config/system/blacklist-sdr.conf
git commit -m "chore: add DVB kernel module blacklist for RTL-SDR"
```

---

### Task 2: Dockerfile and docker-compose.yml

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`

**Reference:** `/home/michael/Projects/Radio/pi-radio-monitor-research/Dockerfile` and `docker-compose.yml`

- [ ] **Step 1: Create Dockerfile**

```dockerfile
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    qemu-user-static \
    qemu-utils \
    parted \
    e2fsprogs \
    wget \
    xz-utils \
    dosfstools \
    kpartx \
    udev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY scripts/ /build/scripts/
COPY config/ /build/config/
COPY test-scripts/ /build/test-scripts/

ENTRYPOINT ["/build/scripts/build-image.sh"]
```

Note: `curl` added for Tailscale install script download during provisioning.

- [ ] **Step 2: Create docker-compose.yml**

```yaml
services:
  binfmt:
    image: multiarch/qemu-user-static
    command: ["--reset", "-p", "yes"]
    privileged: true

  build:
    build: .
    privileged: true
    depends_on:
      binfmt:
        condition: service_completed_successfully
    env_file:
      - path: .env
        required: false
    environment:
      - TAILSCALE_AUTHKEY
      - WIFI_SSID
      - WIFI_PASSWORD
      - WIFI_COUNTRY
    volumes:
      - ./data:/build/data
      - ./scripts:/build/scripts
      - ./config:/build/config
      - ./test-scripts:/build/test-scripts
```

- [ ] **Step 3: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "feat: add Dockerfile and docker-compose for image build"
```

---

### Task 3: build-image.sh — download, resize, mount, chroot, compress

**Files:**
- Create: `scripts/build-image.sh`

**Reference:** `/home/michael/Projects/Radio/pi-radio-monitor-research/scripts/build-image.sh` (263 lines). Start from that file and adapt:
- Trixie image URL: `https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-12-04/2025-12-04-raspios-trixie-arm64-lite.img.xz`
- +2GB expansion instead of +4GB (no audio stack)
- Same WiFi config via NetworkManager keyfile

**Critical patterns to preserve from prior repo:**
- `kpartx -av` for partition mapping in Docker (not `losetup -P`)
- Disable `ld.so.preload` before chroot, **restore in cleanup trap** (not just happy path)
- Bind-mount `/proc`, `/sys`, `/dev`, `/dev/pts` for chroot
- Copy `qemu-aarch64-static` into chroot
- **Copy `/etc/resolv.conf` into chroot for DNS resolution** (prior repo lines 174-179)
- **Restore `resolv.conf` symlink after provisioning** (prior repo lines 232-234)
- **Copy scripts, config, and test-scripts into chroot** at e.g. `/opt/provision/` before the chroot call — the chroot cannot see Docker bind mounts
- WiFi config writes NetworkManager keyfile to `/etc/NetworkManager/system-connections/`

- [ ] **Step 1: Write build-image.sh**

The script should:
1. Set variables: `IMAGE_URL`, `IMAGE_FILE`, `IMAGE_DATE=2025-12-04`, `EXPAND_GB=2`
2. Support `DEBUG=1` env var to enable `set -x`
3. `mkdir -p /build/data`
4. Download image if not cached in `/build/data/`
5. Decompress `.img.xz` → `.img` (working copy)
6. Expand with `truncate` + `parted resizepart` + `e2fsck`/`resize2fs`
7. `losetup --find --show` then `kpartx -av` to get `/dev/mapper/loopXp1` (boot) and `loopXp2` (root)
8. Mount root to `/mnt/pi`, boot to `/mnt/pi/boot/firmware`
9. **Copy provision scripts into chroot:** `/opt/provision/provision.sh`, `/opt/provision/config/`, `/opt/provision/test-scripts/`
10. Disable `ld.so.preload` (comment out lines)
11. Copy `qemu-aarch64-static` into chroot
12. Bind-mount `/proc`, `/sys`, `/dev`, `/dev/pts`
13. **Copy `/etc/resolv.conf` into chroot** (backup original first)
14. If `WIFI_SSID` set, write NetworkManager keyfile
15. Build `CHROOT_ENV` array with `DEBIAN_FRONTEND=noninteractive` + optional `TAILSCALE_AUTHKEY`
16. `chroot /mnt/pi env -i "${CHROOT_ENV[@]}" /opt/provision/provision.sh`
17. **Cleanup trap** must handle: unmount bind mounts, **restore `ld.so.preload`**, **restore `resolv.conf` symlink**, remove `/opt/provision/`, `kpartx -dv`, `losetup -d`
18. Compress to `.img.xz` in `/build/data/` (support `COMPRESS=false` env var to skip during development)

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/build-image.sh
```

- [ ] **Step 3: Commit**

```bash
git add scripts/build-image.sh
git commit -m "feat: add build-image.sh for Trixie arm64 image building"
```

---

### Task 4: provision.sh — apt install packages + Tailscale + config

**Files:**
- Create: `scripts/provision.sh`

**Reference:** `/home/michael/Projects/Radio/pi-radio-monitor-research/scripts/provision.sh` (261 lines). Massively simplified — no source compilation, no audio stack.

**Critical patterns to preserve:**
- `export DEBIAN_FRONTEND=noninteractive`
- `policy-rc.d` returning 101 (prevent service hangs in chroot)
- `dpkg --configure -a` recovery before apt
- `MODULES=most` in initramfs-tools conf
- Tailscale install from official apt repo
- Use correct Trixie/Debian codename for Tailscale repo (verify at `https://pkgs.tailscale.com/stable/debian/`)

- [ ] **Step 1: Write provision.sh**

The script should:
1. `set -euo pipefail`, `export DEBIAN_FRONTEND=noninteractive`
2. Run as root check
3. Write `/usr/sbin/policy-rc.d` returning 101
4. Fix initramfs-tools: set `MODULES=most` in `/etc/initramfs-tools/initramfs.conf`
5. `dpkg --configure -a` recovery
6. `apt-get update`
7. `apt-get install -y rtl-sdr soapysdr-tools soapysdr-module-rtlsdr python3-soapysdr`
8. Install DVB blacklist: `cp /opt/provision/config/system/blacklist-sdr.conf /etc/modprobe.d/`
9. Install Tailscale: add apt repo and install package (use `curl https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg` — fall back to Bookworm GPG if Trixie-specific doesn't exist yet)
10. If `TAILSCALE_AUTHKEY` env var is set, create a first-boot systemd service:
    - `/etc/systemd/system/tailscale-firstboot.service`
    - Runs `tailscale up --authkey=$KEY` then disables itself
    - `systemctl enable tailscale-firstboot.service`
11. Deploy test scripts: copy `/opt/provision/test-scripts/*` to `/usr/local/bin/`
12. Clean apt cache: `apt-get clean && rm -rf /var/lib/apt/lists/*`
13. Remove policy-rc.d

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/provision.sh
```

- [ ] **Step 3: Commit**

```bash
git add scripts/provision.sh
git commit -m "feat: add provision.sh for Trixie SDR package installation"
```

---

### Task 5: test-with-usb.sh — Docker chroot with USB passthrough

**Files:**
- Create: `scripts/test-with-usb.sh`

**Reference:** `/home/michael/Projects/Radio/pi-radio-monitor-research/scripts/test-with-usb.sh` (120 lines). Simplify: remove audio socket passthrough, keep USB forwarding and chroot.

- [ ] **Step 1: Write test-with-usb.sh**

The script should:
1. Require root or fail with helpful message
2. Find the latest `.img` in `data/` (not `.img.xz`)
3. `losetup` + `kpartx` to mount it
4. Mount root + boot partitions to `/mnt/pi`
5. Disable `ld.so.preload`
6. Copy `qemu-aarch64-static` into chroot
7. Bind-mount `/dev/bus/usb`, `/proc`, `/sys`, `/dev`, `/dev/pts`
8. Copy `/etc/resolv.conf` into chroot for DNS
9. `chroot` into the image — run `$@` if args provided, else interactive shell
10. **Trap-based cleanup:** unmount everything, restore `ld.so.preload`, restore `resolv.conf`, `kpartx -dv`, `losetup -d`

Usage: `sudo ./scripts/test-with-usb.sh` for interactive, or `sudo ./scripts/test-with-usb.sh rtl_test -t`

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/test-with-usb.sh
```

- [ ] **Step 3: Commit**

```bash
git add scripts/test-with-usb.sh
git commit -m "feat: add test-with-usb.sh for live SDR testing in chroot"
```

---

### Task 6: Test scripts — tune.sh, scan.sh, and on-Pi check-sdr.sh

**Files:**
- Create: `test-scripts/tune.sh`
- Create: `test-scripts/scan.sh`
- Create: `test-scripts/check-sdr.sh` (copy from `scripts/check-sdr.sh`)

- [ ] **Step 1: Create test-scripts/tune.sh**

The script should:
1. Accept args: frequency (required), `-s` sample rate (default 2048000), `-d` duration seconds (default 5), `-o` output file (default `/tmp/iq_capture.bin`)
2. Validate frequency argument is provided
3. Calculate sample count: `rate * duration`
4. Run `rtl_sdr -f <freq> -s <rate> -n <samples> <outfile>`
5. Print capture summary: freq, rate, duration, file size

- [ ] **Step 2: Create test-scripts/scan.sh**

The script should:
1. Accept args: start freq (required), end freq (required), `-b` bin size (default 1M), `-i` interval (default 1s), `-n` num sweeps (default 1)
2. Run `rtl_power -f <start>:<end>:<bin> -i <interval> -1` for a single sweep, or `-n <num>` for multiple
3. Output CSV to stdout or `-o` file
4. Print summary: freq range, bin count, sweep count

- [ ] **Step 3: Copy check-sdr.sh for on-Pi use**

```bash
cp scripts/check-sdr.sh test-scripts/check-sdr.sh
```

- [ ] **Step 4: Make executable**

```bash
chmod +x test-scripts/tune.sh test-scripts/scan.sh test-scripts/check-sdr.sh
```

- [ ] **Step 5: Commit**

```bash
git add test-scripts/
git commit -m "feat: add tune.sh, scan.sh, and check-sdr.sh test scripts for Pi image"
```

---

### Task 7: GitHub Actions workflow — build + release on tag push

**Files:**
- Create: `.github/workflows/build.yml`

- [ ] **Step 1: Write build.yml**

The workflow should:
1. Trigger on tag push (`v*`)
2. Run on `ubuntu-latest`
3. Set up QEMU binfmt with `docker/setup-qemu-action`
4. Set up Docker Buildx with `docker/setup-buildx-action`
5. `docker compose run --rm build`
6. Run `shellcheck scripts/*.sh test-scripts/*.sh` as a lint step
7. Upload `data/*.img.xz` as release artifact via `softprops/action-gh-release`

No secrets needed for the build itself. Tailscale/WiFi are runtime config, not build-time.

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/build.yml
git commit -m "ci: add GitHub Actions workflow for image build and release"
```

---

### Task 8: Integration test — full build + USB test

- [ ] **Step 1: Run the full build**

```bash
docker compose run --rm build
```

Expected: Produces `data/*.img.xz` — the compressed Trixie SDR image.

- [ ] **Step 2: Verify image size**

```bash
ls -lh data/*.img.xz
```

Expected: Under 1GB compressed.

- [ ] **Step 3: Test with USB dongle**

```bash
sudo ./scripts/test-with-usb.sh check-sdr.sh
```

Expected: All checks pass inside the chroot (rtl_test detects V4, SoapySDR finds device).

- [ ] **Step 4: Test tune.sh**

```bash
sudo ./scripts/test-with-usb.sh tune.sh 462.5625e6 -d 2
```

Expected: Captures IQ samples from GMRS channel, prints summary.

- [ ] **Step 5: Test scan.sh**

```bash
sudo ./scripts/test-with-usb.sh scan.sh 460e6 470e6 -n 1
```

Expected: Single sweep across 460-470 MHz, CSV output.

- [ ] **Step 6: Run shellcheck on all scripts**

```bash
shellcheck scripts/*.sh test-scripts/*.sh
```

Expected: No errors (warnings acceptable).

- [ ] **Step 7: Commit any fixes from integration testing**
