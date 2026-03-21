# OpenWebRX+ Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OpenWebRX+ to the pi-sdr image so it boots as a web-accessible SDR receiver with waterfall, digital mode decoders, and curated frequency bookmarks.

**Architecture:** OpenWebRX+ is installed from the official apt repo (`luarvique.github.io/ppa`) during provisioning, configured with a default RTL-SDR V4 profile and useful VHF/UHF presets. Bookmarks are organized as JSON files in `/etc/openwebrx/bookmarks.d/`. Digital voice decoders (codec2, direwolf, wsjtx, m17-demod, multimon-ng) are installed for APRS, DMR, M17, POCSAG, and WSJT-X modes. The service starts on boot at port 8073, accessible over Tailscale or LAN.

**Tech Stack:** OpenWebRX+ v1.2.x, RTL-SDR via SoapySDR, codec2, direwolf, wsjtx, m17-demod, multimon-ng

**Branch:** `feat/openwebrx` off `main`

**Reference installation:** `middle-earth` host has a working OpenWebRX+ v1.2.94 install. Key patterns:
- Apt source: `https://luarvique.github.io/ppa/ubuntu ./`
- Config: `/etc/openwebrx/openwebrx.conf` (INI) + settings managed via web admin UI
- Bookmarks: JSON files in `/etc/openwebrx/bookmarks.d/<category>.json`
- SDR profiles: `settings.json` with `type: "rtl_sdr"`, gain, sample rate, center freq per band
- Decoders installed as separate apt packages

---

## File Structure

```
pi-sdr/
├── scripts/
│   └── provision.sh                          # Modify: add OpenWebRX+ install + decoders
├── config/
│   ├── system/
│   │   └── blacklist-sdr.conf                # (exists, unchanged)
│   └── openwebrx/
│       ├── openwebrx.conf                    # Core config (port, logging, APRS symbols)
│       ├── settings.json                     # SDR device profiles (RTL-SDR V4 presets)
│       └── bookmarks.d/
│           ├── noaa-weather.json             # NOAA Weather Radio (7 channels)
│           ├── gmrs.json                     # GMRS simplex + repeater channels
│           ├── frs.json                      # FRS channels
│           ├── murs.json                     # MURS channels
│           ├── aviation.json                 # Air band emergency + common freqs
│           ├── marine-vhf.json               # Marine VHF ch16, ch9, etc.
│           ├── fm-broadcast.json             # FM broadcast band markers
│           ├── ham-2m.json                   # 2m band plan markers (national, not local repeaters)
│           └── ham-70cm.json                 # 70cm band plan markers
├── README.md                                 # Modify: add OpenWebRX+ section
└── .env.example                              # Modify: add OPENWEBRX_ADMIN_PASSWORD
```

---

### Task 1: Create branch and OpenWebRX+ config files

**Files:**
- Create: `config/openwebrx/openwebrx.conf`
- Create: `config/openwebrx/settings.json`
- Modify: `.env.example`

- [ ] **Step 1: Create feature branch**

```bash
git checkout -b feat/openwebrx
```

- [ ] **Step 2: Create `config/openwebrx/openwebrx.conf`**

Minimal INI config. Most settings are managed via the web admin UI and stored in `settings.json`.

```ini
[core]
data_directory = /var/lib/openwebrx
temporary_directory = /tmp
log_level = INFO

[web]
port = 8073
ipv6 = true

[aprs]
symbols_path = /usr/share/aprs-symbols/png
```

- [ ] **Step 3: Create `config/openwebrx/settings.json`**

Default RTL-SDR V4 profiles for common bands. Users can add more via the web admin UI.

