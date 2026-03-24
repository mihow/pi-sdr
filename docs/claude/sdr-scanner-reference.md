# SDR Scanner Reference — External Projects & DSP Patterns

> Compiled 2026-03-23 from source code analysis of major open-source SDR projects.
> Line numbers are approximate — check the linked commit/file for exact locations.

## Architecture Patterns

There are three fundamentally different approaches to multi-channel SDR scanning:

| Pattern | Example Projects | How It Works | Pros | Cons |
|---------|-----------------|--------------|------|------|
| **Sequential retune** | SDR++, OpenWebRX+ | Tune to one freq, check squelch, move on | Simple | Slow, misses simultaneous signals |
| **FFT channelizer** | RTLSDR-Airband | Single FFT extracts all channels at once | Fast, efficient | Fixed channel spacing = FFT bin width |
| **Parallel mixer+decimate** | trunk-recorder | Per-channel complex mixer + FIR chain | Flexible, precise | CPU-heavy, complex code |

**Our scanner should use the FFT channelizer approach** for detection + the parallel mixer approach only for audio demod of the active channel.

---

## 1. RTLSDR-Airband — FFT Channelizer

**Repo**: https://github.com/charlie-foxtrot/RTLSDR-Airband
**Language**: C++
**Key insight**: All narrowband channels extracted from a single FFT. No per-channel mixing for AM.

### Core DSP Pipeline

