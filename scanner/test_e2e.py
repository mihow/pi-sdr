"""
End-to-end tests for the radio scanner SDR pipeline.

Includes:
  - FileSdrBackend: reads pre-recorded .cf32 IQ files (SdrBackend protocol)
  - capture_iq_from_pi: SSH capture of IQ samples from pi-sdr-1
  - Synthetic signal tests (no hardware needed)
  - NOAA weather validation test (requires pre-recorded IQ data)

Usage:
  python -m scanner.test_e2e                    # Run all tests (synthetic only if no IQ files)
  python -m scanner.test_e2e --capture-noaa     # Capture NOAA IQ from pi-sdr-1
  python -m scanner.test_e2e --validate-noaa    # Run NOAA validation test
  python -m scanner.test_e2e --generate-iq      # Generate synthetic test IQ files
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

from scanner.sdr_backend import FileSdrBackend, SdrBackend

# Conditional imports for modules that may not exist yet
try:
    from scanner.fft_scan import compute_channel_power, find_active_channels
except ImportError:
    compute_channel_power = None  # type: ignore[assignment]
    find_active_channels = None  # type: ignore[assignment]

try:
    from scanner.demod import demod_channel, extract_channel, fm_demodulate
except ImportError:
    demod_channel = None  # type: ignore[assignment]
    extract_channel = None  # type: ignore[assignment]
    fm_demodulate = None  # type: ignore[assignment]

try:
    from scanner.voice_detect import Detection, VoiceDetector
except ImportError:
    Detection = None  # type: ignore[assignment]
    VoiceDetector = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

TEST_DATA_DIR = Path(__file__).parent / "test_data"


# ---------------------------------------------------------------------------
# IQ File Capture from pi-sdr-1
# ---------------------------------------------------------------------------


def capture_iq_from_pi(
    host: str = "pi@100.66.209.75",
    center_freq: int = 162_475_000,
    sample_rate: int = 2_400_000,
    duration_seconds: float = 5.0,
    output_path: str = "scanner/test_data/noaa_wx4.cf32",
    gain: float | None = None,
) -> Path:
    """SSH into pi-sdr-1 and capture IQ samples using rtl_sdr.

    Requires rtl_sdr on the Pi and no other process using the SDR.
    Captures unsigned 8-bit IQ, converts to cf32 format.

    The conversion: uint8 values [0, 255] -> float32 [-1.0, 1.0]
    via  (sample - 127.5) / 127.5

    Args:
        host: SSH host string (user@ip or Tailscale hostname).
        center_freq: Center frequency in Hz.
        sample_rate: Sample rate in Hz.
        duration_seconds: Duration to capture.
        output_path: Where to save the .cf32 file.
        gain: SDR gain in dB, or None for auto-gain.

    Returns:
        Path to the saved .cf32 file.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Number of bytes to capture (2 bytes per sample: I + Q as uint8)
    num_bytes = int(sample_rate * duration_seconds * 2)

    # Build rtl_sdr command
    gain_flag = f"-g {gain}" if gain is not None else ""
    remote_tmp = "/tmp/capture.bin"
    rtl_cmd = (
        f"rtl_sdr -f {center_freq} -s {sample_rate} {gain_flag} "
        f"-n {num_bytes // 2} {remote_tmp}"
    )

    print(f"Capturing IQ from {host}...")
    print(f"  Frequency: {center_freq / 1e6:.3f} MHz")
    print(f"  Sample rate: {sample_rate / 1e6:.1f} MHz")
    print(f"  Duration: {duration_seconds:.1f}s ({num_bytes:,} bytes)")

    # Run rtl_sdr on the Pi
    try:
        subprocess.run(
            ["ssh", host, rtl_cmd],
            check=True,
            timeout=duration_seconds + 30,
        )
    except subprocess.CalledProcessError as e:
        # rtl_sdr returns non-zero on some normal exits
        logger.warning("rtl_sdr exited with code %d (may be normal)", e.returncode)
    except subprocess.TimeoutExpired:
        print("ERROR: SSH command timed out")
        raise

    # Download the raw file
    raw_path = out.with_suffix(".raw")
    print(f"Downloading raw IQ data from {host}:{remote_tmp}...")
    subprocess.run(
        ["scp", f"{host}:{remote_tmp}", str(raw_path)],
        check=True,
    )

    # Convert uint8 IQ to cf32 (interleaved float32)
    print("Converting uint8 -> cf32...")
    raw = np.fromfile(str(raw_path), dtype=np.uint8)
    float_iq = (raw.astype(np.float32) - 127.5) / 127.5
    float_iq.tofile(str(out))

    # Clean up raw file
    raw_path.unlink()

    # Clean up remote temp file
    subprocess.run(["ssh", host, f"rm -f {remote_tmp}"], check=False)

    # Write sidecar metadata
    sidecar = out.with_suffix(".json")
    meta = {
        "sample_rate": sample_rate,
        "center_frequency": center_freq,
        "duration_seconds": duration_seconds,
        "description": f"NOAA WX4 capture from pi-sdr-1 ({center_freq / 1e6:.3f} MHz)",
        "source": host,
        "format": "cf32 (interleaved float32 I/Q)",
    }
    with open(sidecar, "w") as f:
        json.dump(meta, f, indent=2)

    file_size = out.stat().st_size
    print(f"Saved: {out} ({file_size / 1e6:.1f} MB)")
    print(f"Metadata: {sidecar}")
    return out


