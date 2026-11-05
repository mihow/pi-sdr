# SDR scanner design: `sdrscan`

Date: 2026-11-03
Status: proposed design for the MVP
Plan: `docs/superpowers/plans/2026-11-03-sdr-scanner.md`

## 1. Purpose

The scanner runs unattended for months at a remote field station. It watches the radio
spectrum around the station and tells a remote viewer what is happening in the area: what is
active now, in the last hour, day and week, and in general. The viewer reads this on a phone,
often on a weak cellular link.

The deliverable the operator looks at is a **remote dashboard**. Everything else in this
design exists to fill that dashboard with correct, compact facts while the Pi uses very
little of its LTE data allowance.

The dashboard answers these questions:

- Which channels are busiest now, this hour, today, this week, and over all time?
- Which signals are new, or unusually busy or quiet for this hour of the week?
- What did people say recently (transcripts, when speech-to-text is enabled)?
- Can I play a short recent clip of a transmission (on demand only)?
- Is the rig healthy: last seen, uptime, dongle, temperature, disk and clip quota, uplink use?

## 2. Constraints

| Constraint | Value | Consequence |
|---|---|---|
| Computer | Raspberry Pi 5, 4x Cortex-A76, RAM size not yet confirmed (4 or 8 GB) | Budget for 4 GB. |
| Shared workload | An ambient music player (mpv + PipeWire) and remote-access services run on the same Pi | The scanner has no audio-device access, runs at low priority, and has a CPU quota. |
| Receiver | RTL-SDR Blog V4 (R828D), about 24 MHz to 1.766 GHz, 2.4 MS/s stable, 8-bit | One tuner, one process at a time. 2.4 MHz per window, about 1.92 MHz usable after cropping. |
| Uplink | LTE with a small monthly data cap. Idle Tailscale already uses about 25 MB/day | Scanner target: under 0.5 MB/day, hard cap 30 MB/month. Never raw IQ or audio by default. |
| Storage | Small SD card | Strict rolling quota, free-space floor, tmpfs staging, batched writes. |
| Access | Nobody can touch the Pi after deployment. It is not reachable now | Everything is built and tested on the x86 build host. Deployment is an install kit with a dry run and rollback. |
| Climate | US desert southwest, enclosure in the sun | Thermal governor with degraded modes. Plan on duty cycles, not sustained 4-core load. |
| Band plan | United States | Generic US band plan committed. Site labels stay in an uncommitted overlay file. |
| Repository | Public | No hostnames, tailnet names, internal IPs, bucket URLs, site labels or IQ fixtures in git. |

Prior measurements that shape the design (from the 2026-04-11 signal mining report,
`docs/claude/research/2026-04-11-scanner-signal-mining.md`):

- Per-band noise floors differ by about 16 to 17 dB. A global squelch cannot work.
- Values cluster at a 73.6 dB ceiling on unrelated channels with uncontrolled gain. This
  points to ADC clipping.
- Always-on carriers look "dead" when ranked by peak excursion.
- The v1 scanner logged 2,034,719 "signals" and 0 voice detections in 48 h because of four
  stacked bugs: an absolute squelch compared with positive dB values, 48 kHz audio fed to a
  16 kHz detector, a 27 ms hold buffer, and AGC gain read back as 0 dB. These four bugs
  become regression tests in this design.

No CPU, settle-time or speech-to-text speed figure has been measured on a real Pi 5. All such
numbers in this document are estimates and are labelled that way.

## 3. Chosen design

Two designs competed. The chosen design is **"lean own pipeline"**: one Python service,
`sdrscan`, owns the tuner through librtlsdr and does the sweep, detection, tracking,
channelization, demodulation, voice gating, recording and rollups in its own numpy/scipy
code. Four ideas come from the runner-up ("orchestrator of proven tools"):

1. **A dongle lease and a Job protocol.** The scheduler can release the tuner and give it
   to an external decoder (for example `rtl_433`) for a time slot. The MVP ships the lease
   and the protocol. External decoder jobs come later without a redesign.
2. **Idempotent rollups.** Any hour can be recomputed from its raw rows. This makes late
   events and clock corrections safe.
3. **A permanent `rollup_day` table**, so week and all-time views stay cheap after raw rows
   are pruned.
4. **An install kit with preflight and `--dry-run`**, and a fake-tool harness for the external
   decoder jobs when they arrive.

The scoring is in the appendix (section 17).

## 4. Architecture

```
            +----------------------- sdrscan (one process, one container) ------------------------+
 RTL-SDR -> | IqSource -> Scheduler -> SpectrumDetector -> Tracker -> Channelizer/Demod -> VoiceGate |
  V4 USB    |    ^          |  (lease)        |               |                         |        |
            |    |          v                 v               v                         v        |
            |  Clock   ExternalJob slot   spectrum_hour   transmissions            ClipStore     |
            |                                   \             |                    (quota)       |
            |                                    +-----> Store (SQLite WAL) <-----+  |           |
            |                                                 |                      v           |
            |   HealthMonitor/Governor ---------------------> Rollups ----> Summarizer -> digest |
            |                                                                         |          |
            |   LiveServer (tailnet only) <-- page + /api + /clip                      v          |
            +-------------------------------------------------------------------- DigestPublisher+
                                                                                       | 1 PUT/h
                                                                                       v
                                                          off-site static host: index.html + digest.json.gz
                                                                                       |
                                                                                   phone browser
```

