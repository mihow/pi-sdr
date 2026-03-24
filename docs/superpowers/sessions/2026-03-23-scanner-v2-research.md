# Scanner V2 Research Session — 2026-03-23

## Goal

Validate the scanner v2 design spec against OpenWebRX+ internals. Answer the open questions from the spec by reading the actual source code.

## Open Questions to Answer

1. **Server-side extension:** Does OpenWebRX+ have a plugin API? Can we add a scanner plugin without forking?
2. **SDR retune API:** Can we retune center frequency programmatically from Python?
3. **FFT data access:** How do we read FFT power spectrum server-side?
4. **Audio tee for recording:** Can we tap the demod audio for recording?
5. **Mobile audio:** WebSocket audio streaming architecture.

## Resources

- OpenWebRX+ fork: `/home/michael/Projects/Radio/OpenWebRX/openwebrx+`
- Fork PRs: https://github.com/mihow/openwebrxplus/pulls
- Key PRs to study:
  - PR #6: IQ FileSource for automated testing
  - PR #5: Signal Classification Plugin
  - PR #7: SDR integration tests
  - PR #3: LoRa demodulator
  - PR #10: PyTorch signal classification
- Full research doc: `docs/superpowers/research/2026-03-23-openwebrx-internals.md`

## Findings

### 1. Server-side extension points
**No formal plugin API**, but well-defined extension points:
- Routes: `owrx/http.py:92` — add `StaticRoute` or `RegexRoute` to `Router.__init__()`
- Controllers: subclass `Controller`, implement `indexAction()`
- Background services: implement `SdrSourceEventClient`, follow `ServiceHandler` pattern (`owrx/service/__init__.py:20`)
- The `ServiceScheduler` pattern shows how to auto-switch profiles and run background logic

**Verdict:** Must modify the codebase (fork), but the patterns are clean. No plugin system to work around.

### 2. SDR retune API
**YES — trivial.** `sdrSource.setCenterFreq(freq)` at `owrx/source/__init__.py:283`. Property system propagates to hardware. For ConnectorSource (RTL-SDR), sends TCP command. For DirectSource, restarts process. Profile switching via `activateProfile(id)`.

**Verdict:** Scanner can retune the SDR programmatically without any UI involvement.

### 3. FFT data access
**YES — two approaches:**
- Register as spectrum client: `sdrSource.addSpectrumClient(obj)` — gets ADPCM-compressed frames
- Create own `FftChain` from `sdrSource.getBuffer().getReader()` — gets raw uncompressed data
- FFT uses csdr C library (FFTW under the hood) via pycsdr bindings

**Verdict:** Creating our own FftChain is cleaner for the scanner — we want raw power data, not ADPCM-compressed display data.

### 4. Audio recording tap
**ALREADY EXISTS — twice:**
- **Client-side:** `AudioEngine.js:337-408` — MP3 recording via lamejs in browser
- **Server-side:** `AudioRecorder` at `csdr/chain/toolbox.py:260` — `ServiceDemodulator` with `Mp3Recorder` and **SNR-based squelch** (`SnrSquelch`). Configured via `rec_squelch`, `rec_hang_time`, `rec_produce_silence`.

**Verdict:** The server-side `AudioRecorder` is almost exactly what scanner v2 needs. We can either reuse it directly or model our recording chain after it. The `SnrSquelch` is particularly relevant.

### 5. WebSocket audio architecture
- Binary protocol with type tags: `0x01` FFT, `0x02` audio, `0x03` secondary FFT, `0x04` HD audio
- ADPCM compression default, 12 kHz standard / 48 kHz HD
- Per-client DSP chains reading from shared IQ buffer
- AudioWorklet with ring buffer for playback
- **Mobile gap:** Only `resume()` from user gesture, no handling for background tabs or screen lock

**Verdict:** The WebSocket protocol is simple and well-defined. Mobile audio will need extra work (keep-alive, background handling). The scanner's mobile UI can use the same protocol.

### 6. Existing PRs — relevant patterns
- **Signal classifier (PR #5):** `ThreadModule` tapping `selectorBuffer` — reuse for FFT channelizer
- **LoRa (PR #3):** 6-file pattern for new decoders (modes.py, feature.py, module, chain, parser, dsp.py)
- **IQ FileSource (PR #6):** Hardware-free testing with `.cf32` files and `pv` rate limiting
- **PyTorch classifier (PR #10):** Simple 3-feature pre-classifier (envelope, inst-freq, spectral peak)
- **Upstream `skip_scan` branch:** Adds "scannable" field to bookmarks
- **Upstream `smart_squelch` branch:** SNR-based squelch for recording

## Decisions

1. **Fork approach confirmed.** No plugin API, but extension points are clean. We modify the fork directly.
2. **Scanner service pattern:** Follow `ServiceHandler` model — implement `SdrSourceEventClient`, create own `FftChain`, retune via `setCenterFreq()`.
3. **Recording:** Reuse/extend the existing `AudioRecorder` service chain, not build from scratch.
4. **Classification:** Start with simple heuristics (power, bandwidth, modulation type), use PR #5's `ThreadModule` pattern. ML classification (PR #10 approach) is a future stage.
5. **Mobile UI:** New static HTML/JS at `/scanner`, connect via same WebSocket protocol. Must handle mobile audio quirks (gesture unlock, background tab).
6. **Testing:** Merge IQ FileSource (PR #6) to enable hardware-free development.

## Follow-ups (GitHub issues to create)

1. Set up dev Docker Compose for OpenWebRX+ fork with SDRplay support
2. Merge/cherry-pick IQ FileSource (PR #6) for testing
3. Implement scanner service (SdrSourceEventClient + FftChain + retune loop)
4. Implement signal detection (FFT peak finding, noise floor tracking)
5. Implement classification pipeline (modulation ID, VAD)
6. Implement recording service (extend AudioRecorder pattern)
7. Create SQLite logging schema
8. Build mobile scanner UI (activity feed + listening view)
9. Add scanner API endpoints (WebSocket + REST)
10. Mobile audio handling (gesture unlock, background keep-alive)
