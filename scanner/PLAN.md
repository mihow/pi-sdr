# Plan: Direct SDR Scanning with Audio Streaming

## Context

The radio scanner works — it detected voice on GMRS 7 and NOAA channels during real-world testing on pi-sdr-1. But OpenWebRX+ only supports one active receiver WebSocket client, so the user can't listen through the browser while the scanner runs. Rapid reconnects also triggered a BannedClientException. The fix is to bypass OpenWebRX+ entirely: use SoapySDR to access the RTL-SDR directly, do our own FFT-based scanning and FM demod, and stream audio through the scanner's own dashboard.

## Architecture Change

**Before:** Scanner → OpenWebRX+ WebSocket → pre-demodulated audio + S-meter
**After:** Scanner → SoapySDR → raw IQ → FFT (signal detection) → FM demod (voice detection + audio streaming)

Bonus: wideband FFT scans ALL channels in a 2.4 MHz band window simultaneously instead of one-at-a-time. Full scan cycle drops from ~30s to ~2s.

## Design Principles

- **SDR-agnostic**: `SdrBackend` abstraction supports RTL-SDR now, SDRplay (wider bandwidth) later. Band window size is dynamic based on device sample rate.
- **Recording**: Audio output goes to both browser stream and optional WAV file recording (per-detection or continuous)
- **Announcements**: When locking on voice, play a channel ID — start with a beep/tone, later TTS ("GMRS 7, 462.712"). Injected into the audio stream before the voice audio.

## Phases

### Phase 1: SDR Backend + Wideband FFT

**New: `scanner/sdr_backend.py`**
- `SdrBackend` protocol: open/close, tune, read_iq, get_sample_rate, get_max_bandwidth
- `SoapySdrBackend` class: concrete implementation using SoapySDR
- Band window size derived from `get_max_bandwidth()` (2.4 MHz for RTL-SDR, 10 MHz for SDRplay later)
- Uses `SoapySDR.Device({'driver': 'rtlsdr'})`, sample rate = device max bandwidth

**New: `scanner/fft_scan.py`**
- `compute_channel_power(iq, sample_rate, center_freq, channels)` → dict of freq → power_dB
- FFT 262144 samples (~109ms), measure power at each channel's offset bins
- Replaces per-channel S-meter reads with one FFT per band window

**Modify: `scanner/frequencies.py`**
- Add `BAND_WINDOWS` mapping groups to center frequencies
- Most bands fit in one 2.4 MHz window; HAM 70cm needs two

**Modify: `scanner/scanner.py`**
- Remove all WebSocket code (connect, _receive_loop, _handle_text/binary, _reconnect, _start_dsp, _select_profile)
- Constructor takes `SdrBackend` instead of host/port
- New scan loop: for each band window → tune → read IQ → FFT → check all channels

**Modify: `scanner/__main__.py`**
- Replace `--host/--port/--ssl` with `--gain` (default: auto)
- Instantiate `SoapySdrBackend`, pass to Scanner

**Modify: `scanner/Dockerfile`**
- Change base to `debian:trixie-slim`, install `python3-soapysdr soapysdr-module-rtlsdr python3-numpy` via apt
- Add `scipy` to requirements

**Modify: `docker-compose.yml` on Pi**
- Move USB device from openwebrx to scanner container

### Phase 2: FM Demodulation

**New: `scanner/demod.py`**
- `extract_channel(iq, sample_rate, center_freq, channel_freq, bw)` — frequency-shift + FIR lowpass + decimate (same pipeline as OpenWebRX+ csdr Selector)
- `fm_demodulate(channel_iq, sample_rate, audio_rate=16000)` — phase differentiation + NFM de-emphasis + limiter + AGC (mirrors csdr NFm chain)
- Output format matches existing voice detector contract (16kHz, 16-bit PCM, 30ms frames)

**Modify: `scanner/scanner.py`**
- When signal detected: extract channel from IQ → FM demod → feed to voice detector
- During voice hold: continuous IQ capture → demod → voice detect + audio broadcast

### Phase 3: Audio Streaming to Dashboard

**New: `scanner/audio_stream.py`**
- `AudioBroadcaster` — thread-safe ring buffer, per-client queues, subscribe/unsubscribe

**Modify: `scanner/web.py`**
- Add `flask-sock` WebSocket route at `/ws/audio`
- Send 16kHz 16-bit PCM as binary frames
- Dashboard JS: Web Audio API (`ScriptProcessorNode`) to play raw PCM
- Add mute/unmute button + volume slider
- Channel ID beep (short tone) injected into audio stream when locking on a new channel

**New: `scanner/recorder.py`** (future-ready, Phase 3)
- Record detected voice to WAV files: `recordings/GMRS-7_462.7125_2026-03-23_12-00-05.wav`
- Triggered when voice detected, stops when hold releases
- Optional: continuous recording mode

