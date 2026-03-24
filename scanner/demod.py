"""
NFM channel extraction and demodulation from wideband IQ data.

DSP pipeline mirrors OpenWebRX+'s csdr chain:
  Selector: freq shift -> FIR lowpass -> decimate
  Demodulator: FM phase diff -> limiter -> NFM de-emphasis -> AGC -> int16

Designed for real-time use on Raspberry Pi 5.
"""

from __future__ import annotations

import logging
from math import gcd

import numpy as np
from scipy.signal import decimate, firwin, lfilter, resample_poly

logger = logging.getLogger(__name__)

# Module-level cache for FIR filter coefficients, keyed on
# (sample_rate, channel_bw, output_rate, numtaps).
_fir_cache: dict[tuple[int, int, int, int], np.ndarray] = {}

# De-emphasis IIR coefficient cache, keyed on sample_rate.
_deemph_cache: dict[int, tuple[float, float]] = {}


def _get_fir_coeffs(
    sample_rate: int,
    channel_bw: int,
    output_rate: int,
    numtaps: int = 101,
) -> np.ndarray:
    """Get cached FIR lowpass filter coefficients."""
    key = (sample_rate, channel_bw, output_rate, numtaps)
    if key not in _fir_cache:
        cutoff = channel_bw / sample_rate
        # Clamp to valid range for firwin (0 < cutoff < 1.0)
        cutoff = min(cutoff, 0.99)
        _fir_cache[key] = firwin(numtaps, cutoff).astype(np.float32)
        logger.debug(
            "FIR cache miss: sr=%d bw=%d out=%d taps=%d cutoff=%.6f",
            sample_rate, channel_bw, output_rate, numtaps,
        )
    return _fir_cache[key]


def _get_deemph_coeffs(sample_rate: int, tau: float = 75e-6) -> tuple[float, float]:
    """Get cached single-pole IIR de-emphasis filter coefficients.

    NFM voice uses ~75us time constant. Returns (alpha, 1-alpha) where
    y[n] = (1-alpha) * x[n] + alpha * y[n-1].
    """
    if sample_rate not in _deemph_cache:
        dt = 1.0 / sample_rate
        alpha = np.exp(-dt / tau)
        _deemph_cache[sample_rate] = (float(alpha), float(1.0 - alpha))
        logger.debug("De-emphasis: sr=%d tau=%.0fus alpha=%.6f", sample_rate, tau * 1e6, alpha)
    return _deemph_cache[sample_rate]


def extract_channel(
    iq: np.ndarray,
    sample_rate: int,
    center_freq: int,
    channel_freq: int,
    channel_bw: int = 12_500,
    output_rate: int = 48_000,
) -> np.ndarray:
    """Extract a single channel from wideband IQ data.

    Frequency-shifts the target channel to baseband, applies a FIR lowpass
    filter, and decimates to output_rate.

    Args:
        iq: complex64 wideband IQ samples.
        sample_rate: Input sample rate (e.g. 2_400_000).
        center_freq: Center frequency of the IQ capture.
        channel_freq: Target channel frequency to extract.
        channel_bw: Channel bandwidth in Hz (default 12500 for NFM).
        output_rate: Decimated output sample rate.

    Returns:
        complex64 IQ samples at output_rate.
    """
    offset = channel_freq - center_freq

    # 1. Frequency shift to baseband
    n_samples = len(iq)
    t = np.arange(n_samples, dtype=np.float32) / sample_rate
    shifted = iq * np.exp(-1j * 2.0 * np.pi * offset * t).astype(np.complex64)

    # 2. FIR lowpass filter
    fir = _get_fir_coeffs(sample_rate, channel_bw, output_rate)
    filtered = lfilter(fir, 1.0, shifted).astype(np.complex64)

    # 3. Decimate to output_rate
    dec_factor = sample_rate // output_rate
    if dec_factor < 1:
        dec_factor = 1

    if sample_rate % output_rate == 0 and dec_factor > 1:
        # Integer decimation: just take every Nth sample (already filtered)
        channel = filtered[::dec_factor]
    elif dec_factor > 1:
        # Non-integer: use rational resampling
        g = gcd(sample_rate, output_rate)
        up = output_rate // g
        down = sample_rate // g
        channel = resample_poly(filtered, up, down).astype(np.complex64)
    else:
        channel = filtered

    logger.debug(
        "extract_channel: %d Hz offset, %d -> %d Hz, %d -> %d samples",
        offset, sample_rate, output_rate, n_samples, len(channel),
    )
    return channel