The viewer normally reads the **off-site copy**, so a view costs the Pi nothing. The Pi's
own live server is an optional detail path over the tailnet, and every byte it sends is
counted against the monthly cap.

Every component that reads time takes an injected `Clock` (`RealClock` or `VirtualClock`),
so the test suite runs a simulated week in under a minute.

All data is keyed by **integer frequency in Hz**, never by channel name. Duplicate names
corrupted an earlier analysis.

## 5. Components and interfaces

Python 3.11 or later. The package lives at `scanner/sdrscan/`. Interfaces are given as
Python signatures.

### 5.1 Config (`config.py`)

Loads `/etc/sdrscan/config.yaml` into frozen dataclasses with defaults and validation. Secrets
come from a separate env file (mode 0600) named in the config, never from the YAML or the
command line.

```python
def load_config(path: Path | None) -> Config
```

### 5.2 Clock (`clock.py`)

```python
class Clock(typing.Protocol):
    def now(self) -> float          # wall time, unix seconds UTC
    def monotonic(self) -> float
    def sleep(self, s: float) -> None
class RealClock: ...
class VirtualClock:                 # advance(s), set(t); sleep() advances instantly
```

### 5.3 IqSource (`iq/`)

Opens the tuner, tunes, and streams complex64 IQ.

```python
class IqSource(typing.Protocol):
    def open(self) -> None
    def tune(self, center_hz: int, gain_db: float) -> None   # blocks until settle discard is done
    def read(self, n: int) -> numpy.ndarray                   # complex64
    def close(self) -> None
    def health(self) -> dict          # dropped, reopen_count, last_block_age_s
```

- `RtlSource` uses pyrtlsdr on the system librtlsdr (Debian trixie 2.0.x supports the V4).
  A reader thread drains a ring buffer so the USB stream never overflows. v1 logged 90,805
  overflows without one. After a retune it discards settle samples: 30 ms by default, and
  60 ms when the retune crosses the V4 input-switch boundaries at 28.8 MHz and 250 MHz. Gain
  is always manual, from the band plan. AGC is never used, because it makes dB values
  incomparable between hours.
- `FileSource` replays recorded IQ through one memory-mapped reader (`iq/file.py`): rtl_sdr
  `.cu8`/`.raw`, `.cf32` and SigMF, with a JSON sidecar in either the scanner's keys (rate,
  center, gain) or the older capture scripts' keys. Gain may be missing.
- `SyntheticSource` generates IQ from a scene description (section 13).

### 5.4 BandPlan and channel model (`bandplan.py`, `data/bandplan_us.yaml`)

Each band entry has: `id`, `lo_hz`, `hi_hz`, `mode` (`nfm`, `am`, `wfm`, `digital`,
`unknown`), `raster_hz`, `bw_hz`, `gain_db`, `threshold_db` (offset above floor), `tier`
(`A` or `B`), `policy` (`dwell`, `occupancy`, `decoder`, `ignore`).

Bands in the committed US plan include FM broadcast, airband AM 118 to 137 MHz (25 kHz and
8.33 kHz), 2 m and 70 cm amateur, MURS, FRS/GMRS 462/467 MHz, NOAA weather 162.400 to
162.550 MHz, marine VHF, railroad 160.215 to 161.565 MHz, VHF/UHF business, ISM 433 and
915 MHz, APRS 144.390 MHz, pagers, ADS-B 1090 MHz, and cellular and TV as occupancy only.

A detected peak snaps to the raster of its band: `channel_hz = round(f / raster) * raster`.
A peak outside every band goes to an `unplanned` band with a 12.5 kHz raster and is still
tracked. Display names come from the band plan plus an optional local labels file
(`/etc/sdrscan/labels.yaml`), which is never committed.

```python
class BandPlan:
    def band_for(self, hz: int) -> Band
    def channel_for(self, hz: float) -> Channel   # Channel(hz: int, band, mode, bw_hz, name)
    def hops(self, tier: str, usable_hz: int) -> list[int]   # ordered HF -> VHF -> UHF
```

### 5.5 Scheduler and dongle lease (`scheduler.py`, `lease.py`)

The scheduler owns the tuner and picks the next job.

| Job | What it does | Default share |
|---|---|---|
| `SURVEY` | One hop of the sweep plan, 100 ms of samples | at least 55% |
| `DWELL` | Park on the 2.4 MHz window that covers the most active voice-capable tracks | at most 40% |
| `CALIBRATE` | Spur and floor baseline in quiet hours | about 5% |
| `EXTERNAL` | Release the tuner to an external decoder (empty in the MVP) | 0% in the MVP |

- Tier A (land mobile, air, GMRS/FRS, NOAA, rail, 2 m and 70 cm, roughly 60 hops) is
  revisited about every 15 s. Tier B (the rest of 24 to 1766 MHz, about 900 hops) is
  revisited about every 3 to 5 min. Both figures are estimates until settle time is measured.
- Hops run in HF, VHF, UHF order, so a sweep crosses each input switch only twice.
- A dwell lasts at most 60 s per window, then the window cools down for 30 s. An always-on
  carrier cannot hold the tuner.
- A token bucket enforces the shares.
- The governor (5.13) can slow or restrict the scheduler.

```python
class Scheduler:
    def next_job(self, now: float, tracks: TrackTable, mode: str) -> Job
    def budget_report(self) -> dict
class DongleLease:                    # context manager: closes IqSource, waits 300 ms, yields
    def lease(self, job: ExternalJob) -> typing.ContextManager[None]
class ExternalJob(typing.Protocol):   # for rtl_433 and later decoders
    def argv(self) -> list[str]
    def parse(self, line: str) -> list[Event]
    max_runtime_s: float
```

