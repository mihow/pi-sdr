# Scanner Detection Pipeline Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Before starting:** (a) read the supporting research doc at `docs/claude/research/2026-04-11-scanner-signal-mining.md` — it has the per-band noise floors and validated-active channels you'll need for test fixtures; (b) re-verify that the file:line references in this plan still match the current source on `feat/radio-scanner` before writing test code against them. This plan was written from a snapshot; the branch may have moved.

**Goal:** Fix the four stacked bugs in the `openwebrx-scanner` detection pipeline so that voice transmissions on real channels are correctly identified and recorded, instead of the current state (every channel fires every scan pass, zero voice detections, zero recordings).

**Architecture:** The bugs are in `scanner/*.py` on the `feat/radio-scanner` branch. Each bug independently blocks voice detection, so fixes must land in order: hardware config (gain) → data source (squelch) → analysis path (sample rate) → decision path (hold buffer). A fifth bug discovered during log mining — ~73.6 dB saturation ceiling — is folded in as Task 6. TDD: every code change is preceded by a failing test added to `scanner/test_e2e.py` or a new `scanner/test_voice_detect.py`.

**Tech Stack:** Python 3.13, SoapySDR, numpy, scipy, webrtcvad, Flask, Docker. Scanner source is in `scanner/` subdir of pi-sdr with its own `Dockerfile`, `requirements.txt`, and `test_e2e.py`.

**Branch:** `feat/radio-scanner` (19 commits beyond `main`). Create a worktree from it before editing.

**Evidence baseline:** 2-day run on a production pi-sdr deployment produced 2,034,719 "signal" events across 143 channels with zero voice detections, zero hold dwells, zero recordings. Per-channel smeter distributions and validated-active channels in `docs/claude/research/2026-04-11-scanner-signal-mining.md`.

---

## File Structure

Files touched by this plan. Start reading `scanner/scanner.py` first — it's the orchestration layer and references every other module.

```
scanner/
├── scanner.py                 # Core scan loop + voice analysis orchestration (modify: bugs 1,2,3)
├── voice_detect.py            # VoiceDetector class (may need pitch-lag fix if output rate changes)
├── demod.py                   # demod_channel (no code changes — call sites change)
├── sdr_backend.py             # SDR device wrapper (modify: bug 4 default gain)
├── fft_scan.py                # compute_channel_power (investigate: bug 5 saturation)
├── web.py                     # Flask API (modify: bug 6 freq-keyed aggregation)
├── recorder.py                # WAV recorder (unchanged, just verify path works)
├── test_e2e.py                # Existing integration tests (add new cases)
├── test_voice_detect.py       # NEW — unit tests for voice pipeline with known-speech fixture
└── test_data/                 # NEW contents — known-voice WAV + known-silence WAV
```

---

## Task 1: Worktree, snapshot current state, create test fixtures

**Files:**
- Create worktree at `../pi-sdr-scanner-fixes` from `origin/feat/radio-scanner`
- Create: `scanner/test_data/known_voice_16k.wav` (downloaded or recorded)
- Create: `scanner/test_data/known_silence_16k.wav`
- Create: `scanner/test_voice_detect.py`

- [ ] **Step 1: Create worktree and verify current state**

```bash
cd /home/michael/Projects/Radio/pi-sdr
git fetch origin feat/radio-scanner
git worktree add -b fix/scanner-detection ../pi-sdr-scanner-fixes origin/feat/radio-scanner
cd ../pi-sdr-scanner-fixes
ls scanner/
cat scanner/requirements.txt
```

Expected: scanner directory with `scanner.py`, `voice_detect.py`, `demod.py`, etc. Stay in this worktree for the rest of the plan.

- [ ] **Step 2: Run the existing test suite to establish the baseline**

```bash
cd scanner
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 -m pytest test_e2e.py -v 2>&1 | tee /tmp/baseline.txt
```

Record which tests currently pass and which fail. Expected: most pass because they don't exercise the broken paths. Tests that exercise `_analyze_audio` or `_hold_on_voice` (if any) are the ones likely to need updates after fixes.

- [ ] **Step 3: Obtain a known-voice fixture WAV**

