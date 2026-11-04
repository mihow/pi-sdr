# SDR scanner implementation plan (MVP)

Date: 2026-11-03
Spec: `docs/superpowers/specs/2026-11-03-sdr-scanner-design.md`
Branch: `feat/sdr-scanner`

## Goal

Build the `sdrscan` MVP on the x86 build host, with no Pi and no dongle. The MVP produces a
phone-first remote dashboard from a synthetic week of radio activity, and an arm64 image
that passes its quick tests under qemu.

## Rules for every task

1. **Test first.** Write the failing test, run it and see it fail, then write the code, then
   see it pass. Commit after each task with a Conventional Commit message.
2. **One rate.** All audio after demodulation is 16 kHz mono float32. Use the constant
   `sdrscan.AUDIO_RATE`, never a literal.
3. **Integer Hz keys.** Every frequency in storage and in the digest is `int` Hz.
4. **Injected time.** No `time.time()` or `time.sleep()` outside `clock.py`.
5. **Public repo.** No hostnames, IPs, tailnet names, bucket URLs, site labels or IQ files in
   commits. Fixture paths come from `SDRSCAN_IQ_FIXTURES`; tests skip without it.
6. **Stage files by name.** Never `git add -A` or `git add .`. Never `git stash`.
7. Do not ssh to the Pi or install anything on it.

## Layout

```
scanner/
  pyproject.toml            # name "sdrscan", python >=3.11, deps, pytest config, markers
  Dockerfile                # arm64-capable, stages: base, test, runtime
  .dockerignore
  sdrscan/
    __init__.py             # AUDIO_RATE = 16000, IQ_RATE = 2_400_000, __version__
    config.py  clock.py  bandplan.py  detect.py  tracker.py  scheduler.py  lease.py
    store.py  rollup.py  clips.py  summarize.py  llm.py  uplink.py  health.py
    live.py  dashboard.py  cli.py  bench.py
    iq/       __init__.py base.py rtl.py file.py synthetic.py scene.py
    dsp/      __init__.py spectrum.py channelizer.py demod.py
    voice/    __init__.py gate.py stt.py
    data/     bandplan_us.yaml  config.example.yaml
    web/      index.html.tmpl
  tests/
    conftest.py             # VirtualClock fixture, tmp data root, fake sysfs root
    scenes/   week_basic.yaml  storm.yaml
    golden/   digest_week.json
    test_*.py
  deploy/
    sdrscan.service  install.sh  uninstall.sh  config.template.yaml
```

Pytest markers: `slow` (scene week, browser checks), `browser` (needs Playwright),
`fixtures` (needs `SDRSCAN_IQ_FIXTURES`), `hw` (needs a real dongle; never run in CI).

## Verification commands

Run from the repository root on the build host.

```bash
# Unit and integration tests (x86)
cd scanner && python3 -m venv .venv && . .venv/bin/activate \
  && pip install -e '.[dev]' && python -m pytest -q

# Quick subset only
python -m pytest -q -m "not slow and not browser"

# Lint and format with the versions pinned in pyproject
ruff check sdrscan tests && ruff format --check sdrscan tests

# arm64 image build and tests under qemu (binfmt is already registered on the host)
docker buildx build --platform linux/arm64 --target test -t sdrscan:arm64-test --load scanner/
docker run --rm --platform linux/arm64 sdrscan:arm64-test \
  python -m pytest -q -m "not slow and not browser and not fixtures and not hw"
docker buildx build --platform linux/arm64 --target runtime -t sdrscan:arm64 --load scanner/
docker image ls sdrscan:arm64          # record the size; target < 300 MB

# Deploy scripts
shellcheck scanner/deploy/*.sh
```

---

## PART A: core

### A1. Package skeleton and config

Files: `scanner/pyproject.toml`, `scanner/sdrscan/__init__.py`, `scanner/sdrscan/config.py`,
`scanner/sdrscan/data/config.example.yaml`, `scanner/tests/conftest.py`,
`scanner/tests/test_config.py`.

Dependencies: `numpy`, `scipy`, `pyyaml`, `webrtcvad`; optional extra `rtl` = `pyrtlsdr`;
extra `dev` = `pytest`, `ruff` (pinned), `playwright` (optional).