```json
{
    "version": 7,
    "sdrs": {
        "rtlsdr": {
            "name": "RTL-SDR Blog V4",
            "type": "rtl_sdr",
            "profiles": {
                "fm-broadcast": {
                    "name": "FM Broadcast",
                    "center_freq": 97500000,
                    "samp_rate": 2400000,
                    "start_freq": 97500000,
                    "start_mod": "wfm",
                    "rf_gain": 29
                },
                "noaa-weather": {
                    "name": "NOAA Weather",
                    "center_freq": 162475000,
                    "samp_rate": 2400000,
                    "start_freq": 162400000,
                    "start_mod": "nfm",
                    "rf_gain": 29
                },
                "2m": {
                    "name": "2m Ham",
                    "center_freq": 146000000,
                    "samp_rate": 2400000,
                    "start_freq": 146520000,
                    "start_mod": "nfm",
                    "rf_gain": 29
                },
                "70cm": {
                    "name": "70cm Ham",
                    "center_freq": 446000000,
                    "samp_rate": 2400000,
                    "start_freq": 446000000,
                    "start_mod": "nfm",
                    "rf_gain": 29
                },
                "gmrs": {
                    "name": "GMRS/FRS",
                    "center_freq": 462562500,
                    "samp_rate": 2400000,
                    "start_freq": 462562500,
                    "start_mod": "nfm",
                    "rf_gain": 29
                },
                "airband": {
                    "name": "Air Band",
                    "center_freq": 127000000,
                    "samp_rate": 2400000,
                    "start_freq": 121500000,
                    "start_mod": "am",
                    "rf_gain": 29
                },
                "marine": {
                    "name": "Marine VHF",
                    "center_freq": 157000000,
                    "samp_rate": 2400000,
                    "start_freq": 156800000,
                    "start_mod": "nfm",
                    "rf_gain": 29
                },
                "aprs": {
                    "name": "APRS",
                    "center_freq": 144500000,
                    "samp_rate": 2400000,
                    "start_freq": 144390000,
                    "start_mod": "packet",
                    "rf_gain": 29
                }
            }
        }
    },
    "receiver_name": "pi-sdr",
    "receiver_location": "",
    "receiver_asl": 0,
    "receiver_admin": "",
    "receiver_gps": {
        "lat": 0,
        "lon": 0
    },
    "max_clients": 10,
    "waterfall_scheme": "GoogleTurboWaterfall",
    "fft_fps": 9,
    "fft_size": 4096,
    "fft_voverlap_factor": 0.3,
    "waterfall_levels": {
        "min": -88.0,
        "max": -20.0
    },
    "audio_compression": "adpcm",
    "fft_compression": "adpcm",
    "squelch_auto_margin": 10,
    "digital_voice_dmr_id_lookup": true,
    "digital_voice_nxdn_id_lookup": true,
    "decoding_queue_workers": 2,
    "decoding_queue_length": 10,
    "wsjt_decoding_depth": 3,
    "js8_enabled_profiles": ["normal", "slow"],
    "fst4_enabled_intervals": [15, 30],
    "fst4w_enabled_intervals": [120, 300],
    "q65_enabled_combinations": ["A30", "E120"]
}
```

- [ ] **Step 4: Add `OPENWEBRX_ADMIN_PASSWORD` to `.env.example`**

Append to `.env.example`:

```bash

# --- OpenWebRX+ ---
# Admin password for the web UI (required for first login)
# If not set, you'll need to create a user manually: openwebrx admin adduser admin
OPENWEBRX_ADMIN_PASSWORD=
```

- [ ] **Step 5: Commit**

```bash
git add config/openwebrx/ .env.example
git commit -m "feat: add OpenWebRX+ config and RTL-SDR V4 profiles"
```

---

### Task 2: Create frequency bookmarks

**Files:**
- Create: `config/openwebrx/bookmarks.d/noaa-weather.json`
- Create: `config/openwebrx/bookmarks.d/gmrs.json`
- Create: `config/openwebrx/bookmarks.d/frs.json`
- Create: `config/openwebrx/bookmarks.d/murs.json`
- Create: `config/openwebrx/bookmarks.d/aviation.json`
- Create: `config/openwebrx/bookmarks.d/marine-vhf.json`
- Create: `config/openwebrx/bookmarks.d/fm-broadcast.json`
- Create: `config/openwebrx/bookmarks.d/ham-2m.json`
- Create: `config/openwebrx/bookmarks.d/ham-70cm.json`

All bookmarks use the OpenWebRX+ JSON format — an array of `{"name", "frequency", "modulation"}` objects. Frequencies are in Hz. Only include publicly-documented, nationally-applicable frequencies (no local repeaters or personal data).

**Bookmark sources (all public FCC/ITU allocations):**

