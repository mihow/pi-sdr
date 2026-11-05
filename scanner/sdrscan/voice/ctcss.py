"""Measure the CTCSS tone under a transmission, which names the repeater or user group.

Repeaters and many radios send one sub-audible tone (67.0 to 254.1 Hz) for the whole
transmission. The tone survives a key-up with no speech, so it identifies who keyed up
even when there is nothing to transcribe. The voice gate high-passes these tones away, so
the meter reads the demodulated audio before that filter.

The meter low-passes and decimates its input to 1 kHz and keeps the last ``MAX_S``
seconds (4,000 samples, so the cost per transmission is small). ``tone_hz`` finds the
strongest component between 60 and 260 Hz with a zero-padded FFT (0.5 Hz bins), refines
it to 0.05 Hz with a direct DFT around that bin, and
reports the nearest standard tone when all of these hold:

- at least ``MIN_S`` of audio, for a resolution near 2 Hz;
- the component holds at least ``MIN_SHARE`` of the low-passed energy, so speech, FM
  noise and data, which spread their energy, do not count;
- its amplitude is at least ``MIN_AMP`` (a CTCSS tone is usually 10 to 20% of full
  deviation), so a faint filter residue of a strong audio tone does not count;
- it is within ``MATCH_HZ`` of a tone on the standard list.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import cheby2, sosfilt, sosfilt_zi

from .. import AUDIO_RATE

CTCSS_TONES = (
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5,
    107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7,
    159.8, 162.2, 165.5, 167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8,
    196.6, 199.5, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
)  # fmt: skip
EXTRA_TONES = (150.0,)  # in many radios' lists, though not in the EIA set of 50
METER_RATE = 1000
MAX_S = 4.0
MIN_S = 0.5
MIN_SHARE = 0.25
MIN_AMP = 0.01
MATCH_HZ = 1.0
_LO_HZ, _HI_HZ = 60.0, 260.0


class ToneMeter:
    def __init__(self, rate: int = AUDIO_RATE) -> None:
        if rate % METER_RATE:
            raise ValueError(f"rate must be a multiple of {METER_RATE}")
        self.rate = rate
        self._step = rate // METER_RATE
        # Pass up to 300 Hz; at least 60 dB down from 400 Hz, so nothing aliases into the
        # tone band after decimation to 1 kHz.
        self._sos = cheby2(8, 60, 400, btype="lowpass", fs=rate, output="sos")
        self._tones = np.array(sorted(CTCSS_TONES + EXTRA_TONES))
        self.reset()

    def reset(self) -> None:
        self._zi: np.ndarray | None = None
        self._phase = 0
        self._kept = np.zeros(0)
        self._cached: tuple[float | None] | None = None

    def push(self, pcm: np.ndarray) -> None:
        x = np.asarray(pcm, dtype=np.float64)
        if not len(x):
            return
        if self._zi is None:
            self._zi = sosfilt_zi(self._sos) * x[0]
        y, self._zi = sosfilt(self._sos, x, zi=self._zi)
        dec = y[self._phase :: self._step]
        self._phase = (self._phase - len(x)) % self._step
        self._kept = np.concatenate((self._kept, dec))[-int(MAX_S * METER_RATE) :]
        self._cached = None

    def seconds(self) -> float:
        return len(self._kept) / METER_RATE

    def tone_hz(self) -> float | None:
        """The standard tone heard, or None. Cached until the next ``push``."""
        if self._cached is None:
            self._cached = (self._measure(),)
        return self._cached[0]

    def _measure(self) -> float | None:
        x = self._kept
        if len(x) < MIN_S * METER_RATE:
            return None
        x = x - x.mean()
        w = np.hanning(len(x))
        xw = x * w
        energy = float(np.sum(xw * xw))
        if energy <= 0:
            return None
        # Coarse: a zero-padded FFT with bins of 0.5 Hz or finer.
        nfft = -(-len(x) // (2 * METER_RATE)) * 2 * METER_RATE
        freqs = np.fft.rfftfreq(nfft, 1 / METER_RATE)
        band = (freqs >= _LO_HZ) & (freqs <= _HI_HZ)
        spec = np.abs(np.fft.rfft(xw, nfft)) ** 2
        f0 = float(freqs[band][int(np.argmax(spec[band]))])
        # Fine: a direct DFT at 0.05 Hz steps around the coarse peak.
        fine = np.arange(f0 - 0.5, f0 + 0.5001, 0.05)
        basis = np.exp(-2j * np.pi * np.outer(fine, np.arange(len(x))) / METER_RATE)
        p = np.abs(basis @ xw) ** 2
        k = int(np.argmax(p))
        f, peak = float(fine[k]), float(p[k])
        # A pure tone gives |X|^2 = (A * sum(w) / 2)^2 and windowed energy A^2/2 * sum(w^2).
        share = peak / (energy * w.sum() ** 2 / (2 * np.sum(w * w)))
        amp = 2 * np.sqrt(peak) / w.sum()
        if share < MIN_SHARE or amp < MIN_AMP:
            return None
        nearest = float(self._tones[int(np.argmin(np.abs(self._tones - f)))])
        return nearest if abs(nearest - f) <= MATCH_HZ else None