### 5.6 SpectrumDetector (`detect.py`)

For each hop:

1. Tune 150 kHz off the hop center, so the DC spike sits away from the hop's channels.
2. Compute a Welch power spectrum: 4096-point Hann FFT (586 Hz bins), about 50 averages.
3. Crop the outer 10% of each side. Mask 3 bins either side of DC.
4. Floor = median of the remaining bins. Spread = MAD.
5. A bin is hot above `floor + band.threshold_db` (10 dB default, 15 dB for FM broadcast).
6. Merge adjacent hot bins into runs, drop runs below the minimum width, and compute a
   power-weighted center for each run.
7. Count clipped samples (ADC rail). A high clip count goes into health.
8. Add the hop's occupancy into the current hour's 1 MHz occupancy array.

A per-bin EMA baseline and a spur map run alongside. A bin hot in more than 90% of sweeps
for 24 h, with a constant level and narrow width, is marked as a spur and produces no
events.

```python
def analyze(iq: numpy.ndarray, center_hz: int, rate: int, band: Band) -> HopResult
# HopResult(peaks: list[Peak], floor_db, mad_db, clip_frac, occupancy: dict[int, float])
```

### 5.7 Tracker (`tracker.py`)

One state machine per `channel_hz`:

```
IDLE -> CANDIDATE (seen once) -> ACTIVE (2 of last 3 visits, or SNR >= 20 dB in a dwell)
     -> HANG (2 s, or 1 survey miss) -> CLOSED (emit one Transmission)
```

A channel is marked `always_on` when its absolute floor is more than 10 dB above the band
median floor, or its duty cycle is above 80% over 1 h. Always-on channels count as
occupancy, not as transmissions.

```python
class Tracker:
    def update(self, peaks: list[Peak], t: float, source: str) -> list[TrackEvent]
    def active(self, policy: str = "dwell") -> list[Track]
```

### 5.8 Channelizer and demodulator (`dsp/channelizer.py`, `dsp/demod.py`)

During a dwell, every active voice-capable channel in the window is extracted from the same
IQ blocks: complex mix, then staged FIR decimation (2.4 MHz to 48 kHz to 16 kHz), then the
demodulator. Filter state is kept between blocks.

- NFM: quadrature discriminator, deviation 2.5 kHz (5 kHz for NOAA and marine), de-emphasis.
- AM: envelope, DC removal, slow AGC.
- Output: **16 kHz mono float32 for the whole chain.** The VAD, recorder and speech-to-text
  all read this one rate. This removes the v1 rate-mismatch bug by construction.
- Squelch comes from RF channel power, not audio level. FM-demodulated noise has about the
  same RMS whatever the signal strength.

This code is ported from the v1 scanner's `demod.py`, with its cached FIR coefficients.

```python
class Channelizer:
    def __init__(self, rate: int, center_hz: int, channels: list[Channel]) -> None
    def push(self, iq: numpy.ndarray) -> dict[int, numpy.ndarray]   # hz -> pcm16k float32
```

If a dwell with many channels is too slow on the Pi, the fallback is to demodulate only
the top 3 tracks in the window.

### 5.9 VoiceGate (`voice/gate.py`) and SttEngine (`voice/stt.py`)

The gate classifies squelch-open audio as `voice`, `data`, `carrier` or `noise`. It keeps at
least 10 frames of 30 ms (300 ms) across reads and never resets per read. A frame is voice
when webrtcvad (mode 2) votes speech AND (pitch autocorrelation > 0.2 at 80 to 400 Hz OR
spectral flatness < 0.35). A segment is voice when at least 30% of frames in a 30-frame
window are voice. These thresholds come from v1 and are untuned. A Silero VAD backend can
replace webrtcvad later behind the same interface.

```python
class VoiceGate:
    def push(self, pcm16k: numpy.ndarray) -> VoiceState
    def summary(self) -> dict    # voice_ratio, flatness, pitch_hits, frames
class SttEngine(typing.Protocol):
    def transcribe(self, pcm16k: numpy.ndarray) -> Transcript   # text, confidence, engine, rtf
```

The MVP ships `NullStt` (no text) and `FakeStt` (tests). The adapter for a real engine is a
subprocess wrapper around `whisper.cpp` (`whisper-cli`) with `tiny.en`, 2 threads, a smaller
audio context for short clips, and a 300 to 3000 Hz bandpass before inference. It is
optional and off by default. Moonshine Tiny and sherpa-onnx are other candidates for the
same interface. Results below a confidence threshold, or matching a hallucination blocklist
("thank you", "thanks for watching"), are stored but never shown as fact.

### 5.10 ClipStore (`clips.py`)

See section 9.

```python
class ClipStore:
    def begin(self, hz: int, t: float) -> ClipWriter      # stages 16 kHz PCM in tmpfs
    def finish(self, w: ClipWriter, meta: dict) -> int | None   # encode Opus, move, enforce
    def enforce(self) -> list[int]                         # evicted clip ids
```

### 5.11 Store and rollups (`store.py`, `rollup.py`)

SQLite in WAL mode with `journal_size_limit`. Writes are batched every 10 s. The schema is in
section 7. Rollups update incrementally when a transmission closes, and
`rollup.recompute_hour(h)` rebuilds any hour from raw rows. That makes late events and clock
corrections safe.