Interface:

```python
@dataclasses.dataclass(frozen=True)
class Config:
    site_id: str; data_root: Path; stage_root: Path
    bandplan_path: Path; labels_path: Path | None
    duty: DutyConfig; detect: DetectConfig; quota: QuotaConfig
    thermal: ThermalConfig; uplink: UplinkConfig; live: LiveConfig
def load_config(path: Path | None = None) -> Config
```

Tests first:

- The example config loads and every field has the spec default (quota 2 GiB, cap 30 MB,
  dwell share 0.40, threshold 10 dB).
- An unknown key raises `ConfigError` naming the key.
- A secret-looking key (`token`, `password`, `secret`) in the YAML raises; secrets must come
  from the env file named by `uplink.secrets_env`.

### A2. Clock

Files: `sdrscan/clock.py`, `tests/test_clock.py`.

```python
class Clock(typing.Protocol): now() -> float; monotonic() -> float; sleep(s) -> None
class RealClock: ...
class VirtualClock:  # __init__(start: float); advance(s); set(t); sleep advances instantly
```

Tests: `sleep(3600)` advances both `now` and `monotonic` by 3600 with no real delay;
`set()` backward changes `now` but never decreases `monotonic`.

### A3. Band plan and channel model

Files: `sdrscan/bandplan.py`, `sdrscan/data/bandplan_us.yaml`, `tests/test_bandplan.py`.

Interface (spec 5.4): `Band`, `Channel`, `BandPlan.load(path, labels=None)`,
`band_for(hz)`, `channel_for(hz)`, `hops(tier, usable_hz)`.

Tests first:

- 462.5625 MHz plus 1.2 kHz snaps to 462562500 in the GMRS band.
- 121.5 MHz snaps on the airband raster and has mode `am`.
- A frequency in no band returns band `unplanned` with a 12.5 kHz raster.
- Overlapping bands in the YAML fail validation.
- `hops("A", 1_920_000)` covers every tier A band and is sorted ascending (HF, VHF, UHF).
- A labels overlay renames a channel without changing its Hz key.

### A4. IQ source abstraction

Files: `sdrscan/iq/base.py`, `iq/file.py`, `iq/synthetic.py`, `iq/scene.py`, `iq/rtl.py`,
`tests/test_iq_sources.py`.

Interface (spec 5.3): `IqSource` protocol with `open`, `tune(center_hz, gain_db)`,
`read(n)`, `close`, `health`.

- `FileSource(path)` reads `.cf32` plus a JSON sidecar (`rate`, `center_hz`, `gain_db`). It
  loops or raises `EOFError` per a flag. `tune` to a center other than the file's raises
  `ValueError`, so tests notice wrong tuning.
- `Scene` (YAML) describes emitters: `carrier`, `nfm_voice` (burst schedule), `am_voice`,
  `wfm`, `spur`, `noise_floor` per band, `clip` flag, and `fail_at` events (raise mid-read,
  recover after N s). It takes a `Clock` so bursts follow virtual time.
- `SyntheticSource(scene, clock, rate)` renders the emitters that fall inside the current
  2.4 MHz window into complex64 blocks, adds per-band noise, adds a DC spike at the tuner
  center, and quantizes to 8 bits to imitate the RTL.
- `RtlSource` wraps pyrtlsdr with a reader thread and ring buffer, manual gain only, and a
  settle discard (30 ms; 60 ms across 28.8 MHz and 250 MHz). It imports pyrtlsdr lazily, so
  tests run without librtlsdr. Its tests are marked `hw`.

Port from v1 (`git show feat/radio-scanner:scanner/sdr_backend.py` and
`scanner/test_e2e.py`): the backend protocol shape, the cf32 sidecar format, and the
`generate_fm_signal` and `generate_voice_like_signal` generators. Rewrite, do not copy whole.

Tests first:

- `FileSource` round-trips a generated cf32 and sidecar.
- `SyntheticSource` with one 462.5625 MHz carrier tuned at 462.4125 MHz shows the strongest
  FFT bin within 1 kHz of 462.5625 MHz.