**Modify: `scanner/requirements.txt`**
- Add `flask-sock`, `scipy`; remove `websocket-client`

### Phase 4: Enhanced Dashboard

**More status indicators:**
- SDR connection state (green/red dot)
- Current band being scanned + progress (window N of M)
- Dwell timer when holding on voice
- Scan cycle time ("Last cycle: 1.8s")
- Activity log (last 100 events: signals found, voice detections with duration)
- Audio level VU meter

**New API:** `GET /api/activity` — recent events

**New state fields:** `sdr_connected`, `current_band`, `scan_index/total`, `channel_dwell_start`, `activity_log`, `scan_cycle_time`

### Phase 5 (stretch): Spectrum Display + TTS
- Render FFT data on `<canvas>` — live wideband spectrum with channel markers
- Data already available from Phase 1, just needs to be exposed + rendered
- TTS channel announcements: use browser `SpeechSynthesis` API to announce channel name before audio plays (replaces beep from Phase 3)

## Files Summary

| File | Action | Phase |
|------|--------|-------|
| `scanner/sdr_backend.py` | New | 1 |
| `scanner/fft_scan.py` | New | 1 |
| `scanner/demod.py` | New | 2 |
| `scanner/audio_stream.py` | New | 3 |
| `scanner/scanner.py` | Major rewrite | 1-3 |
| `scanner/__main__.py` | Modify | 1 |
| `scanner/web.py` | Modify | 3-4 |
| `scanner/frequencies.py` | Modify | 1 |
| `scanner/recorder.py` | New | 3 |
| `scanner/voice_detect.py` | No change | — |
| `scanner/requirements.txt` | Modify | 1, 3 |
| `scanner/Dockerfile` | Modify | 1 |

## Learnings from OpenWebRX+ Source (github.com/mihow/openwebrxplus)

**Why we got banned:** OpenWebRX+ has a "robot score" in `owrx/connection.py` — rapid profile switches increase the score, and at 30+ it triggers a 12-hour ban (`BannedClientException`). Our scanner was hitting this by switching profiles every few seconds.

**Binary WebSocket format:** Type byte prefix — `0x01` = FFT/spectrum, `0x02` = demodulated audio, `0x03` = secondary FFT, `0x04` = HD audio. S-meter sent as JSON with a float value (raw linear power, not dB).

**DSP chain (csdr library, C with Python bindings):** Selector → Demodulator → ClientAudio
- Selector: frequency shift (`-offset/inputRate`), FIR decimate (transition BW = `0.15 * outputRate/inputRate`), bandpass filter
- NFM demod: `FmDemod → Limit → NfmDeemphasis → Agc(max_gain=3, profile=SLOW)`
- Our numpy/scipy implementation follows the same pipeline

**SoapySDR usage:** OpenWebRX+ uses `owrx-connector` (separate C program) as the SDR source, not direct SoapySDR Python bindings. We'll use SoapySDR Python bindings directly, which is simpler for our use case.

## Reference Projects

- [soapy_power](https://github.com/xmikos/soapy_power) — SDR-agnostic power spectrum via SoapySDR (RTL-SDR, SDRplay, Airspy, etc). Reference for FFT power measurement, or use as library.
- [shajen/rtl-sdr-scanner](https://github.com/shajen/rtl-sdr-scanner) — Python scanner with auto-recording. Reference for recording feature.
- [RTLion](https://github.com/RTLion-Framework/RTLion) — Flask-SocketIO web UI with spectrum visualization. Reference for dashboard spectrum display.
- [PySpecSDR](https://www.rtl-sdr.com/pyspecsdr-a-text-user-interface-based-python-rtl-sdr-spectrum-analyzer-and-signal-processor/) — SoapySDR-based FM/AM/SSB demod in Python. Reference for demod code.

## Risks

1. **SoapySDR in Docker** — need to switch from `python:3.13-slim` to `debian:trixie-slim` and use system Python + apt-installed SoapySDR packages
2. **CPU on Pi 5** — 256K FFT ~2ms, FM demod ~5ms, voice detect ~1ms per 30ms frame. Well within budget.
3. **Audio latency** — WebSocket + Web Audio adds 100-300ms. Fine for monitoring.
4. **RTL-SDR settling** — 50ms after retune. Accounted for in scan loop.

## Verification

1. Build scanner container on Pi, verify SoapySDR can open RTL-SDR device
2. Run FFT scan, confirm channel power readings match OpenWebRX+ S-meter values
3. Demod a known NOAA Weather channel, verify audio is intelligible
4. Open dashboard, confirm audio streams and plays in browser
5. Run full scan cycle, verify signals detected and voice classification matches previous results