```python
class Store:
    def add_hop(self, r: HopResult, t: float) -> None
    def add_tx(self, tx: Transmission) -> int
    def flush(self) -> None
    def prune(self, now: float) -> None
def recompute_hour(store: Store, hour_ts: int) -> None
def rollup_day(store: Store, day_ts: int) -> None
def query_window(store: Store, window: str, now: float) -> WindowStats  # now|hour|day|week|all
```

### 5.12 Summarizer and LLM adapter (`summarize.py`, `llm.py`)

See section 8.

```python
def build_digest(store: Store, health: Health, now: float) -> dict    # schema v1, sorted keys
class ProseEngine(typing.Protocol):
    def rewrite(self, digest: dict) -> str | None
```

### 5.13 HealthMonitor and Governor (`health.py`)

Every 30 s it reads SoC temperature (`/sys/class/thermal/thermal_zone0/temp`), the throttle
state (the firmware `get_throttled` sysfs node, or `vcgencmd` if present; only the current bits 0 to 3 govern, the since-boot bits are a flag), disk free, quota use, database size, dongle
presence (USB id `0bda:2838` in sysfs), clock sync state, process RSS and the uplink ledger.
All paths come from a configurable root so tests can fake them.

| Mode | Trigger | Effect |
|---|---|---|
| `normal` | default | all features |
| `hot` | SoC at least 70 °C | no STT or LLM, dwell share 25% |
| `very_hot` | at least 78 °C or a throttle flag | survey only, paced; no recording |
| `critical` | at least 82 °C | tier A only, 1 hop/s |
| `disk_low` | free space below floors (section 9) | evict, prune, then stop clips |
| `no_dongle` | device missing or open fails | back off 5 s up to 5 min, report |

It sends a systemd watchdog ping only while IQ blocks keep arriving. A hung USB read
therefore restarts the service.

### 5.14 DigestPublisher (`uplink.py`)

See section 10.

```python
class DigestPublisher:
    def publish(self, now: float) -> PushResult
    def ledger(self) -> dict        # month_bytes, cap_bytes, state
```

Targets: `file` (tests and local), `s3` (an S3-compatible bucket with a long-lived key
scoped to one prefix, signed in process with SigV4; the default), `http_put` (WebDAV with
a fixed auth header) and `http_post` (a tailnet peer). Stdlib `urllib` only. A presigned URL
is not a target: it signs one key and expires within 7 days. Only `s3` keeps
`Content-Encoding`, so only `s3` gets the page gzipped. A missing or bad target turns pushes
off (`uplink_unconfigured`); it never stops the scanner.

### 5.15 Dashboard (`web/index.html`) and LiveServer (`live.py`)

See section 11.

### 5.16 CLI (`cli.py`)

```
sdrscan run      [--config PATH] [--source rtl|file|synthetic] [--scene PATH]
sdrscan simulate --scene PATH --days N --out DIR     # virtual clock, writes db + digests
sdrscan digest   --db PATH --out DIR
sdrscan dashboard --digest PATH --out DIR            # builds static index.html
sdrscan bench                                        # hop ms, demod ms/channel, VAD ms/frame
sdrscan health
sdrscan capture | detect | demod | gate | stt        # one file in, one file out (section 14)
sdrscan corpus cut --src PATH --out CLIP.cu8         # slice a short clip from a capture
```

## 6. Data flow

1. The scheduler picks a job from the clock time, the track table and the governor mode.
2. For a survey hop, the IQ source tunes 150 kHz off center, discards settle samples, and
   reads 240,000 samples (100 ms).
3. The detector returns peaks above the per-hop floor, with DC and spurs masked, and adds
   to the hour's occupancy array.
4. The band plan snaps peaks to channels. The tracker updates state machines.
5. If voice-capable tracks are active and dwell budget remains, the scheduler starts a dwell
   on the window that covers the most of them. Blocks flow through the channelizer to 16 kHz
   PCM per channel. PCM goes to the voice gate, and squelch-open audio goes to a clip
   writer. The tracker gets 10 Hz power updates.
6. When a track closes, one transmission row is written. If the gate called it voice and it
   lasted at least 1.5 s, the clip is encoded to Opus and quota enforcement runs. The clip
   joins the STT queue when an engine is configured.
7. Once an hour, the summarizer builds the digest and the publisher pushes it.
8. A phone opens the off-site URL. It loads `index.html` (cached for a year) and
   `digest.json.gz`. Nothing reaches the Pi.
9. Optional live mode: a phone on the tailnet opens the Pi's page for the last 5 minutes,
   per-channel detail or a clip. Those bytes go on the ledger.

## 7. Event schema and rollup tables

Frequencies are INTEGER Hz. Times are INTEGER unix seconds UTC. Rows carry `clock_ok` (0 when
NTP was not synced).