def fm_demodulate(
    channel_iq: np.ndarray,
    sample_rate: int,
    audio_rate: int = 16_000,
    max_deviation: float = 2500.0,
) -> np.ndarray:
    """Demodulate NFM audio from channel IQ data.

    Mirrors OpenWebRX+ csdr chain: FmDemod -> Limit -> NfmDeemphasis -> AGC.

    Args:
        channel_iq: complex64 channel IQ at intermediate rate.
        sample_rate: Sample rate of channel_iq (e.g. 48000).
        audio_rate: Output audio sample rate (default 16000).
        max_deviation: NFM max frequency deviation in Hz.

    Returns:
        int16 PCM audio at audio_rate.
    """
    if len(channel_iq) < 2:
        return np.array([], dtype=np.int16)

    # 1. FM demodulation via phase differentiation
    phase = np.unwrap(np.angle(channel_iq))
    demod = np.diff(phase)

    # 2. Scale by deviation
    demod = demod * (sample_rate / (2.0 * np.pi * max_deviation))

    # 3. Limiter: clip to [-1.0, 1.0]
    audio = np.clip(demod, -1.0, 1.0).astype(np.float32)

    # 4. NFM de-emphasis (single-pole IIR, 75us time constant)
    alpha, one_minus_alpha = _get_deemph_coeffs(sample_rate)
    # Apply IIR: y[n] = (1-alpha)*x[n] + alpha*y[n-1]
    # Use lfilter with transfer function: H(z) = (1-alpha) / (1 - alpha*z^-1)
    audio = lfilter([one_minus_alpha], [1.0, -alpha], audio).astype(np.float32)

    # 5. Resample to audio_rate if needed
    if sample_rate != audio_rate:
        g = gcd(sample_rate, audio_rate)
        up = audio_rate // g
        down = sample_rate // g
        audio = resample_poly(audio, up, down).astype(np.float32)

    # 6. AGC: normalize by RMS with max_gain=3 (matches csdr NFm AGC)
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms > 0:
        gain = min(0.5 / rms, 3.0)
        audio = audio * gain

    # Re-clip after AGC
    audio = np.clip(audio, -1.0, 1.0)

    # 7. Convert to int16
    return (audio * 32767).astype(np.int16)


def am_demodulate(
    channel_iq: np.ndarray,
    sample_rate: int,
    audio_rate: int = 16_000,
) -> np.ndarray:
    """Demodulate AM audio from channel IQ data.

    Uses envelope detection (magnitude of IQ signal).

    Args:
        channel_iq: complex64 channel IQ at intermediate rate.
        sample_rate: Sample rate of channel_iq.
        audio_rate: Output audio sample rate.

    Returns:
        int16 PCM audio at audio_rate.
    """
    if len(channel_iq) < 2:
        return np.array([], dtype=np.int16)

    # Envelope detection: magnitude of complex signal
    envelope = np.abs(channel_iq).astype(np.float32)

    # Remove DC component (carrier)
    envelope = envelope - np.mean(envelope)

    # Resample to audio_rate
    if sample_rate != audio_rate:
        g = gcd(sample_rate, audio_rate)
        up = audio_rate // g
        down = sample_rate // g
        envelope = resample_poly(envelope, up, down).astype(np.float32)

    # AGC
    rms = float(np.sqrt(np.mean(envelope ** 2)))
    if rms > 0:
        gain = min(0.5 / rms, 5.0)
        envelope = envelope * gain

    envelope = np.clip(envelope, -1.0, 1.0)
    return (envelope * 32767).astype(np.int16)


def demod_channel(
    iq: np.ndarray,
    sample_rate: int,
    center_freq: int,
    channel_freq: int,
    channel_bw: int = 12_500,
    audio_rate: int = 16_000,
    mod: str = "nfm",
) -> np.ndarray:
    """Extract and demodulate a channel in one call.

    Adapts demod parameters based on modulation type and channel bandwidth.

    Args:
        iq: complex64 wideband IQ samples.
        sample_rate: Input sample rate (e.g. 2_400_000).
        center_freq: Center frequency of the IQ capture.
        channel_freq: Target channel frequency.
        channel_bw: Channel bandwidth in Hz.
        audio_rate: Output audio sample rate.
        mod: Modulation type — "nfm", "wfm", or "am".

    Returns:
        int16 PCM audio at audio_rate.
    """
    # Determine intermediate rate and demod parameters from mod type + bandwidth
    if mod == "am":
        # AM (Air Band): envelope detection
        intermediate_rate = 48_000
        channel_iq = extract_channel(
            iq, sample_rate, center_freq, channel_freq,
            channel_bw=channel_bw, output_rate=intermediate_rate,
        )
        return am_demodulate(channel_iq, intermediate_rate, audio_rate=audio_rate)

    elif mod == "wfm" or channel_bw >= 100_000:
        # Wideband FM (broadcast): 75 kHz deviation, higher intermediate rate
        intermediate_rate = 192_000
        max_deviation = 75_000.0
    elif channel_bw >= 20_000:
        # Wide NFM (NOAA weather, some repeaters): 5 kHz deviation
        intermediate_rate = 48_000
        max_deviation = 5_000.0
    else:
        # Standard NFM: 2.5 kHz deviation
        intermediate_rate = 48_000
        max_deviation = 2_500.0

    channel_iq = extract_channel(
        iq, sample_rate, center_freq, channel_freq,
        channel_bw=channel_bw, output_rate=intermediate_rate,
    )
    return fm_demodulate(
        channel_iq, intermediate_rate,
        audio_rate=audio_rate,
        max_deviation=max_deviation,
    )