Two options — pick whichever is faster:

  1. Record ~5 seconds of your own voice as 16-bit mono PCM 16 kHz into `scanner/test_data/known_voice_16k.wav`. On Linux: `arecord -f S16_LE -r 16000 -c 1 -d 5 scanner/test_data/known_voice_16k.wav`
  2. Download a short speech sample from a public-domain source (LibriVox clip, a few seconds, convert with `ffmpeg -i input.mp3 -ar 16000 -ac 1 -sample_fmt s16 scanner/test_data/known_voice_16k.wav`).

Create a matching `known_silence_16k.wav` — 5 seconds of silence at 16 kHz mono: `sox -n -r 16000 -c 1 scanner/test_data/known_silence_16k.wav trim 0.0 5.0`.

Verify with: `soxi scanner/test_data/known_voice_16k.wav` — expect "Sample Rate: 16000, Channels: 1, Precision: 16-bit".

- [ ] **Step 4: Write failing unit tests in `scanner/test_voice_detect.py`**

Read `scanner/voice_detect.py` and `scanner/scanner.py:306-353` first so the test imports match the actual class/function signatures. Then write:

```python
# scanner/test_voice_detect.py
"""Unit tests for the voice detection pipeline with known-speech fixtures.

These tests drive the detector directly without going through the SDR or the
scanner loop. They are the regression gate for bugs 2 and 3 in
docs/claude/research/2026-04-11-scanner-signal-mining.md.
"""
import wave
from pathlib import Path
import numpy as np
from scanner.voice_detect import VoiceDetector, Detection

FIXTURE = Path(__file__).parent / "test_data"


def load_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        return w.readframes(w.getnframes())


def test_voice_detector_confirms_known_speech():
    """Known-voice clip must classify as VOICE with confidence >= 0.3."""
    pcm = load_pcm(FIXTURE / "known_voice_16k.wav")
    det = VoiceDetector(sample_rate=16000)
    for i in range(0, len(pcm) - det.frame_bytes, det.frame_bytes):
        det.process_frame(pcm[i : i + det.frame_bytes])
    decision, conf = det.get_decision()
    assert decision == Detection.VOICE, f"expected VOICE, got {decision} (conf={conf})"
    assert conf >= 0.3


def test_voice_detector_rejects_silence():
    """Known-silence clip must not classify as VOICE."""
    pcm = load_pcm(FIXTURE / "known_silence_16k.wav")
    det = VoiceDetector(sample_rate=16000)
    for i in range(0, len(pcm) - det.frame_bytes, det.frame_bytes):
        det.process_frame(pcm[i : i + det.frame_bytes])
    decision, _ = det.get_decision()
    assert decision != Detection.VOICE
```

- [ ] **Step 5: Run the tests and verify they fail (or unexpectedly pass)**

```bash
cd scanner
python3 -m pytest test_voice_detect.py -v
```

Expected:
- `test_voice_detector_confirms_known_speech` — **FAIL** (known bug: sample-rate math inside detector is consistent with 16 kHz so this *might* pass on pre-sliced WAV input even though it fails in the scanner context; if it passes, add a second test that mimics what scanner.py does — pass 48 kHz audio labeled as 16 kHz — which must fail.)
- `test_voice_detector_rejects_silence` — probably PASS.