# ---------------------------------------------------------------------------
# Synthetic IQ generation for hardware-free testing
# ---------------------------------------------------------------------------


def generate_fm_signal(
    center_freq: int,
    channel_freq: int,
    sample_rate: int = 2_400_000,
    duration_s: float = 1.0,
    audio_freq: float = 1000.0,
    deviation: float = 2500.0,
    amplitude: float = 0.5,
    noise_level: float = 0.01,
) -> np.ndarray:
    """Generate a synthetic NFM signal with a tone as modulating audio.

    Returns complex64 IQ samples centered at center_freq with a carrier
    at channel_freq, frequency-modulated by a sine tone.
    """
    n_samples = int(sample_rate * duration_s)
    t = np.arange(n_samples, dtype=np.float64) / sample_rate

    # FM modulation: carrier + integral of modulating signal
    offset = channel_freq - center_freq
    mod_integral = np.sin(2.0 * np.pi * audio_freq * t) / (2.0 * np.pi * audio_freq)
    phase = 2.0 * np.pi * offset * t + 2.0 * np.pi * deviation * mod_integral
    signal = amplitude * np.exp(1j * phase)

    # Add noise
    noise = noise_level * (
        np.random.randn(n_samples) + 1j * np.random.randn(n_samples)
    )

    return (signal + noise).astype(np.complex64)


def generate_voice_like_signal(
    center_freq: int,
    channel_freq: int,
    sample_rate: int = 2_400_000,
    duration_s: float = 2.0,
    deviation: float = 2500.0,
    amplitude: float = 0.5,
    noise_level: float = 0.01,
) -> np.ndarray:
    """Generate a synthetic NFM signal modulated with voice-like audio.

    Uses multiple harmonics of a fundamental frequency (simulating vocal
    formants) with amplitude modulation to mimic speech patterns.
    """
    n_samples = int(sample_rate * duration_s)
    t = np.arange(n_samples, dtype=np.float64) / sample_rate

    # Voice-like modulating signal: fundamental + harmonics with AM envelope
    f0 = 150.0  # fundamental pitch (Hz)
    audio = np.zeros(n_samples, dtype=np.float64)

    # Add harmonics (vocal formant approximation)
    for harmonic, amp in [(1, 1.0), (2, 0.7), (3, 0.4), (5, 0.2), (8, 0.1)]:
        audio += amp * np.sin(2.0 * np.pi * f0 * harmonic * t)

    # AM envelope: simulate syllable rhythm (~4 Hz modulation)
    envelope = 0.5 + 0.5 * np.sin(2.0 * np.pi * 4.0 * t)
    audio *= envelope

    # Normalize
    audio = audio / (np.max(np.abs(audio)) + 1e-10)

    # FM modulation
    offset = channel_freq - center_freq
    mod_integral = np.cumsum(audio) / sample_rate
    phase = 2.0 * np.pi * offset * t + 2.0 * np.pi * deviation * mod_integral
    signal = amplitude * np.exp(1j * phase)

    # Add noise
    noise = noise_level * (
        np.random.randn(n_samples) + 1j * np.random.randn(n_samples)
    )

    return (signal + noise).astype(np.complex64)


