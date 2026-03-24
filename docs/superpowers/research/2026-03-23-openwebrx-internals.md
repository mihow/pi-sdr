# OpenWebRX+ Internals Research — Scanner V2 Integration Points

> Compiled 2026-03-23 from source analysis of `/home/michael/Projects/Radio/OpenWebRX/openwebrx+`

## 1. Extension Architecture

**No formal plugin API.** OpenWebRX+ is monolithic. Features are added by modifying the codebase directly. However, the extension points are well-defined:

### Adding HTTP endpoints
- `owrx/http.py:92` — `Router.__init__()` holds flat list of `StaticRoute` / `RegexRoute`
- Each route maps URL + HTTP method to a `Controller` subclass
- Pattern: `StaticRoute("/api/scanner", ScannerApiController)`

### Adding controllers
- `owrx/controllers/__init__.py:8` — `Controller(handler, request, options)`
- `handle_request()` dispatches to `indexAction()` by default
- `self.send_response(content, content_type="application/json")` to return data

### Background services
- `owrx/service/__init__.py:20` — `ServiceHandler` implements `SdrSourceEventClient`
- Auto-creates demodulator chains for bandplan frequencies
- `ServiceScheduler` (`owrx/service/schedule.py:208`) switches profiles on schedule when no user connected
- **Key pattern:** Any class implementing `SdrSourceEventClient` can register with an `SdrSource`

## 2. SDR Retune API

### Center frequency
- `owrx/source/__init__.py:283` — `SdrSource.setCenterFreq(frequency)`
- Sets `self.props["center_freq"] = frequency`
- Property change propagates through `PropertyStack` to all subscribers
- For `ConnectorSource` (RTL-SDR): sends `center_freq:{value}\n` over TCP control socket (`owrx/source/connector.py:46`)
- For `DirectSource`: stops and restarts the entire SDR process

### Profile switching
- `owrx/source/__init__.py:274` — `SdrSource.activateProfile(profile_id)`
- Switches `profileCarousel` which fires property changes for center_freq, samp_rate, etc.
- `PropertyCarousel.switch(key)` (`owrx/property/__init__.py:409`) diffs old/new property sets

### Programmatic retune (no UI needed)
```python
source = SdrService.getSource("my_sdr_id")
source.setCenterFreq(146520000)  # retune to 146.520 MHz
source.activateProfile("vhf_profile")  # or switch profile
```

### Client-initiated retune (WebSocket)
- `owrx/connection.py:343` — `{"type": "setfrequency", "params": {"frequency": 146520000}}`
- Profile: `{"type": "selectprofile", "params": {"profile": "sdr_id|profile_id"}}`

## 3. FFT Data Access

### FFT computation pipeline
1. SDR hardware → TCP → `TcpSource` → `Buffer` (IQ samples, COMPLEX_FLOAT)
2. `FftChain` reads from buffer, computes FFT, applies ADPCM compression
3. `SpectrumThread` pumps output to `sdrSource.writeSpectrumData(data)` (`owrx/fft.py:73`)
4. `SdrSource.writeSpectrumData()` (`owrx/source/__init__.py:564`) iterates `self.spectrumClients`
5. Each client's `write_spectrum_data(data)` gets the compressed FFT frame

### Server-side FFT access — two approaches

**Approach A: Register as spectrum client**
```python
sdrSource.addSpectrumClient(my_scanner_obj)
# my_scanner_obj.write_spectrum_data(data) called with each FFT frame
# Data is ADPCM-compressed
```

**Approach B: Create own FftChain**
```python
from csdr.chain.fft import FftChain
reader = sdrSource.getBuffer().getReader()
fft = FftChain(samp_rate=2400000, fft_size=4096, fft_fps=10, ...)
# Read raw uncompressed FFT power data
```

### FFT library
csdr C library (via pycsdr bindings), uses FFTW under the hood.

## 4. Existing Scanner (Client-Side Only)

`htdocs/lib/Scanner.js` — 111 lines, 100% client-side JavaScript.

### How it works
- Gets scannable bookmarks from bookmark bar
- Precomputes FFT bin positions for each bookmark
- On timer (configurable interval), reads single FFT bin per bookmark
- Exponential smoothing: `b.level += (l - b.level) / 3.0`
- If level exceeds squelch threshold, calls `UI.tuneBookmark(b)`

