"""Small generated audio signals at AUDIO_RATE for voice-gate, clip and STT tests."""

from __future__ import annotations

import numpy as np

from sdrscan import AUDIO_RATE
from sdrscan.dsp.channelizer import StreamingDecimator, _taps
from sdrscan.dsp.demod import NfmDemod
from sdrscan.iq.synthetic import voice_weights


def voice(seconds: float, f0: float = 120.0, seed: int = 0) -> np.ndarray:
    """Harmonic pulses at the pitch ``f0``, shaped by three formants, with a syllable rhythm."""
    t = np.arange(int(seconds * AUDIO_RATE)) / AUDIO_RATE
    w = voice_weights(f0)
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 3.0 * t) ** 2
    x = sum(a * np.sin(2 * np.pi * k * f0 * t) for k, a in enumerate(w, start=1))
    return (0.5 * env * x / w.sum()).astype(np.float32)


def fm_noise(seconds: float, seed: int = 1) -> np.ndarray:
    """What an NFM discriminator gives on an empty channel, decimated to AUDIO_RATE."""
    rng = np.random.default_rng(seed)
    n = int(seconds * 48_000)
    x = (rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex64)
    d = NfmDemod(48_000, 2_500).process(x).astype(np.float32)
    return StreamingDecimator(_taps(63, 3_800.0, 48_000), 48_000 // AUDIO_RATE).push(d)


def dtmf(seconds: float, lo: float = 697.0, hi: float = 1209.0) -> np.ndarray:
    t = np.arange(int(seconds * AUDIO_RATE)) / AUDIO_RATE
    return (0.4 * np.sin(2 * np.pi * lo * t) + 0.4 * np.sin(2 * np.pi * hi * t)).astype(np.float32)


def ctcss(seconds: float, hz: float = 107.2, amp: float = 0.3) -> np.ndarray:
    """A CTCSS sub-audible tone, by default louder than ``voice`` (as on a real repeater)."""
    t = np.arange(int(seconds * AUDIO_RATE)) / AUDIO_RATE
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def afsk(seconds: float, baud: int = 1200, seed: int = 3) -> np.ndarray:
    """Bell 202 AFSK: continuous-phase 1200 and 2200 Hz tones keyed by random bits."""
    rng = np.random.default_rng(seed)
    n = int(seconds * AUDIO_RATE)
    bit = (np.arange(n) * baud // AUDIO_RATE).astype(int)
    hz = np.where(rng.integers(0, 2, bit[-1] + 1)[bit] == 1, 1200.0, 2200.0)
    return (0.4 * np.sin(2 * np.pi * np.cumsum(hz) / AUDIO_RATE)).astype(np.float32)
