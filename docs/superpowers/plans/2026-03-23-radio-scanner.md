# Radio Scanner Implementation Plan

> **Status:** v1 prototype working — voice detected on GMRS 7 (462.7125 MHz)

**Goal:** Build a frequency scanner that scans GMRS, HAM, and other voice channels using the existing OpenWebRX+ SDR setup on a Raspberry Pi 5. Detect and stop on voice transmissions. Provide a mobile-friendly web dashboard.

## Architecture

```
┌──────────────────┐  WebSocket (wss://)  ┌──────────────┐
│  radio-scanner   │ ──────────────────→  │  OpenWebRX+  │ ←→ RTL-SDR V4
│  (Python, Docker)│ ← S-meter + audio ── │  (Docker)     │
│  :8080 dashboard │                      │  :8073 web    │
└──────────────────┘                      └──────────────┘
```

The scanner is a **WebSocket client** to OpenWebRX+. It does NOT directly access the SDR hardware — it uses OpenWebRX+ as the SDR engine. This means the OpenWebRX+ waterfall UI remains usable alongside the scanner.

### Key Design Decisions

1. **OpenWebRX+ WebSocket API** over direct SDR access (rtl_fm/pyrtlsdr) — avoids device conflicts, reuses existing infrastructure
2. **Profile-based scanning** — groups frequencies by OpenWebRX+ SDR profiles to minimize retuning
3. **WebRTC VAD** for voice detection — better than spectral-only for FM-demodulated audio
4. **Docker sidecar** — scanner runs as a Docker container alongside OpenWebRX+, communicating over the Docker network
5. **16kHz audio** — required by webrtcvad (12kHz is not a supported sample rate)

## Frequency Database (61 channels)

| Group | Channels | Frequency Range |
|-------|----------|----------------|
| GMRS | 15 (7 simplex + 8 repeater) | 462.550-462.725 MHz |
| FRS | 7 (extra channels 8-14) | 467.562-467.712 MHz |
| HAM 2m | 17 (simplex + repeaters) | 146.520-147.360 MHz |
| HAM 70cm | 7 (simplex + repeaters) | 446.000-449.500 MHz |
| MURS | 5 | 151.820-154.600 MHz |
| Marine VHF | 3 (Ch 9, 13, 16) | 156.450-156.800 MHz |
| NOAA Weather | 7 | 162.400-162.550 MHz |

## Voice Detection

Two-layer approach:
1. **WebRTC VAD** (primary) — Google's voice activity detector, excellent at detecting speech in noisy conditions
2. **Spectral flatness** (secondary) — rejects digital modes (DMR, P25) that have flat spectra vs voice formants

Key findings:
- FM-demodulated noise has very high RMS (~0.58) regardless of signal strength — squelch must be based on S-meter, not audio level
- webrtcvad requires 8/16/32/48 kHz sample rates — 12kHz (OpenWebRX+ default) is NOT supported
- NOAA weather TTS voice has different characteristics than natural speech but webrtcvad detects it well
- The `webrtcvad` pip package requires `setuptools<82` on Python 3.13 (pkg_resources deprecation)

## Protocol Details

### OpenWebRX+ WebSocket Flow

1. Connect: `wss://host:8073/ws/`
2. Handshake: `SERVER DE CLIENT client=radio-scanner type=receiver`
3. Wait for `profiles` message → maps profile names to IDs
4. Select profile: `{"type": "selectprofile", "params": {"profile": "rtlsdr|gmrs"}}`
5. Start DSP: `{"type": "dspcontrol", "action": "start", "params": {"mod": "nfm", "offset_freq": 0, ...}}`
6. Receive: S-meter (JSON `{"type": "smeter", "value": <linear_power>}`), Audio (binary `0x02` prefix)

### Critical learnings:
- `offset_freq` is relative to the profile's center frequency
- `action: "start"` must be sent after each profile switch to start audio
- S-meter values are **raw linear power** — convert with `10*log10(value)` to get dB
- Profile names are prefixed with SDR device name (e.g., "RTL-SDR Blog V4 GMRS/FRS")
- OpenWebRX+ SSL cert is for Tailscale hostname — use `cert_reqs=CERT_NONE` for inter-container connections
- Max clients limit can cause WebSocket close frames (opcode 8) on rapid reconnections

## v2 Roadmap

- [ ] Pipe audio to Claude for summarization
- [ ] Stop scanning based on content criteria (e.g., "emergency", specific callsigns)
- [ ] Audio recording and playback in dashboard
- [ ] Channel activity heatmap / timeline
- [ ] Adjustable dwell time per frequency group
- [ ] CTCSS/DCS tone detection
- [ ] Multi-SDR support (scan + listen simultaneously)

## Files

```
scanner/
├── __init__.py
├── __main__.py          # CLI entry point
├── scanner.py           # Core engine: WebSocket client, profile switching, scan loop
├── voice_detect.py      # WebRTC VAD + spectral pre-filter
├── frequencies.py       # Channel database (GMRS, HAM, MURS, Marine, NOAA)
├── web.py              # Flask dashboard (inline HTML/CSS/JS)
├── requirements.txt    # flask, websocket-client, numpy, webrtcvad
├── Dockerfile          # Python 3.13-slim + gcc for webrtcvad
└── deploy.sh           # SCP deploy helper
```

## Docker Compose Integration

```yaml
scanner:
  build: ./scanner
  container_name: radio-scanner
  restart: unless-stopped
  ports:
    - "8080:8080"
  entrypoint: ["python", "-m", "scanner"]
  command: ["--host", "openwebrx", "--port", "8073", "--ssl", "--auto-start"]
  depends_on:
    - openwebrx
```
