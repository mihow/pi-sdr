# Scanner V2 Design Spec

## Problem

Learning what RF signals are still used in 2026 and what can be heard in a given location. Existing tools are either desktop-first (SDR++), multi-user oriented (OpenWebRX+), or bookmark-driven (v1 scanner). None provide a mobile-first, signal-driven discovery experience that runs autonomously, logs activity, and lets you browse results from your phone.

## Solution

A scanner plugin for OpenWebRX+ with a custom mobile-first web UI. The scanner sweeps the full SDR range, detects signals, classifies them, logs everything, and records audio. The mobile UI is a lean "police scanner" interface for listening and browsing activity — not a replica of the OpenWebRX+ desktop waterfall.

## Architecture

### Components

```
┌──────────────────────────────────────────────────┐
│                  OpenWebRX+                       │
│                                                   │
│  ┌─────────────┐  ┌──────────────────────────┐   │
│  │ Standard UI  │  │  Scanner Plugin           │   │
│  │ (desktop)    │  │                           │   │
│  │ /            │  │  Sweep → Detect → Classify│   │
│  └──────┬───────┘  │  → Log/Record → (Summarize)│  │
│         │          └──────────┬───────────────┘   │
│         │                     │                    │
│  ┌──────┴─────────────────────┴───────────────┐   │
│  │         OpenWebRX+ Core                     │   │
│  │  SdrSource / csdr DSP / pycsdr / decoders   │   │
│  └──────────────────┬─────────────────────────┘   │
│                     │                              │
│  ┌──────────────────┴─────────────────────────┐   │
│  │         SoapySDR → RTL-SDR / SDRplay        │   │
│  └─────────────────────────────────────────────┘   │
│                                                    │
│  ┌─────────────────┐  ┌────────────────────────┐  │
│  │  Mobile UI       │  │  SQLite + Recordings   │  │
│  │  /scanner        │  │  /var/lib/openwebrx/   │  │
│  │  WebSocket + Web │  │  scanner.db            │  │
│  │  Audio API       │  │  recordings/           │  │
│  └─────────────────┘  └────────────────────────┘  │
└──────────────────────────────────────────────────┘
```

**OpenWebRX+ is the base application.** It handles device management, DSP pipelines (csdr), digital mode decoding, admin settings, and serves the standard desktop UI at `/`. All of that stays intact.

**Scanner plugin** is a server-side addition to OpenWebRX+. It drives the SDR through OpenWebRX+'s `SdrSource` API — retuning across the full device range, reading FFT power data, and controlling the demod chain. It writes to the SQLite database and manages recordings.

**Mobile UI** is an alternative frontend served at `/scanner`. Static HTML/JS, connects to the scanner plugin via WebSocket. No framework initially — vanilla JS or lightweight (Preact/Alpine). Designed for phone viewports.

**Standard OpenWebRX+ UI** remains at `/` for full waterfall + desktop use. The two UIs share the same backend and SDR device.

### Single-user model

The scanner takes exclusive control of the SDR when active. Only one client at a time — if the mobile UI is driving the scanner, the standard UI shows a "scanner is active" status (or vice versa). No simultaneous multi-user VFOs. This simplifies everything: one SDR, one tuning state, one demod chain.

## Scanner Plugin

### Scan cycle

1. Retune SDR to next window (width = device sample rate, e.g., 2.4 MHz for RTL-SDR, 6 MHz for RSP1A)
2. Dwell ~300ms for FFT to settle
3. Read power spectrum from OpenWebRX+'s FFT pipeline
4. Find peaks above noise floor (auto-tracked per window)
5. If no peaks → advance to next window
6. If peaks found → rank by power, retune to strongest peak's center frequency
7. Run classification pipeline on the signal
8. If signal passes the active filter → open demod, stream audio, log, record
9. Listen until squelch closes + hang time expires (configurable, default 2 seconds of silence)
10. Log the completed transmission, advance to next signal or next window

### Scan parameters (user-configurable)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `freq_start` | 25 MHz | Start of scan range |
| `freq_stop` | 1700 MHz | End of scan range |
| `dwell_time` | 300 ms | Time per window before reading FFT |
| `squelch_threshold` | auto | SNR above noise floor (dB), or auto-track |
| `hang_time` | 2000 ms | Silence before moving on |
| `skip_list` | [] | Frequencies to ignore (e.g., known pagers) |
| `demod_mode` | auto | auto-detect / NFM / AM / WFM / manual |
| `scan_strategy` | sequential | sequential / random / priority-weighted |