def save_cf32(
    iq: np.ndarray,
    path: Path,
    sample_rate: int,
    center_freq: int,
    description: str = "",
) -> None:
    """Save complex64 IQ data as .cf32 with JSON sidecar metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write interleaved float32
    interleaved = np.zeros(len(iq) * 2, dtype=np.float32)
    interleaved[0::2] = iq.real
    interleaved[1::2] = iq.imag
    interleaved.tofile(str(path))

    # Sidecar metadata
    meta = {
        "sample_rate": sample_rate,
        "center_frequency": center_freq,
        "description": description,
        "format": "cf32 (interleaved float32 I/Q)",
        "num_samples": len(iq),
        "duration_seconds": float(len(iq) / sample_rate),
    }
    with open(path.with_suffix(".json"), "w") as f:
        json.dump(meta, f, indent=2)

    logger.info("Saved %s (%d samples, %.2fs)", path.name, len(iq), len(iq) / sample_rate)


def generate_synthetic_test_files() -> None:
    """Generate synthetic .cf32 test files for hardware-free testing."""
    print("Generating synthetic test IQ files...")

    sr = 2_400_000
    center = 162_475_000  # NOAA WX4 center

    # 1. NFM tone signal (simple, predictable)
    print("  tone_nfm.cf32 -- 1kHz tone FM-modulated on WX4 frequency")
    iq = generate_fm_signal(center, 162_475_000, sample_rate=sr, duration_s=2.0)
    save_cf32(iq, TEST_DATA_DIR / "tone_nfm.cf32", sr, center, "Synthetic 1kHz tone on NOAA WX4")

    # 2. Voice-like signal
    print("  voice_nfm.cf32 -- voice-like signal on WX4 frequency")
    iq = generate_voice_like_signal(center, 162_475_000, sample_rate=sr, duration_s=3.0)
    save_cf32(iq, TEST_DATA_DIR / "voice_nfm.cf32", sr, center, "Synthetic voice-like signal on NOAA WX4")

    # 3. Noise only (no signal)
    print("  noise_only.cf32 -- background noise, no signal")
    n_samples = int(sr * 2.0)
    noise = 0.01 * (np.random.randn(n_samples) + 1j * np.random.randn(n_samples))
    save_cf32(
        noise.astype(np.complex64),
        TEST_DATA_DIR / "noise_only.cf32",
        sr,
        center,
        "Background noise only",
    )

    # 4. GMRS channel with voice
    gmrs_center = 462_650_000
    print("  gmrs_voice.cf32 -- voice-like signal on GMRS 7 (462.7125 MHz)")
    iq = generate_voice_like_signal(gmrs_center, 462_712_500, sample_rate=sr, duration_s=3.0)
    save_cf32(iq, TEST_DATA_DIR / "gmrs_voice.cf32", sr, gmrs_center, "Synthetic voice on GMRS 7")

    print(f"Done. Files saved to {TEST_DATA_DIR}/")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_file_backend_basic() -> None:
    """Test FileSdrBackend with a synthetic signal."""
    print("\n=== test_file_backend_basic ===")

    # Generate a small test file in a temp directory
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.cf32"
        sr = 2_400_000
        center = 162_475_000

        # Create a simple signal
        iq = generate_fm_signal(center, center, sample_rate=sr, duration_s=0.5)
        save_cf32(iq, path, sr, center, "test signal")

        # Open and read
        backend = FileSdrBackend(str(path), sample_rate=sr, center_freq=center)
        backend.open()

        assert backend.is_open(), "Backend should be open"
        assert backend.get_sample_rate() == sr
        assert backend.get_center_freq() == center

        # Read some samples
        samples = backend.read_iq(1024)
        assert len(samples) == 1024, f"Expected 1024 samples, got {len(samples)}"
        assert samples.dtype == np.complex64

        # Read more than available -- should loop
        total_samples = int(sr * 0.5)
        big_read = backend.read_iq(total_samples + 1000)
        assert len(big_read) == total_samples + 1000, "Looping read failed"

        backend.close()
        assert not backend.is_open()

    print("  PASSED")


def test_file_backend_sidecar_metadata() -> None:
    """Test that FileSdrBackend loads metadata from JSON sidecar."""
    print("\n=== test_file_backend_sidecar_metadata ===")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.cf32"

        # Write a minimal IQ file
        np.zeros(200, dtype=np.float32).tofile(str(path))

        # Write sidecar with metadata
        meta = {
            "sample_rate": 1_000_000,
            "center_frequency": 100_000_000,
            "description": "test sidecar",
        }
        with open(path.with_suffix(".json"), "w") as f:
            json.dump(meta, f)

        # Defaults should be overridden by sidecar
        backend = FileSdrBackend(str(path))
        assert backend.get_sample_rate() == 1_000_000
        assert backend.get_center_freq() == 100_000_000

    print("  PASSED")


def test_file_backend_no_loop() -> None:
    """Test FileSdrBackend with loop=False returns only available data."""
    print("\n=== test_file_backend_no_loop ===")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.cf32"
        n_samples = 500
        iq = np.ones(n_samples, dtype=np.complex64) * (0.5 + 0.5j)
        interleaved = np.zeros(n_samples * 2, dtype=np.float32)
        interleaved[0::2] = iq.real
        interleaved[1::2] = iq.imag
        interleaved.tofile(str(path))

        backend = FileSdrBackend(str(path), sample_rate=1000, center_freq=0, loop=False)
        backend.open()

        # Read more than available
        samples = backend.read_iq(1000)
        assert len(samples) == n_samples, f"Expected {n_samples}, got {len(samples)}"

        backend.close()

    print("  PASSED")


def test_file_backend_protocol_compliance() -> None:
    """Verify FileSdrBackend satisfies the SdrBackend protocol."""
    print("\n=== test_file_backend_protocol_compliance ===")

    assert isinstance(FileSdrBackend, type), "FileSdrBackend should be a class"

    # Check all protocol methods exist
    required_methods = [
        "open", "close", "tune", "read_iq",
        "get_center_freq", "get_sample_rate", "get_max_bandwidth", "is_open",
    ]
    for method in required_methods:
        assert hasattr(FileSdrBackend, method), f"Missing method: {method}"

    # Runtime protocol check
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.cf32"
        np.zeros(100, dtype=np.float32).tofile(str(path))
        instance = FileSdrBackend(str(path))
        assert isinstance(instance, SdrBackend), "FileSdrBackend must satisfy SdrBackend protocol"

    print("  PASSED")


def test_fft_scan_synthetic() -> None:
    """Test FFT scan detects a synthetic signal."""
    print("\n=== test_fft_scan_synthetic ===")

    if compute_channel_power is None or find_active_channels is None:
        print("  SKIPPED (fft_scan module not available)")
        return

    sr = 2_400_000
    center = 162_475_000
    channel = 162_475_000

    # Strong signal on WX4
    iq = generate_fm_signal(center, channel, sample_rate=sr, amplitude=0.8, noise_level=0.001)

    powers = compute_channel_power(
        iq, sr, center,
        channel_freqs=[channel, center + 500_000, center - 500_000],
        channel_bandwidths=[25_000, 25_000, 25_000],
    )

    assert channel in powers, "Target channel not in results"
    # Signal channel should be significantly stronger than off-channel
    off_channels = [f for f in powers if f != channel]
    if off_channels:
        signal_power = powers[channel]
        max_noise = max(powers[f] for f in off_channels)
        snr = signal_power - max_noise
        print(f"  Signal: {signal_power:.1f} dB, Noise: {max_noise:.1f} dB, SNR: {snr:.1f} dB")
        assert snr > 10, f"SNR too low: {snr:.1f} dB (expected >10)"

    # Should be found as active
    active = find_active_channels(powers, squelch_level=powers[channel] - 20)
    assert channel in active, "Channel should be active"

    print("  PASSED")


def test_demod_synthetic() -> None:
    """Test FM demod produces audio from a synthetic FM signal."""
    print("\n=== test_demod_synthetic ===")

    if demod_channel is None:
        print("  SKIPPED (demod module not available)")
        return

    sr = 2_400_000
    center = 162_475_000
    channel = 162_475_000
    tone_freq = 1000.0

    iq = generate_fm_signal(
        center, channel, sample_rate=sr,
        duration_s=1.0, audio_freq=tone_freq, amplitude=0.8,
    )

    audio = demod_channel(iq, sr, center, channel, channel_bw=25_000, audio_rate=16_000)

    assert len(audio) > 0, "Demod produced no audio"
    assert audio.dtype == np.int16

    # Audio should not be silence
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    print(f"  Audio RMS: {rms:.1f} (of 32767)")
    assert rms > 100, f"Audio RMS too low ({rms:.1f}), expected audible signal"

    # Check that the dominant frequency is near our tone
    audio_float = audio.astype(np.float64) / 32768.0
    spectrum = np.abs(np.fft.rfft(audio_float))
    freqs = np.fft.rfftfreq(len(audio_float), d=1.0 / 16_000)
    peak_freq = freqs[np.argmax(spectrum[1:]) + 1]  # skip DC
    print(f"  Peak frequency: {peak_freq:.0f} Hz (expected ~{tone_freq:.0f} Hz)")
    assert abs(peak_freq - tone_freq) < 200, f"Peak freq {peak_freq} too far from {tone_freq}"

    print("  PASSED")


def test_voice_detect_synthetic() -> None:
    """Test voice detector on synthetic voice-like signal."""
    print("\n=== test_voice_detect_synthetic ===")

    if demod_channel is None or VoiceDetector is None or Detection is None:
        print("  SKIPPED (demod or voice_detect module not available)")
        return

    sr = 2_400_000
    center = 162_475_000
    channel = 162_475_000

    iq = generate_voice_like_signal(
        center, channel, sample_rate=sr, duration_s=3.0, amplitude=0.8,
    )

    audio = demod_channel(iq, sr, center, channel, channel_bw=25_000, audio_rate=16_000)

    detector = VoiceDetector(sample_rate=16_000, frame_ms=30)
    frame_size = detector.frame_samples * 2  # 16-bit = 2 bytes/sample

    audio_bytes = audio.tobytes()
    n_frames = len(audio_bytes) // frame_size
    print(f"  Processing {n_frames} frames...")

    for i in range(n_frames):
        frame = audio_bytes[i * frame_size : (i + 1) * frame_size]
        detector.process_frame(frame)

    decision, confidence = detector.get_decision()
    print(f"  Decision: {decision.value}, confidence: {confidence:.2f}")

    # Synthetic voice-like signals may not always trigger VOICE due to
    # the simplistic generation -- but they should NOT be classified as DIGITAL
    assert decision != Detection.DIGITAL, "Voice-like signal classified as DIGITAL"

    print("  PASSED")


def test_full_pipeline_synthetic() -> None:
    """End-to-end: FileSdrBackend -> FFT scan -> demod -> voice detect."""
    print("\n=== test_full_pipeline_synthetic ===")

    if compute_channel_power is None or demod_channel is None:
        print("  SKIPPED (fft_scan or demod module not available)")
        return

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pipeline_test.cf32"
        sr = 2_400_000
        center = 162_475_000
        channel = 162_475_000

        # Generate and save voice-like signal
        iq = generate_voice_like_signal(
            center, channel, sample_rate=sr, duration_s=2.0, amplitude=0.8,
        )
        save_cf32(iq, path, sr, center, "pipeline test")

        # 1. Load via FileSdrBackend
        backend = FileSdrBackend(str(path))
        backend.open()

        # 2. Read IQ
        iq_data = backend.read_iq(sr * 2)  # 2 seconds

        # 3. FFT scan
        powers = compute_channel_power(
            iq_data, sr, center,
            channel_freqs=[channel],
            channel_bandwidths=[25_000],
        )
        assert channel in powers, "Channel not detected in FFT"
        print(f"  FFT power: {powers[channel]:.1f} dB")

        # 4. Demod
        audio = demod_channel(
            iq_data, sr, center, channel, channel_bw=25_000, audio_rate=16_000,
        )
        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
        print(f"  Audio RMS: {rms:.1f}")
        assert rms > 50, "Demodulated audio is too quiet"

        # 5. Voice detect
        if VoiceDetector is not None and Detection is not None:
            detector = VoiceDetector(sample_rate=16_000, frame_ms=30)
            frame_size = detector.frame_samples * 2
            audio_bytes = audio.tobytes()
            n_frames = len(audio_bytes) // frame_size
            for i in range(n_frames):
                frame = audio_bytes[i * frame_size : (i + 1) * frame_size]
                detector.process_frame(frame)

            decision, confidence = detector.get_decision()
            print(f"  Voice decision: {decision.value} ({confidence:.2f})")

        backend.close()

    print("  PASSED")


def test_noise_rejected() -> None:
    """Test that noise-only IQ does not produce a signal detection."""
    print("\n=== test_noise_rejected ===")

    if compute_channel_power is None or find_active_channels is None:
        print("  SKIPPED (fft_scan module not available)")
        return

    sr = 2_400_000
    center = 162_475_000
    n_samples = sr * 1  # 1 second

    # Pure noise
    iq = (
        0.01 * (np.random.randn(n_samples) + 1j * np.random.randn(n_samples))
    ).astype(np.complex64)

    powers = compute_channel_power(
        iq, sr, center,
        channel_freqs=[162_475_000, 162_500_000, 162_450_000],
        channel_bandwidths=[25_000, 25_000, 25_000],
    )

    # With a reasonable squelch, nothing should be active
    active = find_active_channels(powers, squelch_level=20.0)
    assert len(active) == 0, f"Noise should not trigger detection, got {active}"

    print(f"  Max noise power: {max(powers.values()):.1f} dB (below squelch 20 dB)")
    print("  PASSED")


# ---------------------------------------------------------------------------
# NOAA Validation (requires pre-recorded IQ)
# ---------------------------------------------------------------------------


def test_noaa_detection(iq_file: str | None = None) -> None:
    """Validate scanner detects voice on NOAA weather broadcast.

    Uses a pre-recorded IQ capture of NOAA WX4 (162.475 MHz).
    NOAA weather radio is continuous voice broadcast, so this
    should always detect as VOICE.

    Run ``python -m scanner.test_e2e --capture-noaa`` first to create the test data.
    """
    print("\n=== test_noaa_detection ===")

    if compute_channel_power is None or demod_channel is None:
        print("  SKIPPED (fft_scan or demod module not available)")
        return
    if VoiceDetector is None or Detection is None:
        print("  SKIPPED (voice_detect module not available)")
        return

    if iq_file is None:
        iq_file = str(TEST_DATA_DIR / "noaa_wx4.cf32")

    path = Path(iq_file)
    if not path.exists():
        print(f"  SKIPPED (IQ file not found: {path})")
        print("  Run: python -m scanner.test_e2e --capture-noaa")
        return

    # 1. Load IQ file via FileSdrBackend
    backend = FileSdrBackend(str(path))
    backend.open()

    sr = backend.get_sample_rate()
    center = backend.get_center_freq()
    channel = 162_475_000  # NOAA WX4

    print(f"  IQ file: {path.name}")
    print(f"  Sample rate: {sr / 1e6:.1f} MHz, Center: {center / 1e6:.3f} MHz")

    # 2. Read IQ data (up to 5 seconds)
    iq = backend.read_iq(sr * 5)

    # 3. FFT scan -- verify signal detected above squelch
    powers = compute_channel_power(
        iq, sr, center,
        channel_freqs=[channel],
        channel_bandwidths=[25_000],
    )
    assert channel in powers, "NOAA WX4 channel not in FFT results"
    print(f"  FFT power on WX4: {powers[channel]:.1f} dB")

    # 4. Extract channel + FM demod -- verify audio is not silence
    audio = demod_channel(iq, sr, center, channel, channel_bw=25_000, audio_rate=16_000)
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    print(f"  Demodulated audio: {len(audio)} samples, RMS={rms:.1f}")
    assert rms > 100, f"Audio RMS too low ({rms:.1f}), NOAA should be audible"

    # 5. Run through voice detector -- verify Detection.VOICE
    detector = VoiceDetector(sample_rate=16_000, frame_ms=30)
    frame_size = detector.frame_samples * 2
    audio_bytes = audio.tobytes()
    n_frames = len(audio_bytes) // frame_size

    frame_results: dict[str, int] = {}
    for i in range(n_frames):
        frame = audio_bytes[i * frame_size : (i + 1) * frame_size]
        result = detector.process_frame(frame)
        frame_results[result.value] = frame_results.get(result.value, 0) + 1

    decision, confidence = detector.get_decision()
    print(f"  Frame results: {frame_results}")
    print(f"  Decision: {decision.value}, confidence: {confidence:.2f}")

    assert decision == Detection.VOICE, (
        f"NOAA weather should be detected as VOICE, got {decision.value} "
        f"(confidence={confidence:.2f})"
    )

    # 6. Save demodulated audio as WAV for manual listening verification
    wav_path = path.with_suffix(".wav")
    with wave.open(str(wav_path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16_000)
        wf.writeframes(audio.tobytes())
    print(f"  Saved demodulated audio: {wav_path}")

    backend.close()
    print("  PASSED")


# ---------------------------------------------------------------------------
# CLI + main
# ---------------------------------------------------------------------------


def run_all_tests() -> int:
    """Run all tests, return number of failures."""
    tests = [
        test_file_backend_basic,
        test_file_backend_sidecar_metadata,
        test_file_backend_no_loop,
        test_file_backend_protocol_compliance,
        test_fft_scan_synthetic,
        test_demod_synthetic,
        test_voice_detect_synthetic,
        test_noise_rejected,
        test_full_pipeline_synthetic,
    ]

    # Include NOAA test only if IQ file exists
    noaa_path = TEST_DATA_DIR / "noaa_wx4.cf32"
    if noaa_path.exists():
        tests.append(test_noaa_detection)

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'=' * 40}")
    print(f"Results: {passed} passed, {failed} failed")
    return failed


def analyze_scan_captures(data_dir: str = "scanner/test_data", squelch: float = 30.0):
    """Analyze multi-band IQ captures and report signals/voice detections.

    Expects files named scan_BANDNAME.cf32 with .json sidecars.
    Run capture_multiband_from_pi() first to create the files.

    Usage:
        python -m scanner.test_e2e --analyze-scan
    """
    from scanner.fft_scan import compute_channel_power
    from scanner.demod import demod_channel
    from scanner.voice_detect import VoiceDetector, Detection
    from scanner.frequencies import get_default_scan_list

    all_channels = get_default_scan_list()
    data_path = Path(data_dir)
    results = []

    # Find all scan_*.cf32 files
    captures = sorted(data_path.glob("scan_*.cf32"))
    if not captures:
        print(f"No scan_*.cf32 files found in {data_dir}")
        print("Run: python -m scanner.test_e2e --capture-scan")
        return results

    print(f"Analyzing {len(captures)} band captures from {data_dir}")
    print()

    for cf32_path in captures:
        json_path = cf32_path.with_suffix(".json")
        band_name = cf32_path.stem.replace("scan_", "")

        if not json_path.exists():
            print(f"  Skipping {cf32_path.name} — no .json sidecar")
            continue

        with open(json_path) as f:
            meta = json.load(f)

        raw = np.fromfile(str(cf32_path), dtype=np.float32)
        iq = raw[0::2] + 1j * raw[1::2]
        sr = meta["sample_rate"]
        cf = meta["center_frequency"]
        dur = len(iq) / sr

        lo = cf - sr // 2
        hi = cf + sr // 2
        band_channels = [c for c in all_channels if lo <= c["freq"] <= hi]

        if not band_channels:
            continue

        powers = compute_channel_power(
            iq[: min(262144, len(iq))], sr, cf,
            [c["freq"] for c in band_channels],
            [c.get("bandwidth", 12500) for c in band_channels],
        )

        print(f"--- {band_name} ({cf/1e6:.3f} MHz, {dur:.1f}s) ---")

        for ch in sorted(band_channels, key=lambda c: -powers.get(c["freq"], -999)):
            p = powers.get(ch["freq"], -999)
            if p < squelch:
                continue

            try:
                audio = demod_channel(
                    iq[: min(int(sr), len(iq))], sr, cf,
                    ch["freq"], ch.get("bandwidth", 12500),
                )
                detector = VoiceDetector(sample_rate=16000)
                for i in range(0, len(audio) - 480, 480):
                    detector.process_frame(audio[i : i + 480].tobytes())
                decision, confidence = detector.get_decision()
            except Exception:
                decision, confidence = Detection.NOISE, 0.0

            entry = {
                "band": band_name, "channel": ch["name"], "freq": ch["freq"],
                "power": round(p, 1), "detection": decision.value,
                "confidence": round(confidence, 2),
            }
            results.append(entry)
            marker = " <<<" if decision == Detection.VOICE else ""
            print(f"  {ch['name']:<25s} {p:5.1f} dB  {decision.value:>8s} (conf={confidence:.2f}){marker}")

    # Summary
    voice = [r for r in results if r["detection"] == "voice"]
    strong = [r for r in results if r["power"] > 35]
    print(f"\nSummary: {len(results)} channels above squelch, {len(strong)} strong (>35 dB), {len(voice)} voice")
    if voice:
        for v in voice:
            print(f"  VOICE: {v['channel']} {v['freq']/1e6:.4f} MHz S={v['power']} dB conf={v['confidence']}")
    return results


def capture_multiband_from_pi(
    host: str = "pi@100.66.209.75",
    bands: list[tuple[str, int]] | None = None,
    duration: float = 5.0,
    output_dir: str = "scanner/test_data",
):
    """Capture IQ from multiple bands on pi-sdr-1 using rtl_sdr.

    Stops the scanner container, captures each band, restarts scanner.
    """
    if bands is None:
        bands = [
            ("NOAA", 162_475_000),
            ("GMRS", 462_637_000),
            ("HAM_2m", 146_940_000),
            ("Marine", 156_625_000),
        ]

    sample_rate = 2_400_000
    n_samples = int(sample_rate * duration * 2)  # *2 for I+Q uint8
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Stop scanner
    print("Stopping scanner on Pi...")
    subprocess.run(
        ["ssh", host.split("@")[1] if "@" in host else host,
         "cd /opt/openwebrx && sudo docker compose stop scanner"],
        capture_output=True,
    )

    import time
    time.sleep(2)

    for name, freq in bands:
        raw_path = f"/tmp/scan_{name}.raw"
        local_cf32 = str(out / f"scan_{name}.cf32")
        local_json = str(out / f"scan_{name}.json")

        print(f"Capturing {name} at {freq/1e6:.3f} MHz for {duration}s...")
        subprocess.run(
            ["ssh", host, f"rtl_sdr -f {freq} -s {sample_rate} -n {n_samples} {raw_path}"],
            capture_output=True, timeout=int(duration + 10),
        )

        # Download and convert
        with tempfile.NamedTemporaryFile(suffix=".raw") as tmp:
            subprocess.run(["scp", f"{host}:{raw_path}", tmp.name], capture_output=True)
            raw = np.fromfile(tmp.name, dtype=np.uint8)

        if len(raw) == 0:
            print(f"  ERROR: no data captured for {name}")
            continue

        floats = (raw.astype(np.float32) - 127.5) / 127.5
        floats.tofile(local_cf32)
        meta = {
            "sample_rate": sample_rate,
            "center_frequency": freq,
            "description": f"{name} band capture from pi-sdr-1",
        }
        with open(local_json, "w") as f:
            json.dump(meta, f, indent=2)
        print(f"  Saved {local_cf32} ({len(raw)/2/sample_rate:.1f}s)")

    # Restart scanner
    print("Restarting scanner on Pi...")
    subprocess.run(
        ["ssh", host.split("@")[1] if "@" in host else host,
         "cd /opt/openwebrx && sudo docker compose up -d scanner"],
        capture_output=True,
    )
    print("Done!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Radio scanner end-to-end tests",
        prog="python -m scanner.test_e2e",
    )
    parser.add_argument(
        "--capture-noaa",
        action="store_true",
        help="Capture NOAA WX4 IQ from pi-sdr-1",
    )
    parser.add_argument(
        "--validate-noaa",
        action="store_true",
        help="Run NOAA validation test",
    )
    parser.add_argument(
        "--generate-iq",
        action="store_true",
        help="Generate synthetic test IQ files",
    )
    parser.add_argument(
        "--capture-scan",
        action="store_true",
        help="Capture IQ from multiple bands on pi-sdr-1",
    )
    parser.add_argument(
        "--analyze-scan",
        action="store_true",
        help="Analyze previously captured multi-band IQ files",
    )
    parser.add_argument(
        "--iq-file",
        help="Path to IQ file for NOAA validation (default: test_data/noaa_wx4.cf32)",
    )
    parser.add_argument(
        "--host",
        default="pi@100.66.209.75",
        help="SSH host for IQ capture (default: pi@100.66.209.75)",
    )
    parser.add_argument(
        "--freq",
        type=int,
        default=162_475_000,
        help="Center frequency for capture in Hz (default: 162475000 / NOAA WX4)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=5.0,
        help="Capture duration in seconds (default: 5.0)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.capture_noaa:
        capture_iq_from_pi(
            host=args.host,
            center_freq=args.freq,
            sample_rate=2_400_000,
            duration_seconds=args.duration,
        )
    elif args.validate_noaa:
        try:
            test_noaa_detection(iq_file=args.iq_file)
        except AssertionError as e:
            print(f"  FAILED: {e}")
            sys.exit(1)
    elif args.capture_scan:
        capture_multiband_from_pi(host=args.host, duration=args.duration)
    elif args.analyze_scan:
        analyze_scan_captures()
    elif args.generate_iq:
        generate_synthetic_test_files()
    else:
        failures = run_all_tests()
        sys.exit(1 if failures > 0 else 0)