- `RtlSource.tune` rejects `gain_db=None` (regression: v1 ran with AGC, read back as 0 dB).
- A scene `fail_at` makes `read` raise `IqSourceError`, and a later `open` succeeds.

### A5. Spectrum and detection

Files: `sdrscan/dsp/spectrum.py`, `sdrscan/detect.py`, `tests/test_detect.py`.

Interface (spec 5.6):

```python
def welch_psd(iq, nfft=4096, navg=50) -> numpy.ndarray        # dB per bin, fftshifted
def analyze(iq, center_hz: int, rate: int, plan: BandPlan) -> HopResult
@dataclasses.dataclass
class Peak: hz: float; width_hz: float; level_db: float; snr_db: float
@dataclasses.dataclass
class HopResult: peaks: list[Peak]; floor_db: float; mad_db: float; clip_frac: float
                 occupancy: dict[int, float]   # 1 MHz bin index -> fraction hot
class SpurMap: def update(r: HopResult, t) -> None; def is_spur(hz) -> bool
```

Tests first:

- Pure noise gives zero peaks, and the floor is within 1 dB of the generated level.
- **Regression (v1 bug 1):** a 20 dB SNR carrier is found whether the absolute noise level is
  +20 dB or -60 dB on the scale. No absolute squelch exists.
- The DC spike at the tuner center is not reported as a peak.
- Two carriers 50 kHz apart give two peaks; one 16 kHz-wide NFM signal gives one peak.
- An FM broadcast band hop uses the 15 dB offset.
- A spur present in every hop for 24 virtual hours is marked by `SpurMap`.
- A clipped scene gives `clip_frac > 0.01`.

### A6. Tracker

Files: `sdrscan/tracker.py`, `tests/test_tracker.py`.

Interface (spec 5.7): `Tracker(plan, clock)`, `update(peaks, t, source) -> list[TrackEvent]`,
`active(policy)`, `close_all(t)`. `TrackEvent` kinds: `opened`, `closed` (with a
`Transmission`), `always_on`.

Tests first (on `VirtualClock`):

- Seen once: `CANDIDATE`, no event. Seen in 2 of 3 visits: `ACTIVE`, `opened`.
- A single dwell reading at SNR 25 dB opens immediately.
- After one survey miss or 2 s without a dwell reading, the track closes and emits exactly one
  transmission whose duration is within 2 s of the scripted burst.
- A channel above 80% duty over 1 h is flagged `always_on` and stops emitting transmissions.
- Two peaks that snap to the same channel give one track (keyed by Hz).

### A7. Channelizer and demodulator

Files: `sdrscan/dsp/channelizer.py`, `sdrscan/dsp/demod.py`, `tests/test_demod.py`.

Interface (spec 5.8):

```python
class Channelizer:
    def __init__(self, rate: int, center_hz: int, channels: list[Channel]) -> None
    def push(self, iq: numpy.ndarray) -> dict[int, numpy.ndarray]   # hz -> float32 @ AUDIO_RATE
    def rf_power_db(self) -> dict[int, float]                         # squelch source
def nfm_demod(x, rate, deviation_hz, deemph_us=75.0) -> numpy.ndarray
def am_demod(x, rate) -> numpy.ndarray
```

Port from v1 `demod.py` (`extract_channel`, `fm_demodulate`, `am_demodulate`, cached FIR).
Use `scipy.signal.firwin` plus `lfilter` with kept state, or `resample_poly`, in stages
2.4 MHz to 48 kHz to 16 kHz.

Tests first:

- An NFM signal with a 1 kHz tone at 2.5 kHz deviation demodulates to a 1 kHz tone (FFT peak
  within 10 Hz) with SNR at least 20 dB.
- An AM signal with a 600 Hz tone demodulates to 600 Hz.
- **Regression (v1 bug 2):** the output sample rate equals `AUDIO_RATE` and the `VoiceGate`
  rate; a test constructs both and asserts equality.
- Two channels in one window give independent outputs with no crosstalk above -30 dB.
- Pushing the same signal in 10 small blocks gives the same audio as one large block within
  1e-3 (filter state is kept).