| Table | Columns | Retention |
|---|---|---|
| `meta` | `key PK, value` (`schema_version`, `site_id`) | forever |
| `channels` | `hz PK, band, mode, bw_hz, name, first_seen, last_seen, always_on, spur` | forever |
| `transmissions` | `id PK, hz, t_start, t_end, dur_ms, peak_snr_db, med_snr_db, floor_db, bw_hz, source, voice_ratio, class (voice/data/carrier/unknown), clip_id NULL, transcript_id NULL, clock_ok, boot_id` | 90 days |
| `clips` | `id PK, hz, t_start, dur_ms, bytes, codec, path NULL, tier, pinned, sha256, evicted_at NULL` | row forever, file by quota |
| `transcripts` | `id PK, clip_id, engine, text, confidence, rtf, created` | 90 days |
| `rollup_hour` | `hz, hour_ts, active_s, tx_count, voice_tx, voice_s, max_snr_db, first_ts, last_ts, PK(hz, hour_ts)` | 400 days |
| `band_hour` | `band, hour_ts, occupancy_pct, peak_count, floor_med_db, coverage_pct` | 400 days |
| `spectrum_hour` | `hour_ts PK, bins BLOB` (1742 uint8 bins of 1 MHz, occupancy %) | 400 days, about 15 MB/year |
| `rollup_day` | `hz, day_ts, active_s, tx_count, voice_tx, voice_s, max_snr_db, PK(hz, day_ts)` | forever |
| `profile_how` | `hz, how (0-167), ema_active_s, ema_var, n, PK(hz, how)` | forever |
| `channel_total` | `hz PK, active_s, tx_count, voice_tx, hours_seen` | forever |
| `health` | `ts PK, temp_c, throttled, disk_free_mb, quota_used_mb, dongle_ok, drops, clip_frac, mode, clock_ok` | 90 days |
| `uplink_log` | `ts, target, bytes_out, bytes_in, status` | 400 days |

Window definitions:

| Window | Source |
|---|---|
| now | In-memory ring of the last 5 minutes of track states, plus the current tuner state |
| hour | Current `rollup_hour` rows |
| day | Sum of the last 24 `rollup_hour` rows |
| week | Sum of the last 7 `rollup_day` rows plus today's hours |
| all-time | `channel_total` plus `profile_how` |

`coverage_pct` is the share of planned tier A revisits that happened in the hour. Heat,
dwells and external jobs reduce it. The dashboard shows it, so low coverage does not read as
a quiet hour.

## 8. Summaries

Summaries are rule-based in the MVP. They are deterministic, testable and never invent
content. The digest (schema v1, sorted keys) contains:

- `now`: channels active in the last 5 minutes with SNR and class, and the tuner state.
- `hour`, `day`, `week`: top 10 channels by active seconds, with tx count, voice tx and max
  SNR; top bands by occupancy; a 24-cell sparkline per top channel (7x24 for week).
- `new`: a channel with at least 3 transmissions in the window and none in the prior 7 days.
  Spurs are excluded.
- `unusual`: activity z-score above 3 against `profile_how` for the same hour of the week
  (with at least 4 weeks of history; before that, against the trailing 7-day mean).
- `quiet`: a channel in the all-time top 20 that has been silent for at least 3 h during
  hours when it is usually active.
- `always_on`: carriers and spurs, in their own group so they never fill the busiest list.
- `all_time`: totals, typical busiest hours, number of distinct channels.
- `transcripts` (when STT is on): up to 3 snippets per channel per day with confidence at
  least 0.6, each at most 160 characters, each with its confidence. Off in the uplink by
  default.
- `health`: section 5.13 fields, plus `generated_at`, `coverage_pct` and the uplink ledger.

Every list has a fixed cap, and numbers are integers or have one decimal place. The digest
size is bounded at about 3 KB gzipped (target), 4 KB maximum, however busy the site is.

**LLM prose (optional, deferred).** A `ProseEngine` adapter runs `llama-cli` one-shot with
Qwen2.5-0.5B or 1.5B Instruct Q4_K_M, 2 threads, nice 19, once a day, only in `normal` mode
and only while the STT queue is empty. It reads only the digest JSON and writes 3 to 5
sentences into a `prose` field marked as generated. The rule JSON stays authoritative. The
dashboard works without it. The MVP ships `NullProse`.

## 9. Clip store and quota

Layout under one data root (`/var/lib/sdrscan`, a Docker volume):

- `db/scan.sqlite`, expected 50 to 300 MB in steady state (rough estimate: 0.5 to 1 KB per
  transmission row, about 2,000 rows/day, 90 days, plus aggregates).
- `clips/YYYY/MM/DD/<hz>-<ts>.opus`.
- `outbox/` for digests waiting to upload (last 48 kept).
- Staging in tmpfs (`/run/sdrscan`, 64 MB). A partial recording never touches the SD card.

Encoding: `opusenc --bitrate 8 --framesize 60 --speech` from 16 kHz mono. That is about
60 KB per minute and 3.6 MB per hour of speech.

Quota: `clip_quota_bytes` = 2 GiB by default, about 550 h of speech. At 1 h/day of retained
speech that is about 18 months; at 4 h/day about 4.5 months.

The eviction loop runs after every finished clip:

1. While used bytes exceed the quota, or filesystem free space is below 15%, delete the
   oldest clip in the lowest tier first. Tier order: noise or low confidence, then voice,
   then pinned. Pinned clips go only when free space is below 10%.
2. No channel may use more than 10% of the quota. One chatty channel cannot evict everything.
3. The clip row stays, with `path = NULL` and `evicted_at` set, so summaries stay correct.

Database guards:

- Below 12% free after clips are gone: prune transmissions older than 30 days, then run
  `VACUUM INTO` a temporary file in a quiet hour.
- Below 5% free: stop clip and transmission inserts. Rollups, health and digests continue;
  they use a few KB per hour. The dashboard shows "DISK LOW".

All thresholds are config values. Tests drive them with a fake `statvfs`.

## 10. Uplink budget

**Chosen approach:** the Pi pushes a gzipped digest to an off-site static host once an hour.
Viewers read it there, so a view costs **0 bytes of Pi uplink**, whatever the number of
views or viewers. The Pi's own page over the tailnet is the optional detail path.