- NOAA Weather: 162.400–162.550 MHz (7 channels)
- GMRS: 462.5625–462.7250 MHz simplex (channels 1-7), 462.550–462.725 MHz repeater outputs (channels 15R-22R)
- FRS: 462.5625–467.7125 MHz (22 channels)
- MURS: 151.820–154.600 MHz (5 channels)
- Aviation: 121.5 MHz emergency, 122.75 MHz air-to-air, 123.025 MHz helicopter, 243.0 MHz military emergency
- Marine VHF: Ch 16 (156.800 MHz), Ch 9 (156.450 MHz), Ch 13 (156.650 MHz), Ch 70 (156.525 MHz DSC)
- FM Broadcast: band markers every 2 MHz from 88–108 MHz
- Ham 2m: 144.390 (APRS), 146.520 (national simplex), 146.940, 147.000 band plan markers
- Ham 70cm: 446.000 (national simplex), 446.500, band plan markers

- [ ] **Step 1: Create all bookmark JSON files**

Each file is a self-contained JSON array. Keep them focused on one service per file.

- [ ] **Step 2: Verify JSON validity**

```bash
for f in config/openwebrx/bookmarks.d/*.json; do python3 -m json.tool "$f" > /dev/null && echo "OK: $f" || echo "FAIL: $f"; done
```

- [ ] **Step 3: Commit**

```bash
git add config/openwebrx/bookmarks.d/
git commit -m "feat: add frequency bookmarks for common US radio services"
```

---

### Task 3: Update provision.sh to install OpenWebRX+ and decoders

**Files:**
- Modify: `scripts/provision.sh`

**Reference:** `middle-earth` uses apt source `https://luarvique.github.io/ppa/ubuntu ./` with the OpenWebRX+ GPG key. Installed packages: `openwebrx`, `codec2`, `direwolf`, `wsjtx`, `m17-demod`, `multimon-ng`.

- [ ] **Step 1: Add OpenWebRX+ apt repo and install**

Add after the SDR packages section in `provision.sh`:

1. Install prerequisites: `apt-get install -y gnupg`
2. Add the OpenWebRX+ GPG key and apt source (for Trixie/Debian — check if they have a debian repo or if the ubuntu one works)
3. `apt-get update`
4. `apt-get install -y openwebrx`
5. Install digital mode decoders: `apt-get install -y codec2 direwolf wsjtx m17-demod multimon-ng`
6. Install APRS symbols: `apt-get install -y aprs-symbols` (or clone the repo if not packaged)

- [ ] **Step 2: Deploy OpenWebRX+ config files**

Add after the decoder install:

1. Copy `config/openwebrx/openwebrx.conf` → `/etc/openwebrx/openwebrx.conf`
2. Copy `config/openwebrx/settings.json` → `/var/lib/openwebrx/settings.json`
3. Copy `config/openwebrx/bookmarks.d/` → `/etc/openwebrx/bookmarks.d/`
4. Set ownership: `chown -R openwebrx:openwebrx /var/lib/openwebrx/`

Note: use `/opt/provision/config/openwebrx/` as the source path (files are copied into chroot by build-image.sh).

- [ ] **Step 3: Create admin user if password provided**

```bash
if [[ -n "${OPENWEBRX_ADMIN_PASSWORD:-}" ]]; then
    echo "=== Create OpenWebRX admin user ==="
    echo "$OPENWEBRX_ADMIN_PASSWORD" | openwebrx admin adduser --noninteractive admin
fi
```

- [ ] **Step 4: Enable OpenWebRX service**

```bash
systemctl enable openwebrx.service || ln -sf \
    /lib/systemd/system/openwebrx.service \
    /etc/systemd/system/multi-user.target.wants/openwebrx.service
```

Note: `systemctl enable` may fail in chroot (no systemd running) — the symlink fallback handles this.

- [ ] **Step 5: Commit**

```bash
git add scripts/provision.sh
git commit -m "feat: install OpenWebRX+ with decoders and config in provision.sh"
```

---

### Task 4: Update build-image.sh and docker-compose.yml

**Files:**
- Modify: `scripts/build-image.sh`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Pass OPENWEBRX_ADMIN_PASSWORD through chroot env**