- `rf_power_db` drops by at least 15 dB when the carrier stops.

### A8. Event store

Files: `sdrscan/store.py`, `tests/test_store.py`.

Interface (spec 5.11 and section 7): `Store.open(path, clock)`, `migrate()`,
`add_hop(result, t)`, `add_tx(tx) -> int`, `add_clip(...)`, `add_health(h)`,
`flush()`, `prune(now)`, plus read helpers used by rollups. WAL mode,
`journal_size_limit`, writes batched every 10 s or at `flush()`.

Tests first:

- `migrate()` creates every table in spec section 7 and sets `schema_version = 1`; running it
  twice is a no-op.
- 1,000 transmissions added without `flush()` are not visible to a second connection; after
  `flush()` they are.
- `prune()` deletes transmissions and health rows older than 90 days and leaves
  `rollup_day`, `channel_total` and `profile_how` untouched.
- Frequencies round-trip as `int`.

### A9. Scheduler, dongle lease and the core loop

Files: `sdrscan/scheduler.py`, `sdrscan/lease.py`, `sdrscan/runner.py`,
`tests/test_scheduler.py`, `tests/test_runner_scene.py`.

Interfaces (spec 5.5):

```python
class Job: kind: str  # SURVEY | DWELL | CALIBRATE | EXTERNAL; center_hz; duration_s; channels
class Scheduler:
    def __init__(self, plan, cfg: DutyConfig, clock) -> None
    def next_job(self, now, tracks: Tracker, mode: str) -> Job
    def budget_report(self) -> dict          # share per kind, tier A revisit p95
class DongleLease:
    def __init__(self, source: IqSource, clock) -> None
    def lease(self, job: ExternalJob) -> typing.ContextManager[None]
class Runner:   # glues source, detector, tracker, channelizer, gate, clip store, store
    def step(self) -> None
    def run(self, until: float) -> None
```

`Runner` calls the `VoiceGate` and `ClipStore` through interfaces that Part B fills. In Part
A, pass stub implementations (`AlwaysNoiseGate`, `NoClips`).

Tests first:

- Over 24 virtual hours with no activity, survey share is at least 95%.
- With 5 persistent voice tracks, dwell share stays at or below 40% and tier A revisit p95 is
  at most 20 s.
- A dwell ends at 60 s and that window is not dwelt on again for 30 s.
- Mode `critical` yields only tier A hops at no more than 1 hop/s.
- `DongleLease` closes the source before an external job and reopens it after, even when the
  job raises.
- Scene test (`tests/scenes/week_basic.yaml`, marked `slow` for the week, with a 6 h variant
  unmarked): every scripted burst appears once in `transmissions`; the carrier and the spur
  never appear as transmissions.

**Part A checkpoint:** the quick subset passes on x86, and the arm64 `test` target builds.
Do the arm64 build here (B9 holds the Dockerfile; write a minimal version of it now) so
package problems on aarch64 surface early.

---

## PART B: voice, summaries, storage, uplink, dashboard, deployment

### B1. VoiceGate and STT adapter

Files: `sdrscan/voice/gate.py`, `sdrscan/voice/stt.py`, `tests/test_voice_gate.py`,
`tests/test_stt.py`.

Interfaces (spec 5.9):

```python
class VoiceGate:
    def __init__(self, rate: int = AUDIO_RATE, frame_ms: int = 30, window: int = 30) -> None
    def push(self, pcm: numpy.ndarray) -> VoiceState    # PENDING | VOICE | DATA | NOISE
    def summary(self) -> dict
    def reset(self) -> None                             # only at transmission end
class SttEngine(typing.Protocol): def transcribe(self, pcm) -> Transcript
class NullStt: ...      class FakeStt: ...  # returns scripted text
class WhisperCppStt:    # subprocess: whisper-cli -m MODEL -t 2 -ac N -nt -oj -f clip.wav
    def __init__(self, binary: Path, model: Path, threads: int = 2) -> None
def make_stt(cfg) -> SttEngine
```

Tests first:

- Synthetic voice (harmonic pulses at 120 Hz with formant filtering) gives `VOICE`.
- FM discriminator noise gives `NOISE`; a DTMF pair gives `DATA`.
- **Regression (v1 bug 3):** pushing 300 ms of voice in 27 ms chunks reaches a decision; the
  gate accumulates at least 10 frames across pushes and is not reset per push.
- `WhisperCppStt` builds the expected argv, parses a recorded JSON output fixture into
  `Transcript`, and drops blocklisted text ("thank you") to confidence 0. It is skipped
  unless `SDRSCAN_WHISPER_BIN` is set for the live-binary test.
- `make_stt` returns `NullStt` when STT is disabled or in a `hot` mode.

### B2. Rolling clip store with quota

Files: `sdrscan/clips.py`, `tests/test_clips.py`.

Interface (spec 5.10 and section 9). The encoder is `opusenc --bitrate 8 --framesize 60
--speech`, called through an injectable `Encoder` so tests can use a fake that writes N
bytes. A real-encoder test is skipped when `opusenc` is missing.

Tests first (fake `statvfs` and fake encoder):

- With quota 1 MB and 50 KB clips, the 21st clip evicts the oldest noise-tier clip first.
- Pinned clips survive until free space is below 10%.
- One channel never holds more than 10% of the quota.
- Free space below 15% evicts even under quota.
- Below 5% free, `finish()` returns `None` and stores nothing.
- An evicted clip keeps its row with `path NULL` and `evicted_at` set.
- Staging writes go only under `stage_root`; a crash mid-clip leaves nothing in `clips/`.

### B3. Rollups

Files: `sdrscan/rollup.py`, `tests/test_rollup.py`.

Interface (spec 5.11 and section 7): `on_tx_closed(store, tx)` (incremental),
`recompute_hour(store, hour_ts)`, `rollup_day(store, day_ts)`, `update_profiles(store,
hour_ts)`, `query_window(store, window, now) -> WindowStats`.

Tests first:

- Incremental updates and `recompute_hour` give identical `rollup_hour` rows for the same
  transmissions (idempotence).
- A transmission that spans an hour boundary splits its active seconds between both hours.
- A late event for hour H, followed by `recompute_hour(H)`, changes only hour H.
- `week` equals the sum of 7 `rollup_day` rows plus today's hours.
- `query_window("all")` still answers after raw rows are pruned.
- Rows with `clock_ok = 0` are recomputed into the right hours after a clock correction.

### B4. Rule summarizer and LLM adapter

Files: `sdrscan/summarize.py`, `sdrscan/llm.py`, `tests/test_summarize.py`,
`tests/golden/digest_week.json`.

Interfaces (spec 5.12 and section 8):

```python
def build_digest(store: Store, health: Health, now: float, *,
                 include_transcripts: bool = False) -> dict
class ProseEngine(typing.Protocol): def rewrite(self, digest: dict) -> str | None
class NullProse: ...
class LlamaCliProse:  # llama-cli -m MODEL -t 2 -n 300 --temp 0.2 -no-cnv -p PROMPT, nice 19
```

Tests first:

- From the simulated week: busiest lists exclude always-on and spur channels; a channel added
  on day 6 is in `new`; a 5x burst at 03:00 is in `unusual`; a usually busy channel silenced
  for 4 h is in `quiet`.
- The digest is deterministic: two builds give byte-identical JSON (`sort_keys=True`, fixed
  float rounding). Encoding twice is not enough proof, so compare against the golden file.
- A worst-case scene with 500 active channels gives a gzipped digest of at most 4 KB.
- Transcripts below confidence 0.6 never appear; with `include_transcripts=False` none
  appear.
- `LlamaCliProse` builds the expected argv and returns `None` in any mode other than
  `normal`. A live test runs only when `SDRSCAN_LLAMA_BIN` is set.

### B5. Health monitor and governor

Files: `sdrscan/health.py`, `tests/test_health.py`.

Interface (spec 5.13): `HealthMonitor(cfg, clock, sys_root: Path)`, `sample() -> Health`,
`mode() -> str`, `notify_watchdog()`.

Tests first (fake `sys_root` with thermal and USB files):