### Limitations (what scanner v2 replaces)
1. Client-side only — no scanning when browser is closed
2. Bookmark-dependent — cannot discover unknown signals
3. Single FFT bin per bookmark — no bandwidth awareness
4. No retuning — only current waterfall bandwidth
5. Simple level detection — no noise floor estimation
6. No recording or logging
7. Sequential round-robin

### Scannable modes
`owrx/bookmarks.py:13` — `SCANNABLE_MODES = ["lsb", "usb", "cw", "am", "sam", "nfm"]`

## 5. Key Integration Points Summary

| Need | Method | Location |
|------|--------|----------|
| Add REST API | Route in `Router.__init__()` | `owrx/http.py:92` |
| New controller | Subclass `Controller` | `owrx/controllers/__init__.py:8` |
| FFT data (server) | `sdrSource.addSpectrumClient(obj)` | `owrx/source/__init__.py:541` |
| Own FFT chain | `FftChain` from `sdrSource.getBuffer()` | `owrx/fft.py:40` |
| Retune SDR | `sdrSource.setCenterFreq(freq)` | `owrx/source/__init__.py:283` |
| Switch profiles | `sdrSource.activateProfile(id)` | `owrx/source/__init__.py:274` |
| React to SDR events | Implement `SdrSourceEventClient` | `owrx/source/__init__.py:57` |
| Background service | Follow `ServiceHandler` pattern | `owrx/service/__init__.py:20` |
| Access bookmarks | `Bookmarks.getSharedInstance().getBookmarks(range)` | `owrx/bookmarks.py:143` |
| WebSocket to browser | `OpenWebRxReceiverClient.send()` | `owrx/connection.py:58` |
| Property system | `PropertyStack`, `PropertyLayer`, `.wire()` | `owrx/property/__init__.py:273` |

## 6. Relevant Patterns from Fork PRs

### Signal classifier (PR #5) — ThreadModule pattern
- `owrx/signal_classifier.py` — `ThreadModule` tapping `selectorBuffer`
- Accumulates IQ samples, processes periodically
- JSON output with predictions and confidence scores
- **Directly reusable** for scanner FFT channelizer stage

### LoRa demodulator (PR #3) — 6-file pattern for new decoders
1. Mode definition: `owrx/modes.py`
2. Feature detection: `owrx/feature.py`
3. ExecModule wrapper: `csdr/module/toolbox.py`
4. Demodulator chain: `csdr/chain/toolbox.py`
5. Output parser: `owrx/toolbox.py`
6. DSP factory: `owrx/dsp.py`

### IQ FileSource (PR #6) — hardware-free testing
- `owrx/source/file.py` — `DirectSource` subclass
- `pv` for rate-limited playback of `.cf32` files
- `test/tools/generate_test_iq.py` — synthetic signal generator
- Test signals available: tone, FM, AM, CW

### Upstream branches of interest
- `skip_scan` — adds "scannable" field to bookmarks
- `smart_squelch` — SNR-based squelch for recording
- `direct-source` — direct SDR source access

## 7. Docker Development Environment

### Base image
`slechev/openwebrxplus-softmbe:latest` — pulled locally, 1.13GB, all drivers included

### Dev pattern (`Dockerfile.dev`)
```dockerfile
FROM slechev/openwebrxplus-softmbe:latest
# overlay local code
COPY . /opt/openwebrx
RUN cd /opt/openwebrx && python3 setup.py install --force
```

### SDRplay in Docker
- S6 init system runs `sdrplay_apiService` as supervised service
- `--device /dev/bus/usb` for device passthrough (no `--privileged` needed)
- SDRplay RSP1a detected on dev machine: `1df7:3000`

