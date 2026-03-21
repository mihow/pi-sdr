# pi-sdr — Next Session Context

## What was accomplished

Built a complete Raspberry Pi 5 SDR image builder with OpenWebRX+ web receiver. The system is Docker-based, cross-compiles for arm64 via QEMU, and produces a flashable `.img.xz`.

### v0.1.0 released (base image, no OpenWebRX+)
- RTL-SDR V4 validated with Trixie's packaged rtl-sdr 2.0.2 (no source compile needed)
- SoapySDR + Python bindings
- Tailscale with optional first-boot auth
- WiFi via NetworkManager keyfile
- DVB blacklist, test scripts (check-sdr.sh, tune.sh, scan.sh)
- GitHub Release: https://github.com/mihow/pi-sdr/releases/tag/v0.1.0

### feat/openwebrx branch (PR #1)
- OpenWebRX+ runs as Docker container (`slechev/openwebrxplus-softmbe`) — native apt install fails because Bookworm's `python3-csdr` requires Python < 3.12 but Trixie ships 3.13
- Docker CE installed in the Pi image via apt
- OpenWebRX+ Docker image (1.1 GB) pre-saved into Pi filesystem for offline first boot
- 8 RTL-SDR V4 band profiles: FM Broadcast, NOAA Weather, 2m Ham, 70cm Ham, GMRS/FRS, Air Band, Marine VHF, APRS
- 9 frequency bookmark files (public US FCC/ITU allocations)
- Auto HTTPS cert generation via Tailscale on first boot (browsers require HTTPS for web audio)
- Admin user: admin / picketfencing
- PR: https://github.com/mihow/pi-sdr/pull/1

### Validated
- OpenWebRX+ v1.2.96 running on beast with V4 dongle — waterfall + audio working via HTTPS + Tailscale cert
- All 8 band profiles working, all digital modes available in UI
- Audio confirmed working in Firefox (Chrome had issues — possibly a plugin conflict on user's machine)
- Tailscale container test: `pi-sdr-test` got its own Tailscale identity, generated a real cert, audio worked at `https://pi-sdr-test.wirehair-yo.ts.net:8073`

### Image patching workflow
Developed a fast patching approach — mount the raw .img via kpartx in a Docker container and write files directly. No full rebuild needed for WiFi, Tailscale key, SSH, hostname changes. Used this to patch:
- WiFi: ShinyObject / doingeasy
- Tailscale: reusable key
- SSH: pi / picketfencing
- Hostname: pi-sdr

## Current state

### Branches
- `main` — v0.1.0 base image (no OpenWebRX+)
- `feat/openwebrx` — OpenWebRX+ integration, PR #1 open

### Built images in data/
- Full rebuild in progress (COMPRESS=false) with SSH + SSL changes baked in
- Previous image deleted — was built before SSH/SSL fixes

### Docker E2E test validated
- Tailscale auth, HTTPS cert (Let's Encrypt), SSH with password, OpenWebRX+ waterfall + audio all working
- Test compose: `docker-compose.test.yml` (Tailscale sidecar + OpenWebRX+ sharing network)
- HTTPS cert needs retry on first boot — ACME auth can lag on new hostnames (entrypoint retries 3x)

### Stale Tailscale nodes to clean up
- Delete from https://login.tailscale.com/admin/machines:
  - pi-sdr-test, pi-sdr-test-1, pi-sdr-test-2, pi-sdr-test-3 (test containers)
  - pi-sdr (100.85.90.10, old Pi attempt)

## What needs to happen next

### Immediate
1. **Wait for rebuild to finish** — verify SSH and SSL are baked in
2. **E2E test the new image** in Docker before flashing
3. **Flash to Pi** and verify first-boot: SSH, Tailscale, OpenWebRX+, audio
4. **Merge PR #1** once Pi test passes

### Build improvements to add
- Add a `scripts/patch-image.sh` script to make the manual patching workflow reusable
- Consider whether the `COMPRESS` env var should default to `false` for dev and only compress in CI

### Known issues
- `test-with-usb.sh` Docker re-exec mangles complex shell commands (`bash -c "..."`) — works fine for single commands
- Chrome audio issue on user's machine (works in Firefox, works in headless Chrome) — likely a browser plugin conflict
- The `.img.xz` doesn't include patches — need to recompress after patching, or always flash from raw `.img`
- GitHub Actions `docker pull --platform linux/arm64` for the pre-save step needs the Docker socket mounted — verify this works in CI

## Key technical decisions

| Decision | Rationale |
|----------|-----------|
| Trixie not Bookworm | Latest Pi OS, rtl-sdr 2.0.2 with V4 support in apt |
| OpenWebRX+ via Docker not apt | python3-csdr needs Python < 3.12, Trixie has 3.13 |
| Pre-save Docker image in Pi filesystem | Offline first boot, no 1GB download needed |
| HTTPS via Tailscale cert | Browsers block AudioContext on HTTP (no sound without HTTPS) |
| Reusable Tailscale keys | One-time keys get consumed before first-boot reboot completes |
| kpartx not losetup -P | Docker doesn't create /dev/loopXpN partition devices |

## Credentials (in .env, gitignored)

- WiFi: ShinyObject / doingeasy
- SSH: pi / picketfencing
- OpenWebRX admin: admin / picketfencing
- Tailscale key: tskey-auth-k4eaQAY5cM11CNTRL-ERhu1rdh7yMzMfaueGHCxMkzwUoEQjpg1 (reusable)

## File structure

```
pi-sdr/
├── Dockerfile                          # Build container (Ubuntu 24.04 + qemu + docker.io)
├── docker-compose.yml                  # binfmt + build service (mounts Docker socket)
├── .env / .env.example                 # Runtime config (WiFi, Tailscale, OpenWebRX admin)
├── scripts/
│   ├── build-image.sh                  # Download, resize, mount, chroot, pre-pull Docker image, compress
│   ├── provision.sh                    # In-chroot: apt install, Docker CE, OpenWebRX config, Tailscale
│   ├── check-sdr.sh                    # Device/driver check script
│   └── test-with-usb.sh               # Docker-wrapped chroot with USB passthrough
├── config/
│   ├── system/blacklist-sdr.conf       # DVB kernel module blacklist
│   └── openwebrx/
│       ├── openwebrx.conf              # Core config
│       ├── settings.json               # SDR profiles (8 bands)
│       └── bookmarks.d/*.json          # 9 bookmark files
├── test-scripts/                       # Deployed to /usr/local/bin/ on Pi
│   ├── tune.sh, scan.sh, check-sdr.sh
├── .github/workflows/build.yml        # CI: shellcheck + build + release on tag push
├── docs/
│   ├── NEXT_SESSION_PROMPT.md          # This file
│   └── superpowers/plans/              # Implementation plans
└── data/                               # Build output (gitignored)
```

## References

- Prior repo: /home/michael/Projects/Radio/pi-radio-monitor-research/
- OpenWebRX+ on middle-earth: v1.2.94, apt from luarvique.github.io/ppa, running on port 8073
- OpenWebRX+ Docker images: slechev/openwebrxplus-softmbe (arm64 + amd64)
- Tailscale admin: https://login.tailscale.com/admin
- RTL-SDR Blog V4: 0bda:2838, R828D tuner, 29 gain values (0.0–49.6 dB)