All figures are rough estimates from payload arithmetic. The on-device ledger measures the
real numbers after deployment.

| Item | Size | Per day |
|---|---|---|
| Hourly `digest.json.gz` | 2.5 to 3.5 KB | |
| HTTPS PUT overhead to a public object store (TLS handshake, headers) | about 6 KB | |
| Hourly total | about 9 KB | about 220 KB |
| Daily `day-YYYYMMDD.json.gz` plus overhead | about 16 to 21 KB | about 20 KB |
| Dashboard page upload, only when its content hash changes | about 10 KB | about 0 |
| **Default total, public HTTPS target** | | **about 0.24 MB/day, about 7 MB/month** |
| Alternative: POST to a tailnet peer (about 1.5 KB overhead per push) | | about 0.1 MB/day |

That is about 1% of what idle Tailscale already uses per day.

Per view:

| Path | Pi bytes | Phone bytes |
|---|---|---|
| Off-site, first open | 0 | about 10 KB page + 3 KB digest |
| Off-site, later open | 0 | about 3 KB, or about 0.3 KB on a 304 |
| Live (tailnet), first open | about 14 KB | same |
| Live refresh of `/api/now` | 1 to 3 KB (0.3 KB on 304) | same |
| Live channel detail | about 2 KB | same |
| Live 30 s clip | about 30 KB | same |

Example live use: 10 views and 5 clips per day is about 0.2 MB/day.

Cap: a monthly ledger with a hard cap of 30 MB by default. It counts request bytes, response
bytes and an estimated TLS overhead.

- Above 80% of the cap: push every 3 h.
- At the cap: one health beat (about 1 KB) per day. The live server returns HTTP 429.
- Offline: digests queue in the outbox. On reconnect only the newest digest plus one
  coalesced day file per missed day are sent.

Raw IQ, raw audio and fine spectrum never leave the Pi automatically. For comparison: Opus
clips for 1 h/day would be 3.6 MB/day, and a 10 kHz spectrum every 5 minutes about 50 MB/day.

Privacy: the off-site path uses a long random prefix with no listing, or signed URLs.
Transcripts are off in the pushed digest by default. Clips are never pushed. Decoder IDs from
later decoder jobs (for example tyre-pressure sensor IDs) are hashed before upload.

## 11. Dashboard