**File**: [`src/rtl_airband.cpp`](https://github.com/charlie-foxtrot/RTLSDR-Airband/blob/master/src/rtl_airband.cpp)

- Default sample rate: **2,560,000 Hz** (`DEFAULT_SAMPLE_RATE`, ~line 50)
- FFT size: 256–8192 (default 512, `fft_size_log`, ~line 60)
- Window: **Blackman-Harris 7-term** (best sidelobe rejection)
- Each channel maps to FFT bin(s) by frequency offset from center

**AM demod** (simplest — magnitude of FFT bin):
```
output = sqrt(re² + im²)  // envelope detection, one sample per FFT frame
```

**NFM demod** from FFT bins (~line 800+):
- Uses `polar_disc_fast()` or `fm_quadri_demod()` on raw IQ from FFT output
- `polar_disc_fast`: fast atan2-based FM discriminator
- Per-channel lowpass: 2nd-order Bessel IIR

**Output rates**: 8000 Hz (AM), 16000 Hz (NFM)

### Squelch — 5-State Machine

**File**: [`src/squelch.h`](https://github.com/charlie-foxtrot/RTLSDR-Airband/blob/master/src/squelch.h)

States: `CLOSED → OPENING → OPEN → CLOSING → LOW_SIGNAL_ABORT`

Key features:
- **Noise floor tracking**: Low-pass filtered, updated only when squelch is CLOSED
- **SNR-based threshold**: Signal must exceed noise floor by configurable margin
- **Flap detection**: Auto-adjusts threshold when squelch oscillates
- **CTCSS gating**: During OPENING state, checks for tone before fully opening
- **Post-filter comparison**: Compares pre/post-filter signal for better discrimination

### Scan Mode

**File**: [`src/rtl_airband.cpp`](https://github.com/charlie-foxtrot/RTLSDR-Airband/blob/master/src/rtl_airband.cpp) (`controller_thread`, ~line 900+)

- Checks squelch state every **200ms**
- After 10 consecutive NO_SIGNAL (2 seconds), retunes to next frequency
- Updates Icecast metadata with current frequency

---

## 2. trunk-recorder — Parallel Multi-Channel

**Repo**: https://github.com/robotastic/trunk-recorder
**Language**: C++ (GNU Radio)
**Key insight**: True simultaneous monitoring of 10+ channels from one wideband capture.

### Signal Detection (Conventional Mode)

**File**: [`trunk-recorder/gr_blocks/signal_detector_cvf_impl.cc`](https://github.com/robotastic/trunk-recorder/blob/master/trunk-recorder/gr_blocks/signal_detector_cvf_impl.cc)

- Computes **periodogram** (windowed FFT → PSD) on full wideband input
- **Single-pole IIR averaging** per FFT bin for smoothing
- **Auto-threshold**: Sorts bins, finds largest power jump above median (controlled by `sensitivity`)
- **Edge detection**: Groups contiguous above-threshold bins into signals with bandwidth + center freq
- **Bandwidth quantization**: Default 12.5 kHz grid filters noise blips
- Runs every **100ms**

### Two-Stage Channelizer

**File**: [`trunk-recorder/gr_blocks/channelizer.cc`](https://github.com/robotastic/trunk-recorder/blob/master/trunk-recorder/gr_blocks/channelizer.cc)

Stage 1 (~line 50-80):
- Complex sinusoid LO + multiply (frequency translation)
- FFT bandpass filter, decimates to IF rate (~96 kHz)

Stage 2 (~line 80-110):
- Lowpass FFT filter
- Decimates to target rate (24000 or 25000 Hz for P25)

Fallback: `freq_xlating_fft_filter` combines translation + decimation in one FFT operation.

**File**: [`trunk-recorder/gr_blocks/xlat_channelizer.cc`](https://github.com/robotastic/trunk-recorder/blob/master/trunk-recorder/gr_blocks/xlat_channelizer.cc)

### Sample Rates

- Configurable per source: 2.4, 4.8, or 8 MHz common
- Usable bandwidth documented as **~80% of sample rate**
- Auto-calculates decimation factors to reach 24000 or 25000 Hz channel rate

---

## 3. SDR++ Scanner Plugin

**Repo**: https://github.com/AlexandreRouma/SDRPlusPlus
**Language**: C++
**Key insight**: Simplest possible scanner — reads pre-computed waterfall FFT, no custom DSP.

**File**: [`misc_modules/scanner/src/main.cpp`](https://github.com/AlexandreRouma/SDRPlusPlus/blob/master/misc_modules/scanner/src/main.cpp)

- **10 Hz scan loop** (100ms sleep) in worker thread
- Reads waterfall FFT via `gui::waterfall.acquireLatestFFT()`
- Steps through frequencies at configurable `interval` (default 100 kHz)
- `getMaxLevel()`: maps freq to FFT bins, finds peak power in passband
- Threshold: user-set `level` (default -50 dBFS)
- **Linger time**: stays on signal until it drops for `lingerTime` ms (default 1000)
- **Settling time**: waits `tuningTime` ms (default 250) after each retune
- `passbandRatio` (default 10%): fraction of VFO bandwidth to check

**This is NOT a channelizer** — it's a single-VFO retune scanner that piggybacks on the GUI's FFT.

---

## 4. OpenWebRX+ DSP Chain

**Repo**: https://github.com/luarvique/openwebrx (OpenWebRX+) / https://github.com/jketterl/openwebrx (original)
**Language**: Python + C (csdr library)

### Selector Chain (Per-Client VFO)

**File**: [`csdr/chain/selector.py`](https://github.com/jketterl/pycsdr/blob/master/csdr/chain/selector.py) (~lines 89-214)

1. `Shift` module: `shift.setRate(-frequencyOffset / inputRate)` — complex multiply
2. `FirDecimate`: Integer FIR decimation with computed cutoff + transition
3. `FractionalDecimator`: For non-integer ratios
4. `Bandpass`: FFT-based bandpass with configurable low/high
5. `Squelch`: Block-based power squelch with hang/flush time

### Scanner Mode

- Sequential retune through bookmarked frequencies
- Uses squelch open/close as trigger
- One VFO per client — no simultaneous multi-channel

### freq_scanner Plugin (Community)

**Repo**: https://github.com/0xAF/openwebrxplus-plugins
- Pure client-side JavaScript
- Reads S-meter + waterfall data from web UI
- Steps through frequencies via `UI.setFrequency()`
- Detection: `10 * Math.log10(window.smeter_level)` vs threshold
- Fine-tuning: scans waterfall bins for peak within ±3 kHz

---

## 5. Python NFM Scanner (epxx.co)

**Article**: https://epxx.co/artigos/python_nfm_en.html
**Language**: Python + NumPy
**Key insight**: Minimal Python-native NFM scanner, good reference for our pipeline.

### DSP Pipeline

1. **Wideband capture**: RTL-SDR, 200 kHz – 2.8 MHz. Center = average of all target channels.
2. **Channel extraction** (per channel):
   - Precomputed carrier: `exp(-1j * tau * IF_freq / INPUT_RATE * n)`
   - Complex multiply to shift to baseband
   - FIR lowpass at `IF_BANDWIDTH / 2` via `numpy.convolve`
   - Decimate by `INPUT_RATE / IF_RATE` (keep every Nth sample)
3. **FM demod**:
   ```python
   angles = numpy.angle(ifsamples)
   rotations = numpy.ediff1d(angles)
   rotations = (rotations + numpy.pi) % (2 * numpy.pi) - numpy.pi
   ```
4. **Squelch** (two methods):
   - **dBFS**: `20 * log10(mean(abs(ifsamples)))` — requires fixed gain
   - **Autocorrelation** (preferred): `correlate(output, output, 'same')` — voice shows periodicity, noise doesn't
5. **Second decimation**: 25 kHz → 12.5 kHz audio rate
6. **DC removal** + clip to ±0.999

### Key parameters
- IF_RATE = 25,000 Hz
- Audio rate = 12,500 Hz
- FIR taps = ~64
- Channel bandwidth = ~20 kHz for search, ~12.5 kHz for demod

---

## 6. SoapySDRPlay3 — SoapySDR Module for SDRplay API v3

**Repo**: https://github.com/pothosware/SoapySDRPlay3
**Language**: C++

### Device Init & API Connection

**File**: [`Settings.cpp`](https://github.com/pothosware/SoapySDRPlay3/blob/master/Settings.cpp)

- `SoapySDRPlay::SoapySDRPlay()` constructor (~line 30): Calls `sdrplay_api_Open()` to connect to daemon
- `sdrplay_api_Open()` is the client library call that connects to `sdrplay_apiService` via **shared memory**
- `sdrplay_api_LockDeviceApi()` / `sdrplay_api_UnlockDeviceApi()`: mutex for multi-process safety
- Gain modes: AGC (`sdrplay_api_AGC_CTRL_EN`) or manual per-element (IFGR, RFGR, LNA)

### RSP1A Specifics

**File**: [`Settings.cpp`](https://github.com/pothosware/SoapySDRPlay3/blob/master/Settings.cpp) (~line 200+)

- Antenna: "Antenna A" only (RSP1A has single input)
- Bandwidth options: 200, 300, 600, 1536, 5000, 6000, 7000, 8000 kHz
- Sample rates: Up to 10 MHz, but ADC resolution drops:
  - **2–6 MSPS**: 14-bit (best)
  - **6–8 MSPS**: 12-bit
  - **8–9.2 MSPS**: 10-bit
- DAB notch filter: `rsp1aTunerParams.dabNotchEnable`
- FM notch filter: `rsp1aTunerParams.rfNotchEnable`

### Docker Compatibility

- `sdrplay_apiService` must be reachable (same `/dev/shm` namespace)
- **Best approach**: Run daemon inside container, start before SoapySDR init
- USB re-enumeration on first init creates new device nodes — need `privileged: true` not just `--device`
- Reference project: [dgadams/sdrpp-server](https://github.com/dgadams/sdrpp-server) — starts `sdrplay_apiService &` in entrypoint

---

## 7. SDRplay API v3 — IPC Details

**Spec**: https://www.sdrplay.com/docs/SDRplay_API_Specification_v3.15.pdf
**Install paths** (Linux x86_64):
- Daemon: `/usr/local/bin/sdrplay_apiService` or `/opt/sdrplay_api/sdrplay_apiService`
- Library: `/usr/local/lib/libsdrplay_api.so.3.15`
- Headers: `/usr/local/include/sdrplay_api*.h`
- Systemd unit: `/etc/systemd/system/sdrplay.service`

**IPC mechanism**: POSIX shared memory (`shm_open` / `/dev/shm`) for IQ data transfer + likely local socket for control. Evidence:
- [herrameise/sdrplay-api-linux-docker](https://github.com/herrameise/sdrplay-api-linux-docker) documents `/dev/shm` mount requirement
- [sdrtrunk#1644](https://github.com/DSheirer/sdrtrunk/issues/1644) logs `shm_open: No such file or directory`

---

## 8. Key DSP Lessons

### Multi-stage decimation
- **scipy.signal.decimate**: Max recommended factor per stage is **13** (Chebyshev anti-alias filter)
- For 6 MHz → 48 kHz (factor 125): decompose into 5 × 5 × 5 = 125
- For 2.4 MHz → 48 kHz (factor 50): decompose into 10 × 5 = 50

### FIR filter design at high ratios
- 101-tap FIR with cutoff `12500 / 6000000 = 0.002` is useless — transition band wider than passband
- Must coarse-decimate first to get cutoff ratio above ~0.05
- Alternative: use IIR (Chebyshev/Butterworth) for coarse stage, FIR only for final precision

### FM demod
- Phase differentiation: `np.diff(np.unwrap(np.angle(iq)))`
- Scale by `sample_rate / (2π × max_deviation)` to normalize
- De-emphasis: single-pole IIR, τ = 75µs (NFM voice)
- **WFM broadcast**: τ = 75µs (Americas) or 50µs (Europe), deviation = 75 kHz, needs 192 kHz+ intermediate rate

### AM demod
- Envelope: `np.abs(iq)` then remove DC with `x - mean(x)`
- Works correctly at any intermediate rate — no phase issues
- This is why Air Band worked and FM didn't in our testing

### Usable bandwidth
- ~80% of sample rate is usable (edges roll off)
- trunk-recorder: "performance on the 10% on either side being significantly down"
- Account for this in band window planning

---

## 9. Portland, OR FM Broadcast Frequencies

Reference for verifying scanner accuracy. NOAA frequencies are nationally standardized.

### NOAA Weather Radio (ground truth — exact frequencies)
| Freq (MHz) | Channel |
|-----------|---------|
| 162.400 | WX1 |
| 162.425 | WX2 |
| 162.450 | WX3 |
| 162.475 | WX4 (Portland primary: KIG77) |
| 162.500 | WX5 |
| 162.525 | WX6 |
| 162.550 | WX7 |

### Portland FM Broadcast (major stations)
| Freq (MHz) | Call | Format |
|-----------|------|--------|
| 88.3 | KBVM | Religious |
| 89.1 | KMHD | Jazz |
| 89.9 | KQAC | Classical |
| 90.7 | KBOO | Community |
| 91.5 | KOPB | OPB/NPR |
| 92.3 | KGON | Classic Rock |
| 94.7 | KNRK | Alternative |
| 95.5 | KBFF | Top 40 |
| 97.1 | KYCH | Variety |
| 98.7 | KUPL | Country |
| 99.5 | KWJJ | Country |
| 100.3 | KKRZ | Top 40 |
| 101.1 | KXL | News |
| 101.9 | KINK | Indie |
| 103.3 | KKCW | AC (K103) |
| 105.1 | KRSK | Sports |
| 105.9 | KFBW | Classic Rock |
| 107.5 | KXJM | Hip-Hop |

---

## 10. Docker + SDRplay Projects

| Project | Approach | Link |
|---------|----------|------|
| dgadams/sdrpp-server | API daemon inside container | https://github.com/dgadams/sdrpp-server |
| herrameise/sdrplay-api-linux-docker | Non-interactive API installer for Docker | https://github.com/herrameise/sdrplay-api-linux-docker |
| gruenwelt/sdrconnect-server-docker | SDRConnect in Docker | https://github.com/gruenwelt/sdrconnect-server-docker |
| f4fhh/sdrplay_openwebrx | OpenWebRX + SDRplay Docker | https://github.com/f4fhh/sdrplay_openwebrx |

All use the pattern: `sdrplay_apiService &` in entrypoint, `privileged: true`, USB device mount.
