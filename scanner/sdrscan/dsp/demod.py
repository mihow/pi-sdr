"""Stateful NFM and AM demodulators. Each keeps its filter state between blocks.

Squelch never comes from these outputs: FM-demodulated noise has about the same RMS
whatever the signal strength, so the channelizer reports RF channel power instead.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.signal import lfilter


class DcBlocker:
    def __init__(self, pole: float = 0.995) -> None:
        self.b = np.array([1.0, -1.0])
        self.a = np.array([1.0, -pole])
        self.zi = np.zeros(1)

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self.zi = lfilter(self.b, self.a, x, zi=self.zi)
        return y


class NfmDemod:
    """Quadrature discriminator scaled so the full deviation reads 1.0, then de-emphasis."""

    def __init__(self, rate: int, deviation_hz: float, deemph_us: float = 75.0) -> None:
        self.scale = rate / (2 * math.pi * deviation_hz)
        alpha = math.exp(-1.0 / (rate * deemph_us * 1e-6)) if deemph_us > 0 else 0.0
        self.b = np.array([1.0 - alpha])
        self.a = np.array([1.0, -alpha])
        self.zi = np.zeros(1)
        self.prev = np.complex64(1.0)
        self.dc = DcBlocker()

    def process(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            return np.zeros(0, dtype=np.float32)
        prev = np.concatenate(([self.prev], x[:-1]))
        self.prev = x[-1]
        disc = np.angle(x * np.conj(prev)) * self.scale
        y, self.zi = lfilter(self.b, self.a, disc, zi=self.zi)
        return self.dc.process(y).astype(np.float32)


class AmDemod:
    """Envelope, DC removal and a slow AGC that brings the audio near unit peak."""

    def __init__(self, rate: int, agc_s: float = 0.5) -> None:
        self.dc = DcBlocker(pole=1.0 - 30.0 / rate)
        self.k = 1.0 - math.exp(-1.0 / (rate * agc_s))
        self.level = None

    def process(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            return np.zeros(0, dtype=np.float32)
        env = np.abs(x).astype(np.float64)
        audio = self.dc.process(env)
        mag = float(np.mean(np.abs(audio))) + 1e-12
        # One AGC step per block: blocks are about 0.1 s, so this is the slow AGC.
        if self.level is None:
            self.level = mag
        else:
            steps = len(x) * self.k
            w = min(1.0, steps)
            self.level = (1 - w) * self.level + w * mag
        return (audio * (0.3 / self.level)).astype(np.float32)


def nfm_demod(x: np.ndarray, rate: int, deviation_hz: float, deemph_us: float = 75.0):
    """One-shot NFM demodulation at the input rate (fresh filter state)."""
    return NfmDemod(rate, deviation_hz, deemph_us).process(np.asarray(x, dtype=np.complex64))


def am_demod(x: np.ndarray, rate: int) -> np.ndarray:
    """One-shot AM demodulation at the input rate (fresh filter state)."""
    return AmDemod(rate).process(np.asarray(x, dtype=np.complex64))
