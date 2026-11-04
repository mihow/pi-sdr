"""A synthetic RTL-SDR: renders a scene into 8-bit-like IQ on an injected clock.

Two outputs share one scene. ``read`` renders real complex64 samples (tones, FM and AM
voice-like audio, noise, a DC spike, optional clipping, 8-bit quantization). ``read_psd``
returns the averaged spectrum the detector would compute from those samples, without
rendering them. Scene simulations of a day or a week use ``read_psd``; a test checks that
both outputs agree.
"""

from __future__ import annotations

import math
import zlib

import numpy as np

from .. import IQ_RATE
from ..clock import Clock
from ..dsp.spectrum import hann_kernel
from .base import IqSourceError, settle_s
from .scene import Emitter, Scene

_BLOCKER_OFFSET_HZ = 1_150_000  # outside the cropped band; only drives the ADC to the rail
_BLOCKER_AMPLITUDE = 1.4


def voice_weights(f0: float, n: int = 20) -> np.ndarray:
    """Harmonic amplitudes of a voice-like sound: pitch harmonics shaped by three formants."""
    k = np.arange(1, n + 1)
    f = k * f0
    shape = sum(
        np.exp(-0.5 * ((f - fc) / bw) ** 2) for fc, bw in ((500, 200), (1500, 250), (2500, 300))
    )
    return shape * (f < 3400)