### Scan speed estimate

RTL-SDR V4 at 2.4 MHz sample rate, scanning 25–1700 MHz:
- 1675 MHz range / 2.4 MHz window = ~700 windows
- 300ms dwell per window = ~210 seconds = ~3.5 minutes per full sweep
- Usable bandwidth is ~80% of sample rate, so effective window is ~1.9 MHz → ~880 windows → ~4.4 minutes

RSP1A at 6 MHz sample rate:
- 1675 MHz / 6 MHz = ~280 windows → ~84 seconds per sweep

These are worst-case (no signals found). In practice, dwelling on active signals slows the sweep, but priority bands and skip lists speed it up.

### Auto-detect demod mode

Based on frequency range and signal characteristics:

| Range | Default mode | Rationale |
|-------|-------------|-----------|
| 25–30 MHz | AM/SSB | HF CB, shortwave |
| 30–88 MHz | NFM | VHF low band, public safety |
| 88–108 MHz | WFM | FM broadcast |
| 108–137 MHz | AM | Air band |
| 137–174 MHz | NFM | VHF high band, NOAA, marine, 2m ham |
| 174–400 MHz | NFM | UHF, various |
| 400–470 MHz | NFM | UHF public safety, GMRS, 70cm ham |
| 470–960 MHz | WFM/NFM | TV broadcast (legacy), various |
| 960–1700 MHz | AM/NFM | Air nav, GPS (limited use) |

User can override per frequency or per range.

## Classification Pipeline

Every detected signal goes through a chain of pluggable stages:

```
Signal detected (power above noise)
  → Stage 1: Modulation check — is there modulation? (vs carrier/interference)
  → Stage 2: Mode ID — NFM / AM / WFM / CW / digital / unknown
  → Stage 3: Content filter (user-configured):
       ┌─ "voice"   → VAD (voice activity detection)
       ├─ "CW"      → Morse decode, display text
       ├─ "APRS"    → packet decode, log position
       ├─ "keyword"  → Whisper transcription → keyword match
       └─ "any"     → pass everything
  → Stage 4: Action
       ┌─ unsquelch → stream audio to client
       ├─ notify    → push notification / beep
       └─ log-only  → record but don't interrupt
```

### Stage priority and defaults

- Default filter: **voice only** (VAD)
- Multiple filters can be active simultaneously with different actions (e.g., voice → unsquelch, CW → notify, everything else → log-only)
- Each stage is a Python class with a `process(signal_metadata, audio_buffer) → result` interface
- New classifiers are added by implementing the interface and registering in config

### Voice Activity Detection (VAD)

