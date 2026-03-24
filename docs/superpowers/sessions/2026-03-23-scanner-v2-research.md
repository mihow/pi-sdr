# Scanner V2 Session Log — 2026-03-23/24

## What was accomplished

### Design (approved)
- Signal-driven scanner as OpenWebRX+ fork extension with mobile UI
- Design spec: `docs/superpowers/specs/2026-03-23-scanner-v2-design.md`
- Research doc: `docs/superpowers/research/2026-03-23-openwebrx-internals.md`
- Implementation plan: `docs/superpowers/plans/2026-03-23-scanner-v2-implementation.md`

### Implementation (66 tests, 18 commits on `feat/scanner-v2`)
All code in OpenWebRX+ fork: `/home/michael/Projects/Radio/OpenWebRX/openwebrx+`

**Core modules:**
- `owrx/scanner/db.py` — SQLite: detections, bookmarks, sessions (8 tests)
- `owrx/scanner/detector.py` — FFT peak finding, noise floor, averaging, merging (11 tests)
- `owrx/scanner/sweep.py` — frequency windows, skip lists, band→mode mapping (9 tests)
- `owrx/scanner/classifier.py` — pluggable pipeline, freq-band authoritative for mode (10 tests)
- `owrx/scanner/known_freqs.py` — Portland freq database: NOAA, FM, air, marine, ham, GMRS (20 tests)
- `owrx/scanner/__init__.py` — ScannerService with background loop, state, callbacks (5 tests)
- `owrx/scanner/config.py` — config keys registered in OWRX+ property system
- `owrx/controllers/scanner.py` — 5 REST API endpoints wired to OWRX+ router

**CLI tools:**
- `scripts/scan_iq.py` — full pipeline: detect → classify → log → demod → WAV
- `scripts/analyze_scan.py` — audit results: mode mismatches, audio quality, duplicates
- `scripts/record_bands.sh` — record IQ from SDRplay across all voice bands

**Validated with real hardware:**
- SDRplay RSP1a recordings across FM, NOAA, airband, 2m, 70cm, GMRS, marine, HF
- Clear voice audio demodulated from 443.150 MHz repeater (Mount Scott?)
- NOAA WX7 (162.550) confirmed clear in both OpenWebRX+ and our demod
- Known freq labels matching detections (NOAA WX4, KYCH, etc.)

### PRs
- pi-sdr design/research: https://github.com/mihow/pi-sdr/pull/3
- OpenWebRX+ implementation: https://github.com/mihow/openwebrxplus/pull/11
- GitHub issues #4-#10 on mihow/pi-sdr

### Known issues fixed this session
- Mode classifier was wrong 100% — bandwidth overriding frequency band lookup (fixed)
- DC offset in demod audio (fixed)
- Stale files in CLI output (fixed)
- Odd sample rates from decimation (fixed)
- Signal detector too sensitive with real data — added FFT averaging + merging (fixed)

### Known issues remaining
- Duplicate detections in DB when scanning same file twice (no dedup)
- `scan_iq.py` CLI args override JSON sidecar for ALL files (should be per-file)
- 16129 Hz sample rate still appears in some edge cases
- No duration tracking for detections from IQ files (only meaningful for live scanning)
- Portland repeater research agent results not yet integrated into known_freqs.py

## What's next (priority order)

1. **Integrate Portland repeater frequencies** into `known_freqs.py` (agent results pending)
2. **Wire scanner to live SDR** — connect ScannerService to OpenWebRX+'s SdrSource via FftChain + setCenterFreq. This is the task that makes real-time scanning work.
3. **Mobile scanner UI** — HTML/JS at `/scanner` with activity feed + listening view
4. **Audio recording service** — Opus encoding, storage management, pruning
5. **WebSocket scanner messages** — real-time state + audio streaming to mobile UI

## Key files for next session

**OpenWebRX+ fork** (`/home/michael/Projects/Radio/OpenWebRX/openwebrx+`):
- Branch: `feat/scanner-v2` (18 commits ahead of master)
- Scanner modules: `owrx/scanner/` (7 files)
- CLI tools: `scripts/scan_iq.py`, `scripts/analyze_scan.py`, `scripts/record_bands.sh`
- Tests: `test/scanner/` (66 tests)
- IQ recordings: `test_data/iq/` (~3 GB, gitignored, JSON sidecars committed)
- Audio output: `test_data/output/` (gitignored)
- REST API routes added to: `owrx/http.py`
- Config defaults added to: `owrx/config/defaults.py`

**pi-sdr** (`/home/michael/Projects/Radio/pi-sdr`):
- Branch: `worktree-scanner-2`
- Design spec: `docs/superpowers/specs/2026-03-23-scanner-v2-design.md`
- Research: `docs/superpowers/research/2026-03-23-openwebrx-internals.md`
- Plan: `docs/superpowers/plans/2026-03-23-scanner-v2-implementation.md`

## Hardware notes
- SDRplay RSP1a on Beast (dev machine), serial 19030F2B96
- Only one `sdrplay_apiService` daemon at a time — kill duplicates with `sudo pkill -f sdrplay_apiService`
- Docker needs `privileged: true` for SDRplay USB re-enumeration
- hwVer=255 means daemon can't talk to device (stale shm or multiple daemons)
- rx_sdr built at `/tmp/rx_tools/build/rx_sdr` for IQ recording