class SyntheticSource:
    def __init__(
        self,
        scene: Scene,
        clock: Clock,
        rate: int = IQ_RATE,
        quantize: bool | None = None,
        simulate_settle: bool = True,
    ) -> None:
        self.scene = scene
        self.clock = clock
        self.rate = rate
        self.quantize = scene.quantize if quantize is None else quantize
        self.simulate_settle = simulate_settle
        self._open = False
        self._opened_once = False
        self._center: int | None = None
        self._gain: float | None = None
        self._reopen = 0
        self._last_block: float | None = None
        self._rng = np.random.default_rng(scene.seed)
        self._pools: dict[int, np.ndarray] = {}

    # -- IqSource protocol -------------------------------------------------------------
    def open(self) -> None:
        if self.scene.failing_at(self._t_rel()):
            raise IqSourceError("synthetic dongle unavailable (scene fail_at)")
        if self._opened_once:
            self._reopen += 1
        self._open = True
        self._opened_once = True
        self._center = None

    def tune(self, center_hz: int, gain_db: float) -> None:
        if not self._open:
            raise IqSourceError("source is not open")
        if self.simulate_settle:
            self.clock.sleep(settle_s(self._center, int(center_hz)))
        self._center = int(center_hz)
        self._gain = float(gain_db)

    def read(self, n: int) -> np.ndarray:
        center = self._check_ready()
        t_rel = self._t_rel()
        iq = self._render(center, t_rel, n)
        self._advance(n)
        return iq

    def read_psd(self, nfft: int, navg: int) -> tuple[np.ndarray, float]:
        """The expected Welch PSD (dB, fftshifted) of the next ``nfft * navg`` samples."""
        center = self._check_ready()
        n = nfft * navg
        t_rel = self._t_rel()
        dur = n / self.rate
        bin_hz = self.rate / nfft
        noise = 10 ** (self.scene.noise_db(center) / 10)
        power = noise * self._gamma(navg, nfft)
        kernel = hann_kernel(nfft)
        kc = len(kernel) // 2
        os_ = len(kernel) // nfft

        def add_tone(off_hz: float, p: float) -> None:
            pos = off_hz / bin_hz + nfft // 2
            k0 = int(round(pos))
            for k in range(k0 - 4, k0 + 5):
                if 0 <= k < nfft:
                    idx = kc + int(round((k - pos) * os_))
                    power[k] += p * kernel[idx]

        def add_flat(off_hz: float, p: float, bw: float) -> None:
            lo = int(math.floor((off_hz - bw / 2) / bin_hz)) + nfft // 2
            hi = int(math.ceil((off_hz + bw / 2) / bin_hz)) + nfft // 2
            lo, hi = max(lo, 0), min(hi, nfft)
            if hi > lo:
                power[lo:hi] += p * self.rate / max(bw, bin_hz)

        add_tone(0.0, 10 ** (self.scene.dc_db / 10))
        for e in self.scene.emitters:
            off = self._offset(e, center)
            if off is None:
                continue
            frac = sum(b - a for a, b in e.active_spans(t_rel, t_rel + dur)) / dur
            if frac <= 0:
                continue
            p = 10 ** (e.level_db / 10) * frac
            if e.kind in {"carrier", "spur"}:
                add_tone(off, p)
            elif e.kind == "nfm_voice":
                fm = e.tone_hz or 3_000.0
                add_flat(off, p, 2 * (e.deviation_hz + fm))
            elif e.kind == "am_voice":
                add_tone(off, p * 0.76)
                add_flat(off, p * 0.24, 6_000.0)
            elif e.kind == "wfm":
                add_flat(off, p, 180_000.0)
        clip = 0.0
        if self.scene.clip:
            probe = self._render(center, t_rel, 8192)
            rail = (np.abs(probe.real) >= 0.99) | (np.abs(probe.imag) >= 0.99)
            clip = float(rail.mean())
        self._advance(n)
        return 10 * np.log10(power + 1e-30), clip

    def close(self) -> None:
        self._open = False

    def health(self) -> dict:
        age = 0.0 if self._last_block is None else self.clock.monotonic() - self._last_block
        return {"dropped": 0, "reopen_count": self._reopen, "last_block_age_s": age}

    # -- internals ---------------------------------------------------------------------
    def _gamma(self, navg: int, nfft: int) -> np.ndarray:
        """Averaged-periodogram noise factors (Gamma(navg) / navg), from a reused pool.

        Drawing 4096 gamma values per hop dominated week-long simulations; a random slice
        of a large pool has the same distribution per bin.
        """
        pool = self._pools.get(navg)
        if pool is None:
            pool = self._pools[navg] = self._rng.gamma(navg, 1.0 / navg, size=1 << 20)
        start = int(self._rng.integers(0, len(pool) - nfft))
        return pool[start : start + nfft].copy()

    def _t_rel(self) -> float:
        return self.clock.now() - self.scene.t0

    def _check_ready(self) -> int:
        if not self._open:
            raise IqSourceError("source is not open")
        if self.scene.failing_at(self._t_rel()):
            self._open = False
            raise IqSourceError("synthetic read failed (scene fail_at)")
        if self._center is None:
            raise IqSourceError("source is not tuned")
        return self._center

    def _advance(self, n: int) -> None:
        self.clock.sleep(n / self.rate)
        self._last_block = self.clock.monotonic()

    def _offset(self, e: Emitter, center: int) -> float | None:
        if e.offset_hz is not None:
            return float(e.offset_hz)
        off = float(e.hz - center)  # type: ignore[operator]
        half = self.rate / 2 + (90_000 if e.kind == "wfm" else 10_000)
        return off if abs(off) < half else None

    def _render(self, center: int, t_rel: float, n: int) -> np.ndarray:
        t = t_rel + np.arange(n, dtype=np.float64) / self.rate
        sigma = math.sqrt(10 ** (self.scene.noise_db(center) / 10) / 2)
        x = (self._rng.normal(0, sigma, n) + 1j * self._rng.normal(0, sigma, n)).astype(
            np.complex128
        )
        x += 10 ** (self.scene.dc_db / 20)
        for e in self.scene.emitters:
            off = self._offset(e, center)
            if off is None:
                continue
            spans = e.active_spans(t[0], t[-1] + 1 / self.rate)
            if not spans:
                continue
            sig = self._emit(e, off, t)
            if not e.always:
                mask = np.zeros(n, dtype=bool)
                for a, b in spans:
                    mask |= (t >= a) & (t < b)
                sig = sig * mask
            x += sig
        if self.scene.clip:
            x += _BLOCKER_AMPLITUDE * np.exp(2j * np.pi * _BLOCKER_OFFSET_HZ * t)
        if self.quantize:
            re = np.clip(np.round(x.real * 127.5 + 127.5), 0, 255)
            im = np.clip(np.round(x.imag * 127.5 + 127.5), 0, 255)
            x = (re - 127.5) / 127.5 + 1j * (im - 127.5) / 127.5
        return x.astype(np.complex64)

    def _emit(self, e: Emitter, off: float, t: np.ndarray) -> np.ndarray:
        amp = 10 ** (e.level_db / 20)
        carrier_phase = 2 * np.pi * off * t
        if e.kind in {"carrier", "spur"}:
            return amp * np.exp(1j * carrier_phase)
        if e.kind == "wfm":
            beta = 75_000 * (
                -0.5 * np.cos(2 * np.pi * 400 * t) / 400 - 0.5 * np.cos(2 * np.pi * 1900 * t) / 1900
            )
            return amp * np.exp(1j * (carrier_phase + beta))
        audio, integral = self._audio(e, t)
        if e.kind == "nfm_voice":
            return amp * np.exp(1j * (carrier_phase + 2 * np.pi * e.deviation_hz * integral))
        # am_voice
        return amp * (1 + 0.8 * audio) / math.sqrt(1 + 0.32) * np.exp(1j * carrier_phase)

    def _audio(self, e: Emitter, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Audio in [-1, 1] and its time integral (closed form, so phase is continuous)."""
        if e.tone_hz:
            w = 2 * np.pi * e.tone_hz
            return np.sin(w * t), -np.cos(w * t) / w
        f0 = 100.0 + (zlib.crc32(str(e.hz).encode()) % 80)
        weights = voice_weights(f0)
        norm = float(weights.sum()) or 1.0
        env = 0.55 + 0.45 * np.sin(2 * np.pi * 3.0 * t) ** 2  # syllable rhythm
        audio = np.zeros_like(t)
        integral = np.zeros_like(t)
        for k, a in enumerate(weights, start=1):
            if a < 1e-3:
                continue
            w = 2 * np.pi * k * f0
            audio += a * np.sin(w * t)
            integral += -a * np.cos(w * t) / w
        return env * audio / norm, env * integral / norm
