"""Averaged power spectra.

The scale is chosen so that complex white noise of variance s2 reads ``10*log10(s2)`` dB in
every bin. All detector levels are then relative to that floor, never absolute.
"""

from __future__ import annotations

import functools

import numpy as np


@functools.lru_cache(maxsize=8)
def hann(nfft: int) -> np.ndarray:
    return np.hanning(nfft + 1)[:-1].astype(np.float32)


def welch_psd(iq: np.ndarray, nfft: int = 4096, navg: int = 50) -> np.ndarray:
    """Average ``navg`` non-overlapping Hann-windowed FFTs. Returns dB per bin, fftshifted."""
    nseg = min(navg, len(iq) // nfft)
    if nseg < 1:
        raise ValueError(f"need at least {nfft} samples, got {len(iq)}")
    w = hann(nfft)
    segs = np.asarray(iq[: nseg * nfft], dtype=np.complex64).reshape(nseg, nfft) * w
    power = np.abs(np.fft.fft(segs, axis=1)) ** 2
    mean = power.mean(axis=0) / float(np.sum(w.astype(np.float64) ** 2))
    return 10.0 * np.log10(np.fft.fftshift(mean) + 1e-30)


@functools.lru_cache(maxsize=8)
def hann_kernel(nfft: int, oversample: int = 16) -> np.ndarray:
    """Power response of the Hann window at fractional bin offsets, on the PSD scale above.

    Index ``i`` is the offset ``(i - center) / oversample`` bins. Used by the fast synthetic
    path to place a tone into bins exactly as the FFT would.
    """
    w = hann(nfft).astype(np.float64)
    spec = np.abs(np.fft.fft(w, nfft * oversample)) ** 2 / np.sum(w**2)
    return np.fft.fftshift(spec)