- A thermal file stepped 60, 71, 79, 83 °C gives modes `normal`, `hot`, `very_hot`,
  `critical`, with hysteresis of 3 °C on the way down.
- A removed USB id gives `no_dongle`.
- The watchdog ping is not sent when the last IQ block is older than 10 s.
- Missing `vcgencmd` is not an error.

### B6. Uplink digest writer with byte budget

Files: `sdrscan/uplink.py`, `tests/test_uplink.py`.

Interface (spec 5.14 and section 10):

```python
class Target(typing.Protocol): def put(self, name: str, body: bytes, etag: str | None) -> Response
class FileTarget: ...  class HttpPutTarget: ...  class HttpPostTarget: ...
class Ledger:  # persisted in the store; month_bytes, cap_bytes, state()
class DigestPublisher:
    def __init__(self, target: Target, ledger: Ledger, outbox: Path, clock) -> None
    def publish(self, digest: dict, now: float) -> PushResult
```

Tests first (a local stdlib HTTP server on 127.0.0.1 stands in for the host):

- One hourly publish sends one gzipped object and charges body plus headers plus the TLS
  estimate to the ledger.
- Above 80% of the cap, publishes happen only every 3 h; at the cap only a daily health beat
  of at most 1 KB goes out.
- With the server returning 500 for 30 virtual hours, the outbox keeps at most 48 digests,
  and on recovery only the newest digest plus one coalesced day file per missed day are sent.
- Credentials come from the env file, never from argv or logs (assert they do not appear in
  captured log output).

### B7. Dashboard and live server

Files: `sdrscan/web/index.html.tmpl`, `sdrscan/dashboard.py`, `sdrscan/live.py`,
`tests/test_dashboard_static.py`, `tests/test_dashboard_browser.py`, `tests/test_live.py`.

Interfaces:

```python
def build_dashboard(digest_url: str, mode: str, out_dir: Path) -> Path   # writes hashed index
class LiveServer:
    def __init__(self, store, clips, ledger, bind: str, port: int) -> None
    def serve_forever(self) -> None
```

Requirements (spec section 11): phone-first, one column at 360 px, two columns from 720 px,
touch targets at least 44 px, light and dark themes with a toggle, no external assets, no
auto-refresh, health strip with stale and offline states, play buttons only in live mode.
Load the `frontend-design` skill guidance before writing the template.

Tests first:

- Static: the built page has no `http://` or `https://` references and no `<link>` or
  `<script src>`; gzipped size at most 10 KB; it contains `meta name="viewport"` and
  `prefers-color-scheme`.
- Browser (marked `browser`, skipped without Playwright): Chromium and WebKit at 360, 390 and
  768 px in light and dark, for the week golden digest plus empty, stale (3 h old) and
  health-red digests. Assert `document.scrollingElement.scrollWidth <=
  document.scrollingElement.clientWidth`, every `button` and `[role=tab]` at least 44 x 44,
  the only network request is the digest, and no console errors. Save screenshots under
  `scanner/build/screens/` (gitignored).
- Without Playwright, do the same check by hand with Chrome DevTools MCP (`resize_page`,
  `take_screenshot`, an `evaluate_script` for `scrollWidth`), and view every screenshot.
- Live: `/api/now` returns gzip with an ETag and answers 304 on a matching `If-None-Match`;
  every response size is charged to the ledger; at the cap it returns 429; `/clip/{id}`
  supports a `Range` request.

### B8. CLI and benchmark

Files: `sdrscan/cli.py`, `sdrscan/bench.py`, `sdrscan/__main__.py`, `tests/test_cli.py`.

Commands (spec 5.16): `run`, `simulate`, `digest`, `dashboard`, `bench`, `health`.

Tests first:

- `sdrscan simulate --scene tests/scenes/week_basic.yaml --days 1 --out TMP` writes
  `scan.sqlite`, `digest.json.gz` and `index.html`, in under 30 s on x86.
- `sdrscan bench --json` prints hop ms, demod ms per channel and VAD ms per frame.
- `sdrscan run --source synthetic --scene ...` exits cleanly on SIGTERM and flushes the store.

Manual check after B8: run `simulate --days 7`, serve the output directory with
`python -m http.server --bind 127.0.0.1`, and look at the page at 360 px in both themes.