One self-contained `index.html` with inline CSS, JS and SVG. No external assets, no web fonts
(system font stack), no framework. Budget: at most 30 KB raw and 10 KB gzipped. The same file
works in two modes, set at build time: `static` (reads `digest.json.gz` next to it) and
`live` (reads the Pi's `/api/*`).

Layout, top to bottom:

1. **Health strip**: "updated N min ago", dongle, temperature and mode, disk and quota,
   uplink this month, coverage. Stale after 2 h (amber), offline after 6 h (red).
2. **Segmented control**: Now / Hour / Day / Week / All.
3. **Busiest channels**: name, frequency, band, a bar of active minutes, a 24-cell SVG
   sparkline.
4. **New and unusual**, then **quiet**.
5. **Bands by occupancy**.
6. **Recent transmissions** with transcript snippets and confidence badges, when present.
7. **Always-on / spurs**, collapsed by default.
8. **All-time typical hour-of-week** strip.

Mobile requirements, all tested:

- Phone-first. One column at 360 px. Two columns from 720 px.
- No horizontal scroll at 360, 390 and 768 px. Long frequencies and names wrap or truncate.
- Touch targets at least 44 x 44 CSS px.
- Light and dark themes from `prefers-color-scheme`, plus a manual toggle saved in
  `localStorage` inside `try/catch`.
- iOS Safari: `viewport-fit=cover` with safe-area padding, `-webkit-text-size-adjust: 100%`,
  no `:has()` dependency, no `position: fixed` content bars, 16 px minimum input text.
- No auto-refresh. A Refresh button sends a conditional GET (ETag). The page says how old
  the data is.
- Play buttons appear only in live mode, after the Pi answers `/healthz`. A clip is fetched
  only after a tap. iOS Safari playback of Ogg Opus varies by version; the live server can
  remux to WebM or CAF. This needs one check on a real iPhone.

Caching: `index.html` has a content hash in its name and `Cache-Control: immutable,
max-age=31536000`, behind a tiny `index` redirect or a short-lived loader. `digest.json.gz`
has an ETag and `max-age=300`.

**LiveServer** (`live.py`): stdlib `ThreadingHTTPServer`, 2 worker threads, bound only to
the configured tailnet interface address (read at start, never committed). gzip and ETag on
every response. Routes: `/`, `/digest.json`, `/api/now`, `/api/channel/{hz}?w=day`,
`/clip/{id}.opus`, `/healthz`. Every response's size goes on the ledger.

## 12. Deployment

Image: `debian:trixie-slim`, arm64.

- apt: `python3`, `python3-numpy`, `python3-scipy`, `librtlsdr2` (trixie 2.0.x, V4 support),
  `rtl-sdr` (for `rtl_test`), `opus-tools`, `tini`.
- venv with system site packages: `pyrtlsdr`, `webrtcvad`, `pyyaml`.
- whisper.cpp is not packaged in Debian, so a builder stage compiles a static
  `whisper-cli` (pinned tag and checksum, armv8.2-a with dotprod and fp16, no OpenMP) and
  adds the `ggml-base.en-q5_1` model (about 60 MB, pinned by checksum).
- Target size under 300 MB, measured with `docker image ls` after the build. With
  whisper.cpp and its model the runtime image measures 363 MB. If the trixie
  librtlsdr shows V4 problems on real hardware, build the rtl-sdr-blog fork from a pinned
  commit in a builder stage (use `make -j1` under qemu).
- Built on the x86 host with `docker buildx --platform linux/arm64` and exported as a zstd
  tarball (estimate 80 to 120 MB). The transfer happens once, ideally before the rig ships,
  and is not part of the daily budget.

Runtime: one system unit, `sdrscan.service`, runs:

```
docker run --rm --name sdrscan --read-only \
  -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule 'c 189:* rmw' \
  --tmpfs /run/sdrscan:size=64m -v sdrscan-data:/var/lib/sdrscan \
  -v /etc/sdrscan:/etc/sdrscan:ro -v /sys/class/thermal:/host/thermal:ro \
  --network host sdrscan:<tag>
```

Unit settings: `Restart=always`, `RestartSec=15`, `StartLimitIntervalSec=0`,
`TimeoutStartSec=300` (a docker load on an SD card can take more than 90 s),
`Conflicts=` the legacy OpenWebRX unit. Resource limits are `docker run` flags
(`--cpus 2 --cpu-shares 512 --blkio-weight 100 --memory 1g`), because the container's
processes live in Docker's cgroup, not the unit's; `Nice=` on the unit would reach only the
docker client. Speech-to-text and the prose model run at nice 19 inside the container. The
scanner's own 120 s progress watchdog replaces `WatchdogSec`.

The unit has no PipeWire, PulseAudio or audio-device access. It never touches the ambient
player, the phone tunnel or the remote-access units, and it has no ordering against them.

Install kit (`scanner/deploy/`):

- `install.sh` is idempotent and has `--dry-run`. Preflight checks: OS release (trixie or
  bookworm), free disk, that the DVB module is blacklisted (report only), that no other
  process holds the dongle (the legacy OpenWebRX container did), and `rtl_test -s 2400000`
  for 10 s with no lost samples. Then it loads the image, writes `/etc/sdrscan/config.yaml`
  from the template if missing, installs the unit, enables it, and polls `/healthz` for
  120 s. On failure it rolls back to the previous image tag.
- `uninstall.sh` reverses it and keeps the data volume unless `--purge` is given.
- The Pi is unreachable now. The kit is staged for the owner to run when it is reachable.

Config holds: `site_id`, band plan and labels paths, per-band gain overrides, duty shares,
thresholds, quota and floors, thermal limits, uplink target and secrets-file path, monthly
cap, `live_server` on/off and bind interface. Secrets live in `/etc/sdrscan/uplink.env`
(mode 0600).

## 13. Failure handling

| Failure | Detection | Response |
|---|---|---|
| Dongle unplugged or USB reset | read raises, sysfs id gone | close, back off 5 s up to 5 min, mode `no_dongle`, health shows it. A backoff sleep counts as progress for the watchdog, which restarts only a hung read. The bind-mounted `/dev/bus/usb` shows a replugged dongle's new node; after 8 failed opens (about 10 min) the scanner exits with code 71 for a clean restart |
| Dongle held by another process | open fails with a claim error | same back-off; health says "dongle busy"; install preflight disables the legacy holder |
| Overheating | thermal file, throttle flag | governor modes (5.13); `coverage_pct` drops visibly |
| Disk filling | `statvfs` | eviction, pruning, insert stop (section 9) |
| Clock not synced or jumping | NTP state, monotonic versus wall delta | rows marked `clock_ok = 0`; durations use the monotonic clock; backward jumps clamped; affected hours recomputed after sync; dashboard says "clock unsynced" |
| Uplink down or erroring | HTTP status, timeout | outbox queue with coalescing; ledger unchanged on failure |
| Uplink cap reached | ledger | cadence steps down (section 10) |
| SQLite corruption | weekly `PRAGMA integrity_check` | move the database aside, export rollups, start fresh |
| Process crash | systemd | `Restart=always` |

## 14. Testing strategy (all on the x86 build host)

Use the cheapest tier that answers the question.

1. **Unit tests** (pytest, seconds): raster snapping and band lookup; per-hop floor and MAD
   on generated noise with a DC spike; peak merge and minimum width; tracker transitions on a
   virtual clock; demod on synthetic FM and AM tones (recovered tone, SNR, output rate equal
   to the VAD rate); voice gate on synthetic voice (harmonic pulses with formant filters),
   FM noise and DTMF tones. The 2026-04-11 fix plan's four gates are explicit regression
   tests: RF-relative squelch, matched rates, at least 10 frames accumulated across reads,
   and nonzero manual gain.
2. **Scene simulation** (`SyntheticSource` + `VirtualClock`): a YAML scene with an always-on
   carrier, bursty NFM voice on GMRS and 2 m rasters, AM air bursts, a strong FM broadcast
   with adjacent bleed, a fixed spur, per-band floors spread by about 16 dB, and optional
   clipping. The real scheduler, detector, tracker, store and summarizer run a simulated week
   in under a minute. Assertions: every scripted transmission appears once with the right Hz
   and duration within 2 s; carriers and spurs never appear in "busiest"; a channel added on
   day 6 appears in "new"; a 5x burst at 03:00 appears in "unusual"; the dwell share stays at
   or below 40%; tier A revisit is at most 20 s.
3. **Stage tests and recorded IQ**: every stage is a command and a function that reads one
   file and writes one (`sdrscan capture`, `detect`, `demod`, `gate`, `stt`, `digest
   --events`, `dashboard`; code in `stages.py`). Each stage has tests on small synthetic
   files, with a noise-in, nothing-out case. `tests/corpus/manifest.json` names two-second
   slices of real SDRplay and RTL-SDR V4 captures by relative path and sha256, with expected
   results (presence, absence, class, frequency within a tolerance). The tests read
   `SDRSCAN_IQ_CORPUS` and skip when it is unset or a clip is missing or changed. Recordings
   are never committed.
4. **Journeys**: storage against a tiny quota and fake `statvfs`; failure injection (source
   raises mid-read then recovers, thermal file steps 60 to 85 °C, clock jumps of -2 h and
   +3 days, uplink returns 500 or times out, full disk); uplink against a local stdlib HTTP
   server with ledger byte checks and offline coalescing.
5. **Dashboard**: build `index.html` from digests produced by the simulated week (golden
   files), plus empty, stale and health-red digests. Static checks: no `http://` or
   `https://` references, gzip size at most 10 KB, digest at most 4 KB. Browser checks
   (Playwright Chromium and WebKit when installed, otherwise Chrome DevTools MCP by hand) at
   360, 390 and 768 px in both themes: `scrollWidth <= clientWidth`, every button at least
   44 x 44 px, no network request other than the digest, no console errors. WebKit stands in
   for iOS Safari; one check on a real iPhone is still required.
6. **arm64**: `docker buildx build --platform linux/arm64`, then the quick test subset under
   qemu. This proves that packages resolve and the code runs on aarch64. qemu timings are
   not Pi timings. `sdrscan bench` is committed for a later run on real hardware.

## 15. MVP and deferred work

MVP, buildable and tested on the build host in one session:

- The `sdrscan` package: config, clocks, IQ sources (RTL, file, synthetic), band plan with
  the US YAML, detector, tracker, scheduler with dongle lease and an empty external slot,
  channelizer and NFM/AM demod at 16 kHz, voice gate, STT interface with `NullStt`,
  `FakeStt` and an optional `whisper.cpp` subprocess adapter, clip store, SQLite store and
  rollups, rule summarizer, `NullProse` plus an optional `llama-cli` adapter, digest
  publisher with ledger, health and governor, live server, dashboard builder, CLI.
- Test tiers 1, 2, 4 and 5; tier 3 when fixtures are present.
- Dockerfile and a passing arm64 build with the quick subset under qemu.
- systemd unit, config template, install and uninstall scripts with `--dry-run`,
  shellchecked but not run on the Pi.

Deferred:

- A tuned real STT engine and its Pi benchmark; Silero VAD.
- Enabling LLM prose.
- External decoder jobs through the lease: `rtl_433`, a `readsb` snapshot, `direwolf` on
  144.390 MHz, `multimon-ng` on our NFM audio, `dsd-fme` for digital-mode labels.
- Spectrum heatmap rendering; digital or trunked voice; a small CNN classifier; a Codec2
  archive tier; a 15-minute "now" push.
- Measurements on real Pi and V4 hardware: gain per band, settle time, CPU, thermals, STT
  speed next to the ambient player.
- Retiring the legacy OpenWebRX container and the first deployment, which need the Pi.

Known gap, stated plainly: the first deployed version shows activity, not transcripts.

## 16. Open measurements

These need real hardware and block tuning, not the MVP:

1. Retune settle time per band and at the two input-switch boundaries.
2. Dropped samples at 2.4 MS/s with the reader thread on the Pi 5.
3. CPU per survey hop and per demodulated channel, next to the ambient player.
4. Gain per band that removes the clip ceiling without burying weak bands.
5. Floor threshold (10 dB default) on 8-bit V4 captures from the site.
6. STT real-time factor at 2 threads and SoC temperature during a hot afternoon.
7. The Pi's OS release and RAM size.

## 17. Appendix: judge scoring

<details>
<summary>Scores for the two competing designs (1 = poor, 5 = strong)</summary>

| Criterion | A: lean own pipeline (`sdrscan`) | B: orchestrator of proven tools (`scanwatch`) |
|---|---|---|
| Fit to Pi 5 CPU and thermal | 4: one process, governor modes, top-3 demod fallback; numpy DSP cost unmeasured | 4: one tool at a time keeps CPU low; process restarts per job add overhead |
| Data-cap safety | 5: 0 Pi bytes per off-site view, ledger with 30 MB cap, stdlib uplink | 4: same push model; rclone adds about 40 MB RAM per run and less exact byte accounting |
| Storage safety | 5: tiered eviction, per-channel cap, three free-space floors, tmpfs staging | 4: similar quota; fewer floors |
| Testability on the build host | 5: in-process sources and virtual clock run a week in seconds | 4: fake executables on PATH work, but subprocess timing is harder to compress |
| Reuse of prior work | 4: ports v1 demod, detector and test generators; carries the fix-plan gates | 3: reuses tools, but `rtl_power` and `rtl_fm` replace most of v1's DSP |
| Risk | 3: more own DSP to get right; thresholds unproven on V4 | 3: `rtl_power` hop and integration are coarse; `rtl_fm` gives one channel per dwell; per-job USB claim and release churn |
| One-session MVP | 3: large surface; needs strict task order | 4: smaller custom code |
| **Total (of 35)** | **29** | **26** |

Synthesis: design A is the base. From design B it takes the dongle lease and Job protocol
for later decoders, idempotent hour recompute, the permanent `rollup_day` table, the install
preflight and `--dry-run`, staleness thresholds for the dashboard, and hashing of decoder
IDs in the uplink.

</details>