### Hardware-free testing
- IQ FileSource (PR #6 branch available locally)
- Synthetic signal generator for test fixtures
- Full e2e test pipeline in Docker

## 8. WebSocket Audio Protocol

### Binary message types (first byte = type tag)

| Byte | Type | Method | Description |
|------|------|--------|-------------|
| `0x01` | FFT/spectrum | `write_spectrum_data` (connection.py:473) | Waterfall + spectrum |
| `0x02` | Audio | `write_dsp_data` (connection.py:476) | Standard demod audio |
| `0x03` | Secondary FFT | `write_secondary_fft` (connection.py:499) | Digimodes waterfall |
| `0x04` | HD audio | `write_hd_audio` (connection.py:479) | WFM, DAB, etc. |

### Audio format
- **Compression:** ADPCM (IMA ADPCM) default, with `"SYNC"` word every 1000 samples
- **Sample rate:** Negotiated by client, typically 12000 Hz standard, 48000 Hz HD
- **Wire format:** Int16 PCM → optional ADPCM encoding

### Client-to-server messages
- `dspcontrol` with `action: "start"` and/or `params` (offset_freq, mod, squelch_level, etc.)
- `connectionproperties` with `output_rate` and `hd_output_rate`
- `setsdr`, `selectprofile`, `setfrequency`

## 9. Audio Playback (Client-Side)

### Web Audio API initialization (AudioEngine.js:39-69)
- Creates `AudioContext` with `{latencyHint: 'playback'}`
- Prefers `AudioWorkletNode` (`openwebrx-audio-processor`); falls back to `ScriptProcessorNode`
- AudioWorklet uses ring buffer (AudioProcessor.js:1-61), 128-sample frames
- `Interpolator` class upsamples from server rate to AudioContext rate via lowpass FIR

### Mobile handling
- `audioContext.onstatechange` waits for `state === 'running'`
- `resume()` must be called from user gesture (tap) to unlock on iOS/Android
- **No other mobile workarounds** — this is a known gap. No handling for background tabs, screen lock, or autoplay policies beyond the initial resume.

## 10. DSP Chain: SDR IQ → Browser Audio

```
SdrSource IQ buffer (COMPLEX_FLOAT)
  → Selector (shift + decimate + bandpass + squelch)
    → Demodulator (AM/NFM/SSB/WFM/digital)
      → ClientAudioChain (resample + noise filter + SHORT + ADPCM)
        → Buffer → DspManager.wireOutput() pump thread
          → connection.write_dsp_data() [prepend 0x02]
            → WebSocket binary frame
              → Browser: ADPCM decode → Interpolator resample → AudioWorklet
```

### Key classes
- **`ClientDemodulatorChain`** (dsp.py:39) — per-client: Selector + Demodulator + ClientAudioChain
- **`Selector`** (selector.py) — shift, FirDecimate, Bandpass, Squelch on complex IQ
- **`DspManager`** (dsp.py:484) — orchestrates chain per client, wires properties

### Per-client vs per-source
- **Per source:** Single IQ buffer (`getBuffer()`), shared spectrum/FFT thread
- **Per client:** Own `DspManager` → own `ClientDemodulatorChain`, all reading from same IQ buffer via independent `Buffer.getReader()` instances

## 11. Recording Capabilities (Already Exist)

### Client-side MP3 recording (AudioEngine.js:337-408)
- `lamejs` MP3 encoder in browser
- Taps into `processAudio()` at line 298: post-ADPCM-decode, pre-resample Int16 samples
- Filename: `REC-240323-120000-145500.mp3` (date + freq in kHz)
- Controlled by `allow_audio_recording` config flag

### Server-side AudioRecorder (csdr/chain/toolbox.py:260)
- `AudioRecorder(ServiceDemodulator, DialFrequencyReceiver)`
- Uses `Mp3Recorder` with **SNR-based squelch** (`SnrSquelch`)
- Configured via: `rec_squelch`, `rec_hang_time`, `rec_produce_silence`
- Runs as background service, not per-client
- **This is almost exactly what scanner v2 needs for recording**

### Recording tap points for scanner v2
- **Server-side (preferred):** In `DspManager.wireOutput()` (dsp.py:891), fork the buffer reader to write to file simultaneously
- **Alternative:** Create a parallel `AudioRecorder` service chain for the scanner's active frequency

## 12. Connection Lifecycle

### Startup sequence
1. Client opens WebSocket to `/ws/`
2. Handshake: `"SERVER DE CLIENT client=openwebrx.js type=receiver"`
3. Server creates `OpenWebRxReceiverClient`
4. Client sends `connectionproperties` (output_rate, hd_output_rate)
5. Server sends config, profiles, features, modes, bookmarks, bands
6. Client sends `dspcontrol` with `action: "start"`
7. `DspManager.start()` connects chain to SDR source buffer
8. Audio + FFT data start flowing

### Frequency change (no chain teardown)
Client sends `{"type": "dspcontrol", "params": {"offset_freq": N}}` → Selector shift updates, chain keeps running.

### Modulation change (chain rebuild)
Client sends `{"type": "dspcontrol", "params": {"mod": "am"}}` → `DspManager.setDemodulator()` stops old, creates new, re-wires output.

### Single-user potential
No explicit single-user mode exists. `SdrBusyState.BUSY`/`IDLE` (source/__init__.py:519/531) tracks whether USER clients are connected — could serve as basis for scanner's exclusive-access mode.