### B9. Dockerfile for arm64

Files: `scanner/Dockerfile`, `scanner/.dockerignore`.

Stages:

1. `base`: `debian:trixie-slim`; apt `python3 python3-venv python3-numpy python3-scipy
   librtlsdr2 rtl-sdr opus-tools tini`; venv with `--system-site-packages`; pip
   `pyrtlsdr webrtcvad pyyaml` and the package.
2. `test`: base plus `pytest`, the tests and scenes.
3. `runtime`: base only, non-root user `sdrscan`, `ENTRYPOINT ["tini","--","sdrscan","run"]`,
   `HEALTHCHECK` on `sdrscan health`.

Verification: the commands in "Verification commands". Record the image size and the qemu
test result in the commit message. If `webrtcvad` has no arm64 wheel, add `build-essential
python3-dev` to a builder stage only.

### B10. systemd unit and install kit

Files: `scanner/deploy/sdrscan.service`, `scanner/deploy/install.sh`,
`scanner/deploy/uninstall.sh`, `scanner/deploy/config.template.yaml`,
`tests/test_deploy_scripts.py`.

Unit settings: spec section 12. `install.sh` steps, each printed and skipped under
`--dry-run`:

1. Preflight: OS release, free disk at least 3 GB, DVB module blacklist present (report
   only), nothing holding the dongle, `rtl_test -s 2400000` for 10 s with no lost samples.
2. `docker load` the image tarball and tag the previous image as `sdrscan:previous`.
3. Write `/etc/sdrscan/config.yaml` from the template only if it is missing.
4. Install the unit, `daemon-reload`, `enable --now`.
5. Poll `/healthz` for 120 s; on failure, retag `previous`, restart, and exit non-zero.

Tests: `shellcheck` passes; `install.sh --dry-run` with a fake `PATH` (stub `docker`,
`systemctl`, `rtl_test`) prints every step and changes nothing; the unit file contains no
audio-related directives and no ordering against other units.

Do not run either script on the Pi. When the Pi is reachable, stage the run for the owner with
`stage-for-owner`.

### B11. Documentation

Files: `scanner/README.md`, an update to the repository `README.md` (one paragraph and a
link), and `docs/claude/INDEX.md` entries for the spec and plan.

Content: what the scanner does, how to run `simulate` and view the dashboard, the byte
budget table from the spec, the open measurements list, and the install kit steps. Public-repo
check before commit: `git diff --cached | grep -nE '192\.168|100\.[0-9]+\.|\.ts\.net|/home/'`
returns nothing.

### B12. Optional: first external decoder job (only if time remains)

Files: `sdrscan/jobs/rtl433.py`, `tests/fakes/rtl_433`, `tests/test_job_rtl433.py`.

An `ExternalJob` that runs `rtl_433 -F json -M level -M time:unix:utc -f 433.92M` for 5
minutes in each hour through `DongleLease`, normalizes JSON lines to decode events, and hashes
device IDs before they enter the digest. A fake `rtl_433` script on `PATH` replays JSON lines
in tests.

---

## Order and dependencies

```
A1 -> A2 -> A3 -> A4 -> A5 -> A6 -> A7 -> A8 -> A9 (checkpoint: arm64 test build)
                                              |
B1 (needs A7) -> B2 (needs A8) -> B3 (A8) -> B4 (B3) -> B5 -> B6 (B4) -> B7 (B4, B6)
              -> B8 (all) -> B9 -> B10 -> B11 -> [B12]
```

## Done criteria for the MVP

- `python -m pytest -q` passes on x86, including the 7-day scene.
- The arm64 `test` target passes its quick subset under qemu, and the `runtime` image size is
  recorded.
- `sdrscan simulate --days 7` produces a dashboard that passes the browser checks at 360, 390
  and 768 px in both themes, with screenshots viewed.
- The gzipped digest is at most 4 KB and the gzipped page at most 10 KB.
- `shellcheck` passes on the deploy scripts, and `install.sh --dry-run` runs cleanly with stubs.
- Nothing has been run on, or copied to, the Pi.
