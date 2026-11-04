# sdrscan

`sdrscan` is an unattended radio scanner for a Raspberry Pi 5 with an RTL-SDR Blog V4. It
sweeps the US band plan, finds and tracks transmissions, keeps short clips of speech under a
rolling storage quota, and summarizes the activity on the device. Once an hour it pushes a
digest of a few KB to an off-site static host. A phone-friendly dashboard reads that
digest, so looking at it costs the rig no data at all.

Design: [`docs/superpowers/specs/2026-11-03-sdr-scanner-design.md`](../docs/superpowers/specs/2026-11-03-sdr-scanner-design.md).
Plan: [`docs/superpowers/plans/2026-11-03-sdr-scanner.md`](../docs/superpowers/plans/2026-11-03-sdr-scanner.md).

## How it works

1. The scheduler sweeps the bands in 2.4 MHz windows and dwells on windows with active
   voice channels.
2. The detector finds peaks above each window's own noise floor. The tracker turns them into
   transmissions, keyed by integer Hz.
3. During a dwell, each channel is demodulated to 16 kHz audio. The voice gate classifies it
   as voice, data or noise. A voice transmission of at least 1.5 s keeps an 8 kbit/s Opus clip.
4. Optional speech-to-text (whisper.cpp) turns kept clips into short transcripts.
5. Hour, day and all-time rollups feed a rule-based digest: busiest channels, new, unusual
   and quiet channels, bands, recent transmissions and rig health.
6. The digest goes off site once an hour, within a monthly byte cap.

## Try it on a build host

```bash
cd scanner
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q -m "not slow"

# Simulate two hours of a synthetic scene and look at the dashboard
.venv/bin/sdrscan simulate --scene tests/scenes/week_basic.yaml --hours 2 --out build/sim
python3 -m http.server --bind 127.0.0.1 --directory build/sim 8000
# open http://127.0.0.1:8000/ (use the browser's phone view at 360 px wide)
```

`--spectrum hybrid` also renders IQ for dwells, so the simulation produces clips and
transcripts (from a stub engine). On the x86 build host one simulated hour takes about
10 s with the default fast spectrum path.

Browser checks need Playwright (`pip install -e '.[browser]'` and
`python -m playwright install webkit`). Set `SDRSCAN_CHROME` to use a local Chrome.

## Data budget

All figures are measured on the build host with the test fixtures, except the TLS
overhead, which is a configured estimate (6 KB per HTTPS request). The on-device ledger
measures the real numbers after deployment.

| Item | Bytes |
|---|---|
| Hourly digest, gzipped (week fixture) | about 1.1 KB, capped at 4 KB for 500 busy channels |
| One hourly push with headers and the TLS estimate | about 7.3 KB |
| Per day (24 pushes) | about 175 KB, about 5 MB per month |
| Dashboard page, gzipped, uploaded only when it changes | about 5.5 KB |
| Off-site view, first open | 0 rig bytes; the phone fetches about 5.5 KB page + 1 to 4 KB digest |
| Off-site view, later open | 0 rig bytes; about 0.3 KB on a 304, or the digest |
| Live page over the tailnet, first open | about 5.8 KB, charged to the ledger |

The ledger's hard cap is 30 MB per month. Above 80% the digest goes every 3 h; at the cap
only a 1 KB health beat goes out per day and the live server answers 429.

## Deploying (not done yet)

The rig is not reachable now, so nothing has been installed. The install kit is in
`deploy/`:

1. Build the image: `docker buildx build --platform linux/arm64 --target runtime -t sdrscan:arm64 --load scanner/`.
2. Export it: `docker save sdrscan:arm64 | zstd -19 > sdrscan-arm64.tar.zst`.
3. Copy the tarball and `deploy/` to the rig.
4. Run `sudo deploy/install.sh --dry-run --image sdrscan-arm64.tar.zst`, read the output,
   then run it without `--dry-run`.
5. Put the upload URL in `/etc/sdrscan/uplink.env` (mode 0600) and restart the service.

`install.sh` checks the OS, disk, DVB driver blacklist and `rtl_test` sample loss first. It
keeps the previous image as `sdrscan:previous` and rolls back when the service is not healthy
within 120 s. `uninstall.sh` keeps the data volume unless `--purge` is given.

## Open measurements

These need the real rig:

1. Retune settle time per band.
2. Dropped samples at 2.4 MS/s on the Pi 5.
3. CPU per survey hop and per demodulated channel, next to the ambient player
   (`sdrscan bench --json` prints these).
4. Gain per band, and the 10 dB floor threshold on real 8-bit captures.
5. Voice gate thresholds on real radio audio; they come from v1 and are untuned.
6. Speech-to-text real-time factor at 2 threads, and SoC temperature on a hot afternoon.
7. Ogg Opus clip playback in iOS Safari.