In `scripts/build-image.sh`, add to the `CHROOT_ENV` array building section (around line 294):

```bash
[[ -n "${OPENWEBRX_ADMIN_PASSWORD:-}" ]] && CHROOT_ENV+=("OPENWEBRX_ADMIN_PASSWORD=${OPENWEBRX_ADMIN_PASSWORD}")
```

- [ ] **Step 2: Add env var to docker-compose.yml**

Add `OPENWEBRX_ADMIN_PASSWORD` to the `environment` list in the `build` service.

- [ ] **Step 3: Expand image size from +2GB to +3GB**

OpenWebRX+ and decoders add ~500MB. Change `EXPAND_GB=2` to `EXPAND_GB=3` in build-image.sh.

- [ ] **Step 4: Commit**

```bash
git add scripts/build-image.sh docker-compose.yml
git commit -m "feat: pass OpenWebRX admin password and expand image to 3GB"
```

---

### Task 5: Update README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add OpenWebRX+ to the "What's included" table**

Add rows for OpenWebRX+, codec2, direwolf, wsjtx, m17-demod, multimon-ng.

- [ ] **Step 2: Add "Web UI" section after "Testing with a real dongle"**

```markdown
## Web UI (OpenWebRX+)

After flashing and booting, OpenWebRX+ starts automatically on port 8073:

- **Local:** `http://<pi-ip>:8073`
- **Tailscale:** `http://<pi-tailscale-name>:8073`

### First-time setup

1. Log in to the admin panel at `http://<pi-ip>:8073/settings`
2. Set your receiver name, location, and GPS coordinates
3. Adjust RTL-SDR gain for your antenna and environment
4. Add local repeater frequencies to bookmarks

If you set `OPENWEBRX_ADMIN_PASSWORD` in `.env` before building, the admin user is pre-created.
Otherwise, create one via SSH:

    openwebrx admin adduser admin

### Pre-configured profiles

The image ships with RTL-SDR V4 profiles for:
- FM Broadcast (88–108 MHz)
- NOAA Weather Radio (162.4–162.55 MHz)
- 2m Ham (144–148 MHz) with APRS decoding
- 70cm Ham (420–450 MHz)
- GMRS/FRS (462–467 MHz)
- Air Band (118–137 MHz)
- Marine VHF (156–162 MHz)

### Supported digital modes

DMR, D-STAR, NXDN, M17 (via codec2), APRS (via direwolf), POCSAG, FT8/FT4/WSPR (via wsjtx), JS8Call.
```

- [ ] **Step 3: Add OPENWEBRX_ADMIN_PASSWORD to the Configuration section**

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: add OpenWebRX+ web UI and digital mode documentation"
```

---

### Task 6: Integration test — build and verify OpenWebRX+

- [ ] **Step 1: Build the image**

```bash
rm -f data/pi-sdr-base.img
COMPRESS=false docker compose run --rm build
```

- [ ] **Step 2: Verify OpenWebRX+ is installed in the image**

```bash
./scripts/test-with-usb.sh /usr/bin/openwebrx --version
```

Expected: `OpenWebRX+ version v1.2.x`

- [ ] **Step 3: Verify decoders installed**

```bash
./scripts/test-with-usb.sh which codec2 direwolf wsprd m17-demod multimon-ng
```

- [ ] **Step 4: Verify config files deployed**

```bash
./scripts/test-with-usb.sh ls /etc/openwebrx/bookmarks.d/
./scripts/test-with-usb.sh cat /var/lib/openwebrx/settings.json
```

- [ ] **Step 5: Verify service is enabled**

```bash
./scripts/test-with-usb.sh ls /etc/systemd/system/multi-user.target.wants/
```

Expected: `openwebrx.service` in the list.

- [ ] **Step 6: Verify SDR detection through OpenWebRX**

```bash
./scripts/test-with-usb.sh check-sdr.sh
```

Expected: All checks pass (same as before — OpenWebRX uses SoapySDR which we already validated).

- [ ] **Step 7: Commit any fixes**

- [ ] **Step 8: Create PR**

```bash
git push -u origin feat/openwebrx
gh pr create --title "feat: add OpenWebRX+ web SDR receiver" --body "..."
```