First-pass implementation: autocorrelation-based (from epxx.co reference — voice shows periodicity, noise doesn't). Lightweight, no ML dependency.

Future upgrade: WebRTC VAD or Silero VAD (small neural network, runs on Pi).

### Future: Summarize stage

Not built in v1, but the pipeline is designed for it:
- Whisper transcription produces text from recorded audio clips
- LLM summarization produces structured output: topic, participants, tone, key phrases
- Summaries stored in SQLite alongside the log entry
- Enables queries like "what was discussed on 146.520 this week?"

## Logging & Database

### SQLite schema

```sql
-- Every detected signal, whether it passed filters or not
CREATE TABLE detections (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,          -- ISO 8601
    frequency_hz INTEGER NOT NULL,
    bandwidth_hz INTEGER,
    mode TEXT,                        -- NFM, AM, WFM, CW, digital, unknown
    peak_power_db REAL,
    snr_db REAL,
    duration_sec REAL,               -- NULL until transmission ends
    classification TEXT,             -- voice, data, CW, noise, unknown
    filter_result TEXT,              -- passed, rejected, log-only
    bookmark_label TEXT,             -- from known frequency list, if matched
    recording_path TEXT,             -- path to audio file, NULL if not recorded
    transcription TEXT,              -- future: whisper output
    summary TEXT                     -- future: LLM summary
);

CREATE INDEX idx_detections_time ON detections(timestamp);
CREATE INDEX idx_detections_freq ON detections(frequency_hz);
CREATE INDEX idx_detections_class ON detections(classification);

-- User bookmarks / saved frequencies
CREATE TABLE bookmarks (
    id INTEGER PRIMARY KEY,
    frequency_hz INTEGER NOT NULL,
    label TEXT NOT NULL,
    mode TEXT,                       -- preferred demod mode
    notes TEXT,
    created_at TEXT NOT NULL,
    last_heard TEXT                  -- updated when detected
);

-- Scanner configuration snapshots (for reproducibility)
CREATE TABLE scan_sessions (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    config_json TEXT NOT NULL        -- full scan parameters
);
```

### Database location

Default: `/var/lib/openwebrx/scanner.db`

Configurable — same setting as recording storage location.

## Recording & Storage

### Recording pipeline

When a signal passes the classification filter and the action includes recording:

1. Audio from the demod pipeline is tee'd to an encoder (opus/ogg, 24 kbps for voice)
2. Written to `{storage_path}/recordings/{YYYY-MM-DD}/{timestamp}_{freq_hz}_{mode}.ogg`
3. `recording_path` in the detections table points to this file
4. When the transmission ends (squelch closes + hang time), the file is finalized

### Storage management

| Setting | Default | Description |
|---------|---------|-------------|
| `storage_path` | `/var/lib/openwebrx/recordings/` | Base directory for recordings |
| `max_storage_mb` | 10240 (10 GB) | Total storage cap. Oldest pruned first. |
| `max_clip_duration_sec` | 300 (5 min) | Max per-clip recording length |
| `retention_days` | 30 | Delete recordings older than this |
| `recording_quality_kbps` | 24 | Opus bitrate (24 kbps = ~10 MB/hour) |
| `record_mode` | filter_matches | off / filter_matches / all |

### Storage math

At 24 kbps opus:
- 1 hour of continuous audio = ~10 MB
- 10 GB cap = ~1000 hours of audio
- In practice, scanner audio is intermittent. A busy day might produce 2–4 hours of actual audio = 20–40 MB. 10 GB holds months of recordings.

### Pruning

A background task runs periodically (every hour):
1. Delete recordings older than `retention_days`
2. If total storage exceeds `max_storage_mb`, delete oldest recordings until under limit
3. Update SQLite: set `recording_path = NULL` for pruned entries (log entry stays)

## Mobile UI

### Technology

Static HTML/JS/CSS served by OpenWebRX+ at `/scanner`. No build step, no framework initially. WebSocket connection to the scanner plugin for:
- Real-time scanner state (current frequency, mode, signal strength, scan progress)
- Audio stream (opus/ogg frames, played via Web Audio API)
- Commands (skip, hold, release, nudge freq/width, change filter)

### Mobile audio handling

Mobile browsers restrict audio playback until user gesture. The UI will:
1. Show a "Tap to start" overlay on first load
2. Initialize Web Audio API context on the tap event
3. Keep the audio context alive with a silent buffer if needed (prevents iOS suspension)

### Screen 1: Activity Feed (default)

Shown when not actively listening. The scanner is running in the background, logging everything.

```
┌─────────────────────────┐
│  ● Scanning 25-1700 MHz │
│  Filter: Voice only     │
│                         │
│  ACTIVE NOW         ↻   │
│  ▸ 162.475  NFM  ●●●○○ │
│  ▸ 146.520  NFM  ●●○○○ │
│  ▸ 121.500  AM   ●○○○○ │
│                         │
│  RECENT (last 30 min)   │
│  ▸ 462.562  NFM  12:21  │
│  ▸ 155.730  NFM  12:14  │
│  ▸ 146.940  NFM  11:58  │
│                         │
│  ★ BOOKMARKS            │
│  ▸ NOAA WX4   162.475 ● │
│  ▸ 2m Simplex 146.520 ● │
│  ▸ Air Emerg  121.500   │
│                         │
│  MOST ACTIVE (24h)      │
│  ▸ 162.475  284 det.    │
│  ▸ 146.520   47 det.    │
│                         │
│  [⏸ Pause] [📊 Log] [⚙]│
└─────────────────────────┘
```

- **Active Now** — signals detected in the current or most recent sweep, sorted by power. Live-updating via WebSocket. Signal strength shown as dot indicators.
- **Recent** — signals that passed the classification filter in the last N minutes, sorted by recency. Shows last-seen time.
- **Bookmarks** — user-saved frequencies. Activity dot (●) if heard recently.
- **Most Active** — top frequencies by detection count over 24 hours. Surface to show patterns at a glance.
- Tap any row → retune, start demod, switch to Listening view.
- The scanner continues logging in the background while the user listens to one frequency.

### Screen 2: Listening View

Shown when tuned to a frequency (either by tapping a row or by the scanner auto-stopping on a signal that passed the filter).

```
┌─────────────────────────┐
│  ← Back                 │
│                         │
│  162.475 MHz        NFM │
│  NOAA Weather KIG77     │
│                         │
│  ▁▃▅▇█▇▅▃▁▁▃▅▇█▇▅▃▁   │
│                         │
│   ◄ ◄   162.475   ► ►  │
│          ±12.5 kHz      │
│   ◄◄     width     ►►  │
│                         │
│  [Skip] [Hold] [★ Save] │
│                         │
│  ● Listening            │
│  Filter: Voice only     │
└─────────────────────────┘
```

- **Frequency display** — current freq, mode, bookmark label if known
- **Signal indicator** — small signal strength meter or mini waterfall strip (toggleable)
- **Nudge controls** — arrows to adjust center frequency (±1 kHz fine, ±5 kHz coarse) and demod bandwidth (±2.5 kHz steps)
- **Skip** — release this frequency, scanner advances to next signal
- **Hold** — stay on this frequency, disable auto-advance
- **Save** — bookmark with label (prompted)
- When the scanner auto-advances (squelch closed + hang time expired), it returns to the Activity Feed or jumps to the next matching signal

### Screen 3: Activity Log

Accessed from the activity feed via the Log button. Shows historical data.

- Scrollable list of logged detections with filters (date range, mode, classification)
- Tap a row to see detail: timestamp, frequency, mode, duration, SNR, classification
- Play button for entries that have recordings
- Export option (CSV) for analysis

### Screen 4: Settings

- Scan range (start/stop frequency)
- Classification filter (voice / CW / any / keyword list)
- Squelch mode (auto / manual threshold)
- Hang time
- Recording (off / filter matches / all)
- Storage (location, max size, retention)
- Skip list management
- Demod mode overrides per frequency range

## Hardware Support

### RTL-SDR Blog V4 (Pi)
- Sample rate: 2.4 MHz (max usable)
- Frequency range: 24–1766 MHz
- 8-bit ADC
- Single antenna input
- Scan speed: ~4.4 minutes per full sweep

### SDRplay RSP1A (host/Beast)
- Sample rate: 6 MHz at 14-bit (best), 8 MHz at 12-bit
- Frequency range: 1 kHz–2 GHz
- DAB + FM notch filters
- Scan speed: ~84 seconds per full sweep at 6 MHz
- Requires `sdrplay_apiService` daemon inside Docker container

Both devices are supported through SoapySDR — the scanner plugin doesn't need to know which hardware is connected. Device-specific config (notch filters, gain modes) is handled in OpenWebRX+'s device profile settings.

## What's NOT in V1

- LLM summarization (future: Whisper + LLM pipeline)
- Keyword-based filtering (requires Whisper transcription)
- Native mobile app (start with web, evaluate later)
- Multi-device support (one SDR at a time)
- Trunked radio following (P25, DMR trunking)
- Signal fingerprinting / re-identification
- Heatmap / visualization dashboard (v1 has the log list; heatmap is a future UI addition)
- CW decode integration (future classifier stage)
- APRS decode integration (future classifier stage, OpenWebRX+ already decodes APRS)

## Dependencies

- OpenWebRX+ v1.2.x (base application)
- csdr / pycsdr (DSP library, installed with OpenWebRX+)
- SoapySDR (device abstraction, already in pi-sdr image)
- SQLite3 (Python stdlib, no extra dependency)
- opus-tools or opusenc (for recording encoding)
- Digital mode decoders: codec2, direwolf, multimon-ng, wsjtx, m17-demod (installed with OpenWebRX+)

## Open Questions

1. **Plugin architecture:** Does OpenWebRX+ have a formal plugin API for server-side extensions, or do we need to fork/patch the core? The community `freq_scanner` is client-side JS only. Need to investigate the server-side extension story.

2. **SDR retune API:** Can we retune the SDR center frequency programmatically through OpenWebRX+'s Python API without the standard UI being involved? Need to trace the `SdrSource` class.

3. **FFT data access:** How do we read the FFT power spectrum from the server side? The standard UI gets it via WebSocket. The scanner plugin needs it internally.

4. **Audio tee:** Can we tap the demod audio output for recording while simultaneously streaming to the WebSocket client? Need to understand the csdr pipeline graph.

5. **Mobile audio reliability:** Web Audio API on mobile Safari/Chrome has known issues with background tabs, screen lock, and autoplay. Need to prototype and test early.
