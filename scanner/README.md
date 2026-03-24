# Radio Scanner

Wideband frequency scanner with direct SoapySDR access, FFT-based signal detection, multi-mode demodulation (NFM/WFM/AM/USB/LSB), voice detection, and a live web dashboard with spectrum visualization.

## Features

- **Wideband FFT scanning** — checks all channels in a 2.4 MHz band window simultaneously (~2s full scan cycle)
- **143 channels across 19 bands** — FM broadcast, Air Band, HAM 2m/70cm/1.25m/33cm/23cm, GMRS/FRS, MURS, Marine VHF, NOAA Weather, Railroad, Public Safety, Business, ISM 433/900, ADS-B, WX Satellites
- **Multi-mode demod** — NFM, WFM (75 kHz), AM (envelope), USB/LSB (SSB)
- **Voice detection** — WebRTC VAD + spectral analysis (pitch, flatness, ZCR)
- **Audio streaming** — listen in the browser via WebSocket + Web Audio API
- **Live FFT spectrum** — canvas-based display with channel markers and squelch line
- **Channel management** — click to tune/listen, rename, skip, adjust mod type and bandwidth per channel
- **Auto-discover** — FFT peak detection adds unknown signals
- **IQ file playback** — test without hardware using pre-recorded `.cf32` files
- **Recording** — save voice detections to WAV files

## Quick Start

### On Raspberry Pi (with RTL-SDR)

```bash
cd /opt/openwebrx
docker compose up -d scanner
# Dashboard at http://<pi-ip>:8080
```

### Local testing (no hardware)

```bash
# Generate synthetic IQ test files
python -m scanner.test_e2e --generate-iq

# Run with IQ file
python -m scanner --iq-file scanner/test_data/gmrs_voice.cf32 --squelch 20 --auto-start
```

### Run tests

```bash
python -m scanner.test_e2e
```

## CLI Options

```
--driver DRIVER     SoapySDR driver (default: rtlsdr)
--gain GAIN         SDR gain in dB (default: auto)
--iq-file FILE      IQ file for testing (.cf32 format)
--web-port PORT     Dashboard port (default: 8080)
--squelch LEVEL     Squelch level in dB (default: -45)
--auto-start        Start scanning immediately
--record            Enable voice recording to WAV
--record-dir DIR    Recording directory (default: recordings)
```

## Architecture

```
RTL-SDR (SoapySDR)
    │
    ▼
IQ Samples (2.4 Msps complex float32)
    │
    ├──▶ FFT Power Spectrum ──▶ Signal Detection (squelch)
    │                                    │
    │                                    ▼
    ├──▶ Channel Extract ──▶ FM/AM/SSB Demod ──▶ Voice Detection
    │        (FIR filter,        (phase diff,       (webrtcvad +
    │         freq shift,         de-emphasis,        spectral
    │         decimate)           AGC, resample)      analysis)
    │                                    │
    │                                    ▼
    └──▶ FFT Visualization      Audio Streaming ──▶ Browser
              │                 (WebSocket PCM)     (Web Audio API)
              ▼
         Dashboard Canvas
```

## Modules

| File | Purpose |
|------|---------|
| `scanner.py` | Core scan loop, voice hold, tune-to-channel, state management |
| `sdr_backend.py` | SoapySDR hardware abstraction + IQ file playback |
| `fft_scan.py` | Wideband FFT power measurement, peak detection, spectrum data |
| `demod.py` | Channel extraction, NFM/WFM/AM/SSB demodulation |
| `voice_detect.py` | WebRTC VAD + spectral voice classification |
| `audio_stream.py` | WebSocket audio broadcaster (ring buffer, multi-client) |
| `recorder.py` | WAV file recording for voice detections |
| `frequencies.py` | Channel database + band window computation |
| `web.py` | Flask dashboard, REST API, WebSocket audio endpoint |
| `test_e2e.py` | E2E tests, IQ capture/analysis tools, MockSdrBackend |

## Dashboard API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/state` | GET | Full scanner state (channels, FFT, activity log) |
| `/api/scan/start` | POST | Start scanning |
| `/api/scan/stop` | POST | Stop scanning |
| `/api/tune/<freq>` | POST | Tune to channel and stream audio |
| `/api/listen/stop` | POST | Stop listening mode |
| `/api/skip/<freq>` | POST | Toggle channel skip |
| `/api/squelch` | POST | Set squelch level `{"level": -45}` |
| `/api/channel/add` | POST | Add channel `{"freq", "name", "group", "mod", "bandwidth"}` |
| `/api/channel/rename` | POST | Rename channel `{"freq", "name"}` |
| `/api/channel/params` | POST | Set mod/bandwidth `{"freq", "mod", "bandwidth"}` |
| `/api/auto-discover` | POST | Toggle auto-discover `{"enabled": true}` |
| `/api/activity` | GET | Activity log (last 100 events) |
| `/ws/audio` | WebSocket | PCM audio stream (16kHz, 16-bit, mono) |

## Testing Tools

```bash
# Capture IQ from pi-sdr-1 (NOAA, GMRS, HAM, Marine)
python -m scanner.test_e2e --capture-scan --duration 10

# Analyze captured IQ files
python -m scanner.test_e2e --analyze-scan

# Validate NOAA voice detection
python -m scanner.test_e2e --validate-noaa

# Run synthetic pipeline tests
python -m scanner.test_e2e
```
