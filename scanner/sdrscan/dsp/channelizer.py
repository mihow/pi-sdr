"""Extract many channels from one wideband IQ stream and demodulate each to 16 kHz audio.

Per channel: complex mix to baseband, FIR decimation 2.4 MHz -> 240 kHz -> 48 kHz, the
demodulator at 48 kHz, then an audio FIR 48 kHz -> 16 kHz. Every filter and the mixer keep
their state between blocks, so any block size gives the same audio.
"""

from __future__ import annotations

import functools
import math

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy.signal import firwin

from .. import AUDIO_RATE
from ..bandplan import Channel
from .demod import AmDemod, NfmDemod

IF_RATE = 48_000


class StreamingDecimator:
    """FIR filter followed by keep-every-``down``-th sample, exact across block boundaries."""

    def __init__(self, taps: np.ndarray, down: int) -> None:
        self.hrev = np.ascontiguousarray(np.asarray(taps)[::-1])
        self.down = down
        self.L = len(taps)
        self.buf: np.ndarray | None = None

    def push(self, x: np.ndarray) -> np.ndarray:
        if self.buf is None:
            self.buf = np.zeros(self.L - 1, dtype=x.dtype)
        data = np.concatenate((self.buf, x))
        n_out = max(0, (len(data) - self.L) // self.down + 1)
        if n_out == 0:
            self.buf = data
            return np.zeros(0, dtype=x.dtype)
        win = sliding_window_view(data, self.L)[:: self.down][:n_out]
        y = win @ self.hrev.astype(win.dtype if np.iscomplexobj(win) else self.hrev.dtype)
        self.buf = data[n_out * self.down :]
        return y.astype(x.dtype, copy=False)


@functools.lru_cache(maxsize=32)
def _taps(numtaps: int, cutoff_hz: float, fs: float) -> np.ndarray:
    return firwin(numtaps, cutoff_hz, fs=fs).astype(np.float32)


class _ChannelChain:
    def __init__(self, rate: int, center_hz: int, ch: Channel) -> None:
        if rate % 10 or (rate // 10) % 5:
            raise ValueError(f"sample rate {rate} must be a multiple of 240 kHz x 10")
        self.ch = ch
        self.rate = rate
        self.step = -2 * math.pi * (ch.hz - center_hz) / rate
        self.phase = 0.0
        self._ramp: np.ndarray | None = None
        r1 = rate // 10
        down2 = r1 // IF_RATE
        if r1 % IF_RATE:
            raise ValueError(f"{r1} Hz is not an integer multiple of {IF_RATE} Hz")
        self.stage1 = StreamingDecimator(_taps(51, 40_000.0, rate), 10)
        cutoff = min(max(ch.bw_hz / 2 + 1_000.0, 5_000.0), 10_000.0)
        self.stage2 = StreamingDecimator(_taps(63, cutoff, r1), down2)
        if ch.mode == "am":
            self.demod: NfmDemod | AmDemod = AmDemod(IF_RATE)
        else:
            self.demod = NfmDemod(IF_RATE, ch.deviation_hz)
        self.audio = StreamingDecimator(_taps(63, 3_800.0, IF_RATE), IF_RATE // AUDIO_RATE)
        self.power_db = -200.0

    def push(self, iq: np.ndarray) -> np.ndarray:
        n = len(iq)
        if self._ramp is None or len(self._ramp) != n:
            # Blocks usually have one size, so the mixer ramp is computed once per dwell.
            self._ramp = np.exp(1j * self.step * np.arange(n, dtype=np.float64))
        rot = np.complex128(complex(math.cos(self.phase), math.sin(self.phase)))
        mixed = iq * (self._ramp * rot).astype(np.complex64)
        self.phase = math.fmod(self.phase + self.step * n, 2 * math.pi)
        bb = self.stage2.push(self.stage1.push(mixed))
        if len(bb):
            self.power_db = float(10 * np.log10(np.mean(np.abs(bb) ** 2) + 1e-30))
        return self.audio.push(self.demod.process(bb).astype(np.float32))


class Channelizer:
    def __init__(self, rate: int, center_hz: int, channels: list[Channel]) -> None:
        self.rate = rate
        self.center_hz = center_hz
        self.audio_rate = AUDIO_RATE
        self.chains = {ch.hz: _ChannelChain(rate, center_hz, ch) for ch in channels}

    def push(self, iq: np.ndarray) -> dict[int, np.ndarray]:
        """Audio for each channel (Hz key), float32 at ``AUDIO_RATE``."""
        iq = np.asarray(iq, dtype=np.complex64)
        return {hz: chain.push(iq) for hz, chain in self.chains.items()}

    def rf_power_db(self) -> dict[int, float]:
        """Channel-filtered RF power of the last block, the squelch source."""
        return {hz: chain.power_db for hz, chain in self.chains.items()}