If the first test passes, it confirms the bug is purely at the call site (`scanner.py:289-293` doesn't pass `output_rate=16_000`), not in the detector itself. That's useful; note it in the commit.

- [ ] **Step 6: Commit the test fixture and failing tests**

```bash
git add scanner/test_data/ scanner/test_voice_detect.py
git commit -m "test: add voice detector fixtures and failing unit tests"
```

---

## Task 2: Fix SDR gain default (Bug #4)

**Files:**
- Modify: `scanner/sdr_backend.py` — find the gain setup and set a sensible default
- Modify: `scanner/test_e2e.py` — add a test that asserts the default gain is nonzero

- [ ] **Step 1: Read `scanner/sdr_backend.py` and locate gain setup**

```bash
grep -n "gain\|setGain\|getGain" scanner/sdr_backend.py
```

Note the current code — `project_scanner_bugs` memory says gain is effectively 0.0 on the running container. The fix is to either call `device.setGain(stream, channel, 29)` (matches what OpenWebRX+ uses) or to enable AGC via `setGainMode`.

- [ ] **Step 2: Add a failing test in `scanner/test_e2e.py`**

```python
def test_sdr_backend_default_gain_is_nonzero():
    """Scanner must not pin the RTL-SDR at 0 dB gain by default.

    Reason: a 2-day production capture showed gain=0.0 dB with noise floors
    13–28 dB above typical, masking weak signals (see
    docs/claude/research/2026-04-11-scanner-signal-mining.md).
    """
    from scanner.sdr_backend import SDRBackend  # verify actual class name
    backend = SDRBackend()
    backend.open()
    try:
        gain = backend.get_current_gain()  # verify actual method name
        assert gain > 0, f"default gain should be nonzero, got {gain}"
    finally:
        backend.close()
```

Run: `python3 -m pytest test_e2e.py::test_sdr_backend_default_gain_is_nonzero -v`
Expected: FAIL (either because method doesn't exist or gain is 0).

This test requires a real dongle — skip it in CI with `@pytest.mark.skipif(not os.path.exists("/dev/bus/usb"), reason="needs SDR")`.

- [ ] **Step 3: Set a default gain in `sdr_backend.py`**

The exact edit depends on what's there. The fix is one of:
  1. Call `self._device.setGain(SOAPY_SDR_RX, 0, 29.0)` after `makeStream`
  2. Call `self._device.setGainMode(SOAPY_SDR_RX, 0, True)` for AGC

Prefer (1) because AGC on RTL-SDR pumps with strong adjacent-channel signals, which is exactly the environment this scanner runs in. Make it configurable via constructor parameter `gain: float = 29.0` and expose it through the scanner CLI in `scanner/__main__.py`.

- [ ] **Step 4: Run the test, verify it passes**

```bash
python3 -m pytest test_e2e.py::test_sdr_backend_default_gain_is_nonzero -v
```

Expected: PASS, `gain > 0`.

- [ ] **Step 5: Commit**

```bash
git add scanner/sdr_backend.py scanner/test_e2e.py scanner/__main__.py
git commit -m "fix: set default RTL-SDR gain to 29 dB instead of 0"
```

---

## Task 3: Fix voice detector sample-rate mismatch (Bug #2)

**Files:**
- Modify: `scanner/scanner.py:289-293` — pass `output_rate=16_000` to `demod_channel`
- Modify: `scanner/scanner.py:385-388` — same fix in `_hold_on_voice` hot path
- Verify: `scanner/voice_detect.py` already expects 16 kHz (it does, line 32)

- [ ] **Step 1: Add a failing scanner-level integration test in `test_voice_detect.py`**

This test mimics what `scanner.py::_analyze_audio` actually does: calls `demod_channel` then feeds the result to the detector. If Bug 2 is real, the decision will be wrong.

```python
import numpy as np
from scanner.demod import demod_channel
from scanner.voice_detect import VoiceDetector, Detection


def _make_fake_fm_iq_from_voice_wav(wav_path, sdr_sample_rate=2_400_000):
    """Synthesize an FM-modulated IQ stream from a 16k voice WAV.

    Returns complex64 IQ array at sdr_sample_rate, center freq 0.
    """
    import wave
    with wave.open(str(wav_path), "rb") as w:
        assert w.getframerate() == 16000
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio_f = pcm.astype(np.float32) / 32768.0
    # Upsample to sdr_sample_rate and frequency-modulate it
    from scipy.signal import resample_poly
    up = sdr_sample_rate // 16000
    audio_up = resample_poly(audio_f, up, 1).astype(np.float32)
    deviation = 5000.0  # narrowband FM, 5 kHz
    phase = 2 * np.pi * deviation * np.cumsum(audio_up) / sdr_sample_rate
    iq = np.exp(1j * phase).astype(np.complex64)
    return iq


def test_demod_into_detector_confirms_voice():
    """End-to-end: synthetic NFM signal carrying known speech must classify as VOICE.

    This is the regression gate for bug 2 (sample-rate mismatch between
    demod_channel output and VoiceDetector).
    """
    iq = _make_fake_fm_iq_from_voice_wav(FIXTURE / "known_voice_16k.wav")
    audio = demod_channel(
        iq, 2_400_000, center_freq=0, target_freq=0, bandwidth=12500, mod="nfm",
        output_rate=16_000,  # CRITICAL — default is 48000, which breaks detection
    )
    det = VoiceDetector(sample_rate=16000)
    audio_bytes = audio.tobytes()
    for i in range(0, len(audio_bytes) - det.frame_bytes, det.frame_bytes):
        det.process_frame(audio_bytes[i : i + det.frame_bytes])
    decision, conf = det.get_decision()
    assert decision == Detection.VOICE, f"got {decision} conf={conf}"
```

Run: `python3 -m pytest test_voice_detect.py::test_demod_into_detector_confirms_voice -v`
Expected: PASS (because we're explicitly passing `output_rate=16_000`). This test validates the *fix*. Now we need the test that shows the bug: call without `output_rate` and assert it fails.

- [ ] **Step 2: Add a test that captures the current-buggy behavior**

```python
def test_demod_default_output_rate_breaks_detection():
    """Regression: demod_channel() without output_rate returns 48 kHz audio,
    which VoiceDetector(sample_rate=16000) misinterprets. This test documents
    the call-site bug in scanner.py:289-293.

    Should pass BEFORE the fix (captures current broken state) and fail AFTER
    the fix — at which point flip the assertion and rename, or delete.
    """
    iq = _make_fake_fm_iq_from_voice_wav(FIXTURE / "known_voice_16k.wav")
    audio = demod_channel(iq, 2_400_000, 0, 0, 12500, mod="nfm")  # default 48k
    det = VoiceDetector(sample_rate=16000)
    audio_bytes = audio.tobytes()
    for i in range(0, len(audio_bytes) - det.frame_bytes, det.frame_bytes):
        det.process_frame(audio_bytes[i : i + det.frame_bytes])
    decision, _ = det.get_decision()
    # Current state: wrong rate → decision is not VOICE
    assert decision != Detection.VOICE, "unexpected: call site might already be correct"
```

Run both tests: `python3 -m pytest test_voice_detect.py -v`
Expected: both pass, because one captures correct behavior and the other captures broken behavior.

- [ ] **Step 3: Fix the call sites in `scanner.py`**

Edit `scanner/scanner.py` around line 289-293 (inside `_scan` method) to pass `output_rate=16_000`:

```python
audio = demod_channel(
    iq, self.backend.get_sample_rate(),
    window["center"], ch.freq, ch.bandwidth,
    mod=ch.mod,
    output_rate=16_000,  # fix: detector expects 16 kHz
)
```

And around line 385-388 in `_hold_on_voice`:

```python
audio = demod_channel(
    iq, self.backend.get_sample_rate(),
    window["center"], ch.freq, ch.bandwidth,
    mod=ch.mod,
    output_rate=16_000,
)
```

Note: the original call at line 289 passes `mod=ch.mod` but the one at line 385 does NOT in the current source (reconfirm when editing — this is a separate bug worth fixing in the same commit because hold mode uses WFM instead of the channel's actual mod type).

- [ ] **Step 4: Delete the regression-captures-broken-state test**

The second test from Step 2 is now obsolete (the bug is fixed). Delete it so nobody is confused by an inverted assertion.

- [ ] **Step 5: Run all voice tests, verify green**

```bash
python3 -m pytest test_voice_detect.py -v
```

Expected: `test_voice_detector_confirms_known_speech`, `test_voice_detector_rejects_silence`, and `test_demod_into_detector_confirms_voice` all pass.

- [ ] **Step 6: Commit**

```bash
git add scanner/scanner.py scanner/test_voice_detect.py
git commit -m "fix: pass output_rate=16000 to demod_channel for voice detection"
```

---

## Task 4: Fix squelch scale — adaptive noise-floor-relative (Bug #1)

**Files:**
- Modify: `scanner/scanner.py:55` (the `squelch_level` default is wrong as an absolute)
- Modify: `scanner/scanner.py:232-274` (compute per-window noise floor from FFT, compare relative)
- Modify: `scanner/scanner.py:415` (same comparison in hold mode)
- Modify: `scanner/fft_scan.py` — add a helper to compute per-window noise floor
- Modify: `scanner/test_e2e.py` — add assertion on per-scan noise floor behavior

- [ ] **Step 1: Add a failing test for adaptive squelch**

Read `scanner/fft_scan.py` first to find `compute_channel_power` and understand how it uses bins. Then write:

```python
def test_compute_window_noise_floor():
    """Noise floor helper returns the median power of off-channel FFT bins.

    Regression: adaptive squelch requires a band-relative noise floor.
    See docs/claude/research/2026-04-11-scanner-signal-mining.md for evidence
    that band noise floors span 13 dB in real RF.
    """
    import numpy as np
    from scanner.fft_scan import compute_window_noise_floor

    # Synthesize IQ: pure noise at -40 dB with one bright tone at +10 dB at 100 kHz offset
    n = 262144
    sample_rate = 2_400_000
    noise = (np.random.randn(n) + 1j * np.random.randn(n)).astype(np.complex64) * 0.01
    t = np.arange(n) / sample_rate
    tone = np.exp(2j * np.pi * 100_000 * t).astype(np.complex64) * 0.5
    iq = noise + tone

    floor_db = compute_window_noise_floor(iq, sample_rate)
    # Expect ~ -40 dB (noise power), not contaminated by the +10 dB tone
    assert -45 < floor_db < -35, f"noise floor {floor_db} off by too much"
```

Expected: FAIL (helper doesn't exist yet).

- [ ] **Step 2: Implement `compute_window_noise_floor` in `fft_scan.py`**

```python
def compute_window_noise_floor(iq: np.ndarray, sample_rate: float) -> float:
    """Median power of FFT bins, in dB. Robust to a few strong signals.

    Use as the adaptive squelch baseline: signals with bin power >
    floor_db + SNR_margin are considered active.
    """
    fft = np.fft.fftshift(np.fft.fft(iq))
    bin_power = (fft * np.conj(fft)).real / len(iq)
    bin_db = 10.0 * np.log10(bin_power + 1e-12)
    return float(np.median(bin_db))
```

- [ ] **Step 3: Run the test — verify it passes**

```bash
python3 -m pytest test_e2e.py::test_compute_window_noise_floor -v
```

- [ ] **Step 4: Change squelch semantics in `scanner.py`**

Replace the absolute squelch field with an SNR-above-floor margin:

```python
# scanner.py state class (around line 55)
squelch_snr_db: float = 10.0  # signal must be this many dB above window noise floor
```

In the scan loop (around line 232-274), after computing `powers`:

```python
from .fft_scan import compute_window_noise_floor
noise_floor_db = compute_window_noise_floor(iq, self.backend.get_sample_rate())
self.state.current_noise_floor = noise_floor_db
# ... later
if powers[ch.freq] > noise_floor_db + self.state.squelch_snr_db:
    # Signal detected
    ...
```

Same fix in `_hold_on_voice` around line 415 — compute floor from the hold-mode IQ block.

Update the API state dict in `web.py` to expose `squelch_snr_db` and `current_noise_floor` (rename from `squelch_level`). Update the frontend if it reads `squelch_level` anywhere.

- [ ] **Step 5: Update test_e2e.py for the new API**

Replace any references to `state.squelch_level` with `state.squelch_snr_db`. Add a test:

```python
def test_adaptive_squelch_does_not_trigger_on_noise():
    """Pure-noise IQ should not cause every channel to fire.

    Reason: 2-day production capture showed squelch_level=-45 triggered on
    every channel every pass because compute_channel_power returns positive dB.
    """
    # Construction of the scanner with a noise-only backend is tricky —
    # either mock sdr_backend.read_iq or spin the real scan loop for one pass.
    # Pick whichever you can make work in 15 minutes. The assertion is:
    #   sum of signal_counts across all channels after one scan cycle == 0
```

Write the actual fixture based on what's easiest given the existing test_e2e.py patterns.

- [ ] **Step 6: Commit**

```bash
git add scanner/scanner.py scanner/fft_scan.py scanner/web.py scanner/test_e2e.py
git commit -m "fix: replace absolute squelch with adaptive SNR-above-noise-floor"
```

---

## Task 5: Fix hold mode IQ buffer — accumulate audio across reads (Bug #3)

**Files:**
- Modify: `scanner/scanner.py:355-434` (`_hold_on_voice`)

- [ ] **Step 1: Add a failing test**

```python
def test_hold_mode_accumulates_enough_audio_to_decide():
    """_hold_on_voice must feed the detector enough audio to reach a non-PENDING
    decision within one iteration of its while loop.

    Regression: read_iq(65536) at 2.4 Msps = 27ms → ~432 samples at 16 kHz,
    less than one 30ms frame. Detector needs ≥10 frames (~300ms) before it
    returns anything other than PENDING.
    """
    # Mock the backend to return a known-voice IQ stream on each read_iq call
    # Call _hold_on_voice with a stop event that fires after one loop iteration
    # Assert current_detection != "pending" after exit
```

Writing this requires mocking `SDRBackend` — either use `unittest.mock` or create a fake backend class in the test file. Exact code depends on existing test patterns; read the test_e2e.py first for the fake/mock style in use.

- [ ] **Step 2: Fix the buffer size and accumulation**

Option A (simpler): increase the read size.

```python
# scanner.py line ~379
iq = self.backend.read_iq(524288)  # ~218 ms at 2.4 Msps — enough for ≥10 frames at 16 kHz
```

Option B (better): keep 65536 reads (lower latency) but accumulate audio into a persistent detector window across reads. The `VoiceDetector.window` already accumulates across `process_frame` calls — but `_analyze_audio` currently calls `detector.reset()` at the start. Move the reset outside the hold loop so the window grows across reads.

Prefer B. The minimal edit:

```python
# In _hold_on_voice, before the while loop:
self.detector.reset()  # fresh window for this dwell

# Inside the loop — do NOT call _analyze_audio (which resets). Instead:
audio_bytes = audio.tobytes()
offset = 0
while offset + self.detector.frame_bytes <= len(audio_bytes):
    self.detector.process_frame(audio_bytes[offset : offset + self.detector.frame_bytes])
    offset += self.detector.frame_bytes
detection, confidence = self.detector.get_decision()
```

- [ ] **Step 3: Run the test, verify green**

- [ ] **Step 4: Run ALL tests to check for regressions**

```bash
python3 -m pytest scanner/ -v
```

Expected: everything green.

- [ ] **Step 5: Commit**

```bash
git add scanner/scanner.py scanner/test_e2e.py
git commit -m "fix: accumulate audio across hold-mode reads so detector reaches a decision"
```

---

## Task 6: Investigate saturation ceiling at ~73.6 dB (Bug #5 from research)

**Files:**
- Read-only first: `scanner/fft_scan.py::compute_channel_power`
- Possibly modify: `scanner/fft_scan.py` or `scanner/sdr_backend.py`

- [ ] **Step 1: Read `compute_channel_power`**

```bash
grep -n "compute_channel_power" scanner/fft_scan.py
```

Identify how it converts FFT bins to dB, whether it clamps or normalizes against a fixed reference, and what units the output is in. Expect to see `10 * log10(...)` math.

- [ ] **Step 2: Write a diagnostic test**

```python
def test_channel_power_does_not_saturate_on_strong_signal():
    """compute_channel_power must scale linearly with input signal amplitude,
    not saturate at a fixed ceiling.

    Regression: 2-day production capture shows max smeter clustered at
    73.5–73.7 dB for a dozen unrelated channels. This looks like either
    8-bit ADC clip at gain=0 or a fixed clamp in the function itself.
    """
    import numpy as np
    from scanner.fft_scan import compute_channel_power

    sample_rate = 2_400_000
    center = 146_000_000
    ch_freq = 146_000_000
    bw = 12500

    def one_tone(amplitude):
        n = 262144
        t = np.arange(n) / sample_rate
        # DC tone (center freq) at given amplitude
        return (np.ones(n, dtype=np.complex64) * amplitude).astype(np.complex64)

    p_low = compute_channel_power(
        one_tone(0.01), sample_rate, center, [ch_freq], [bw]
    )[ch_freq]
    p_high = compute_channel_power(
        one_tone(0.5), sample_rate, center, [ch_freq], [bw]
    )[ch_freq]

    delta = p_high - p_low
    # 50x amplitude = 34 dB power increase — should appear in the output
    assert delta > 30, f"power did not scale linearly: {p_low} → {p_high}"
```

- [ ] **Step 3: Run the test**

```bash
python3 -m pytest test_e2e.py::test_channel_power_does_not_saturate_on_strong_signal -v
```

If it PASSES, the function is fine — the 73.6 dB ceiling is from 8-bit ADC clip at gain=0, which Task 2 already mitigates. Document the finding and skip to Step 6.

If it FAILS, there's a clamp in the function. Trace it and remove the clamp.

- [ ] **Step 4 (conditional): Fix the clamp in `fft_scan.py`**

Depends on what's found. Likely candidates: a `min(x, 73.0)` somewhere, a normalization against a hard reference, or a dtype that overflows.

- [ ] **Step 5 (conditional): Re-run the test, verify green**

- [ ] **Step 6: Update the research doc if the root cause was different from the hypothesis**

Edit `docs/claude/research/2026-04-11-scanner-signal-mining.md` "Saturation ceiling" section with the actual finding (ADC clip vs. code clamp).

- [ ] **Step 7: Commit**

```bash
git add scanner/fft_scan.py scanner/test_e2e.py docs/claude/research/2026-04-11-scanner-signal-mining.md
git commit -m "fix: investigate 73dB channel power ceiling (root cause: <fill in>)"
```

---

## Task 7: Persistent volume + fix duplicate channel names

**Files:**
- Modify: `scanner/Dockerfile` (document volume) or `docker-compose.yml` at deploy time
- Modify: `scanner/recorder.py::__init__` — accept output dir from env, default to `/data/recordings`
- Modify: `scanner/scanner.py` — activity log persists to `/data/activity.jsonl` on each event
- Modify: `scanner/web.py` — all channel aggregation keyed by freq, not name
- Add: note in README about required `-v host_path:/data` mount

- [ ] **Step 1: Make recorder output dir configurable**

In `scanner/recorder.py`, change the default from `"recordings"` to reading `SCANNER_DATA_DIR` env var defaulting to `/data/recordings`.

- [ ] **Step 2: Add activity log persistence**

In `scanner/scanner.py::_log_activity`, append each event to `/data/activity.jsonl` in addition to the in-memory deque. Use `json.dumps({...}) + "\n"` and open in append mode per write (concurrency is low; atomicity matters).

- [ ] **Step 3: Fix freq-keyed aggregation in `web.py`**

Find any `channels[name]` or `signal_counts[name]` usage. Change to freq keying. The state dict in `get_state()` can still return `name` for display, but dedup internally by freq.

- [ ] **Step 4: Document the volume in README or deploy script**

Add to scanner README (or create one):

```markdown
## Persistent data

Mount a host directory at `/data` inside the container:

    docker run -v /var/lib/radio-scanner:/data ...

This keeps recordings and activity logs across container restarts.
```

- [ ] **Step 5: Add a test that two same-name channels don't collapse**

```python
def test_duplicate_channel_names_are_distinct_in_state():
    """Two channels with the same name but different freqs must appear
    as two entries in /api/state, not one.

    Regression: scanner config has 14 channels named '2m Repeater' at
    different freqs; web.py previously grouped them by name.
    """
    # Construct a scanner with two channels: name='X' freq=146000000 and
    # name='X' freq=147000000. Call get_state(). Assert len(channels) == 2.
```

- [ ] **Step 6: Commit**

```bash
git add scanner/recorder.py scanner/scanner.py scanner/web.py scanner/README.md scanner/test_e2e.py
git commit -m "feat: persistent data volume + freq-keyed channel aggregation"
```

---

## Task 8: Integration test on real hardware, then deploy

**Files:**
- None (validation only)

- [ ] **Step 1: Rebuild the scanner Docker image**

```bash
cd scanner
docker build -t openwebrx-scanner:fix .
```

- [ ] **Step 2: Stop openwebrx on the target Pi to free the SDR**

```bash
ssh pi@<pi-host> 'docker stop openwebrx'
```

- [ ] **Step 3: Run the new scanner with a persistent volume**

```bash
ssh pi@<pi-host> 'docker rm -f radio-scanner; docker run -d --name radio-scanner \
    --privileged --device /dev/bus/usb \
    -p 8080:8080 \
    -v /var/lib/radio-scanner:/data \
    openwebrx-scanner:fix --auto-start --squelch-snr 10'
```

- [ ] **Step 4: Wait 30 minutes, then check the activity log**

```bash
ssh pi@<pi-host> 'tail /var/lib/radio-scanner/activity.jsonl | jq -C'
ssh pi@<pi-host> 'curl -s http://localhost:8080/api/state | jq "{
    scanning,
    sdr_info,
    voice_total: ([.channels[].voice_count] | add),
    signal_total: ([.channels[].signal_count] | add),
    top_voice: (.channels | sort_by(-.voice_count) | .[0:5] | map({name, freq_mhz, voice_count}))
}"'
```

Expected:
- `sdr_info.current_gain` is ~29 dB, not 0
- `signal_total` is a fraction of what it was before (proportional to real activity, not 14,229 × 143)
- `voice_total` is **nonzero** on at least one of the validated-active channels from the research doc (GMRS 15R, GMRS 17R, GMRS 18R, RR End of Train 161.100). Timing depends on local traffic — check against the diurnal table.
- `/data/recordings` contains at least one .wav file if voice was detected

- [ ] **Step 5: Scanner snapshot for regression comparison**

Capture a new snapshot for future diffs:

```bash
mkdir -p data/scanner-snapshot-$(date +%Y-%m-%d)-post-fix
ssh pi@<pi-host> 'docker logs radio-scanner 2>&1' > data/scanner-snapshot-$(date +%Y-%m-%d)-post-fix/scanner.log
ssh pi@<pi-host> 'curl -s http://localhost:8080/api/state' > data/scanner-snapshot-$(date +%Y-%m-%d)-post-fix/api-state.json
python3 scripts/analyze-scanner-log.py \
    data/scanner-snapshot-$(date +%Y-%m-%d)-post-fix/scanner.log \
    data/scanner-snapshot-$(date +%Y-%m-%d)-post-fix/api-state.json \
    > data/scanner-snapshot-$(date +%Y-%m-%d)-post-fix/report.md
```

Compare against the pre-fix report at `docs/claude/research/2026-04-11-scanner-signal-mining.md`. Key diffs expected:
- signal_count per channel no longer uniform
- voice_count > 0 on at least one channel
- max smeter still clustered at ~73 dB → ADC clip is physical; not clustered → it was a code clamp Task 6 fixed

- [ ] **Step 6: Decide on SDR coexistence with openwebrx**

The scanner still grabs `/dev/bus/usb` directly, so it still conflicts with openwebrx. Either:
  - (a) Document "run one or the other, not both"
  - (b) Add a rtl_tcp daemon as the sole device owner, refactor `sdr_backend.py` to optionally connect via `rtl_tcp://` instead of SoapySDR direct

(b) is a separate plan and a significant lift. (a) is a one-line README change. Pick based on whether the user wants both to coexist on the same Pi.

- [ ] **Step 7: Push and open PR**

```bash
git push -u origin fix/scanner-detection
gh pr create --title "fix: scanner detection pipeline" --body "$(cat <<'EOF'
## Summary
- Restore voice detection that has been broken since feat/radio-scanner first shipped
- Four root causes identified from a 2-day production capture: default gain 0 dB, absolute squelch with wrong scale, 48 kHz audio fed to 16 kHz-configured detector, hold-mode IQ buffer too small to ever reach a decision
- Each fix is independently validated by a unit test; see `scanner/test_voice_detect.py` and new cases in `scanner/test_e2e.py`
- Adaptive squelch is now per-scan-window noise-floor-relative (SNR threshold)
- Persistent `/data` volume added for recordings and activity log
- Investigation of 73 dB ceiling documented in the research doc

Evidence and numbers: `docs/claude/research/2026-04-11-scanner-signal-mining.md`

## Test plan
- [x] Unit: known-voice WAV through VoiceDetector → VOICE
- [x] Unit: known-silence WAV → not VOICE
- [x] Unit: FM-modulated voice IQ through demod_channel + detector → VOICE
- [x] Unit: compute_window_noise_floor returns median in dB, rejects bright bins
- [x] Unit: duplicate-name channels stay distinct in state
- [x] Integration: hardware run on a real Pi with real antenna, captured new snapshot, compared against pre-fix baseline
- [x] Manual: verified at least one voice_count > 0 on a validated-active channel
EOF
)"
```

---

## Self-review notes

This plan is written from a snapshot. Before executing:

1. **Re-read each target file on `feat/radio-scanner` HEAD** — file:line numbers here will have drifted. The `grep -n` steps are for finding, not confirming; don't rely on the exact line numbers.
2. **The test code in this plan is illustrative**, not final. Adapt function/class names to what's actually in the source. The test *assertions* are the contract; the setup code may need to change.
3. **Task 6 is conditional** on what Step 3 finds. If the saturation ceiling is purely an ADC clip (gain=0 effect), the fix is already in Task 2 and Task 6 reduces to updating the research doc.
4. **Task 8 Step 6 is a decision point** that probably needs user input — don't default to refactoring for rtl_tcp without explicit approval.
5. **No placeholder pass:** I deliberately did not provide final test code for Step 1 of several tasks because the exact signatures depend on reading the source. Each such step is marked with "read X first, then...". At execution time, read and write; don't skip reading.
