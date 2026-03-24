"""
Wideband FFT power measurement for SDR scanning.

Given IQ samples from a 2.4 MHz window, compute the power at each
channel's offset frequency using averaged FFT (simplified Welch's method).
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


def compute_channel_power(
    iq: np.ndarray,
    sample_rate: int,
    center_freq: int,
    channel_freqs: list[int],
    channel_bandwidths: list[int],
    fft_size: int = 4096,
) -> dict[int, float]:
    """Compute power at each channel frequency from wideband IQ samples.

    Args:
        iq: Complex64 IQ samples from the SDR.
        sample_rate: Sample rate in Hz (e.g., 2_400_000).
        center_freq: Center frequency of the IQ window in Hz.
        channel_freqs: List of channel frequencies in Hz.
        channel_bandwidths: List of channel bandwidths in Hz (same length as channel_freqs).
        fft_size: FFT size per segment. Smaller than the full IQ block;
                  multiple segments are averaged.

    Returns:
        Dict mapping each channel frequency to its power in dB.
    """
    n_samples = len(iq)
    if n_samples < fft_size:
        logger.warning(
            "IQ block (%d samples) smaller than fft_size (%d), using block size",
            n_samples,
            fft_size,
        )
        fft_size = n_samples

    # Split into segments and average power spectra (simplified Welch's method)
    n_segments = n_samples // fft_size
    if n_segments == 0:
        n_segments = 1

    logger.debug(
        "FFT: %d samples, %d segments of %d, center %.3f MHz",
        n_samples,
        n_segments,
        fft_size,
        center_freq / 1e6,
    )

    # Apply Hann window to reduce spectral leakage
    window = np.hanning(fft_size).astype(np.float32)
    window_power = np.mean(window**2)

    # Accumulate power spectra
    power_spectrum = np.zeros(fft_size, dtype=np.float64)
    for i in range(n_segments):
        segment = iq[i * fft_size : (i + 1) * fft_size]
        windowed = segment * window
        spectrum = np.fft.fft(windowed)
        power_spectrum += np.abs(spectrum) ** 2

    # Average and normalize by window power
    power_spectrum /= n_segments * window_power

    # FFT bin frequency resolution
    bin_hz = sample_rate / fft_size

    # Compute power for each channel
    results: dict[int, float] = {}
    for freq, bandwidth in zip(channel_freqs, channel_bandwidths):
        offset_hz = freq - center_freq
        half_bw = bandwidth / 2

        # Check that channel is within the sampled bandwidth
        if abs(offset_hz) + half_bw > sample_rate / 2:
            logger.debug(
                "Channel %.3f MHz outside window (offset %.0f Hz, BW %.0f Hz)",
                freq / 1e6,
                offset_hz,
                bandwidth,
            )
            continue

        # Find FFT bin range for this channel
        # FFT output: bin 0 = DC (center_freq), negative freqs in upper half
        low_bin = int(np.floor((offset_hz - half_bw) / bin_hz)) % fft_size
        high_bin = int(np.ceil((offset_hz + half_bw) / bin_hz)) % fft_size

        # Extract bins, handling wrap-around
        if low_bin <= high_bin:
            channel_power = np.sum(power_spectrum[low_bin : high_bin + 1])
        else:
            # Wraps around the FFT array
            channel_power = np.sum(power_spectrum[low_bin:]) + np.sum(
                power_spectrum[: high_bin + 1]
            )

        # Avoid log10(0)
        if channel_power <= 0:
            power_db = -120.0
        else:
            power_db = 10.0 * np.log10(channel_power)

        results[freq] = float(power_db)
        logger.debug(
            "  %s: offset %+.1f kHz, bins %d-%d, power %.1f dB",
            freq,
            offset_hz / 1e3,
            low_bin,
            high_bin,
            power_db,
        )

    return results


def find_active_channels(
    powers: dict[int, float],
    squelch_level: float,
) -> list[int]:
    """Return list of frequencies with power above squelch.

    Args:
        powers: Dict mapping frequency (Hz) to power (dB).
        squelch_level: Threshold in dB. Channels above this are active.

    Returns:
        List of active channel frequencies, sorted by power (strongest first).
    """
    active = [freq for freq, power in powers.items() if power > squelch_level]
    active.sort(key=lambda f: powers[f], reverse=True)

    if active:
        logger.info(
            "Active channels (squelch %.1f dB): %s",
            squelch_level,
            ", ".join(f"{f / 1e6:.4f} MHz ({powers[f]:.1f} dB)" for f in active),
        )

    return active
