"""Voice gate: decide whether squelch-open channel audio is speech, data or noise.

Only speech is worth a clip and a transcript, so the gate decides what the rig keeps. It
works on 30 ms frames at ``AUDIO_RATE`` and keeps its frames across ``push`` calls, so audio
arriving in small pieces still reaches a decision (v1 reset per 27 ms read and never did).

The gate first high-passes its input so that only the voice band is measured (the filter
passes from about 350 Hz). Repeaters send a CTCSS tone (67 to 254.1 Hz) or a DCS code
(134.4 bit/s) under the speech, and that tone often carries most of the demodulated energy.
Without the filter, the tone test below counts the tone and labels speech as data. A frame
with only sub-audible content is then below ``SILENCE_RMS``, so a squelch tail or kerchunk
is not voice. A ``ToneMeter`` reads the unfiltered input, and ``summary`` reports the
tone it found as ``ctcss_hz``.

A frame is voice when webrtcvad (mode 2) votes speech AND the frame is pitched
(autocorrelation above 0.2 at 80 to 400 Hz) or not flat (spectral flatness below 0.35),
AND its energy is not held by one or two pure tones. A frame whose energy is in one or two
tones is data (DTMF, tone signalling). Over the last ``window`` frames, at least 30% voice
frames means VOICE and at least 50% tone frames means DATA. The thresholds come from v1 and
are untuned on real audio.

The runner reaches a gate only through ``Gate``. ``AlwaysNoiseGate`` records nothing and
remains for tests that need a gate with no opinion.
"""

from __future__ import annotations

import collections
import enum
import typing

import numpy as np
from scipy.signal import cheby2, sosfilt, sosfilt_zi

from .. import AUDIO_RATE
from .ctcss import ToneMeter

PITCH_MIN = 0.2
FLATNESS_MAX = 0.35
TONAL_MIN = 0.80
VOICE_SHARE = 0.30
DATA_SHARE = 0.50
MIN_FRAMES = 10
SILENCE_RMS = 1e-3  # demodulated audio is scaled to about [-1, 1]
# High-pass: at least 60 dB down from 260 Hz (the highest CTCSS tone is 254.1 Hz), within
# 3 dB from about 350 Hz.
SUBAUDIBLE_STOP_HZ = 260.0
SUBAUDIBLE_STOP_DB = 60.0


class VoiceState(enum.Enum):
    PENDING = "pending"
    VOICE = "voice"
    DATA = "data"
    NOISE = "noise"


class Gate(typing.Protocol):
    rate: int

    def push(self, pcm: np.ndarray) -> VoiceState: ...

    def summary(self) -> dict: ...

    def reset(self) -> None: ...


class AlwaysNoiseGate:
    def __init__(self, rate: int = AUDIO_RATE) -> None:
        self.rate = rate
        self._samples = 0

    def push(self, pcm: np.ndarray) -> VoiceState:
        self._samples += len(pcm)
        return VoiceState.NOISE

    def summary(self) -> dict:
        return {
            "voice_ratio": 0.0,
            "frames": self._samples // (self.rate * 30 // 1000),
            "class": "unknown",
        }

    def reset(self) -> None:
        self._samples = 0


def _load_vad():
    """The webrtcvad extension, without its wrapper's import of ``pkg_resources``.

    The ``webrtcvad`` module imports ``pkg_resources`` only to read its version, and newer
    setuptools no longer ships it. The compiled ``_webrtcvad`` module is all the gate uses.
    """
    try:
        import _webrtcvad
    except ImportError:  # pragma: no cover - only when the extension is missing
        return None
    return _webrtcvad


class VoiceGate:
    def __init__(self, rate: int = AUDIO_RATE, frame_ms: int = 30, window: int = 30) -> None:
        self.rate = rate
        self.frame = rate * frame_ms // 1000
        self.window = window
        self._vad_mod = _load_vad()
        self._win = np.hanning(self.frame)
        freqs = np.fft.rfftfreq(self.frame, 1 / rate)
        self._band = (freqs >= 300) & (freqs <= 3400)
        self._lag_lo = rate // 400
        self._lag_hi = rate // 80
        self._sos = cheby2(
            10, SUBAUDIBLE_STOP_DB, SUBAUDIBLE_STOP_HZ, btype="highpass", fs=rate, output="sos"
        )
        self._tone = ToneMeter(rate)
        self.reset()

    def reset(self) -> None:
        """Forget everything. Call only when a transmission ends."""
        self._buf = np.zeros(0, dtype=np.float32)
        self._zi: np.ndarray | None = None
        self._recent: collections.deque[str] = collections.deque(maxlen=self.window)
        self._counts: collections.Counter[str] = collections.Counter()
        self._flat_sum = 0.0
        self._pitch_hits = 0
        self._state = VoiceState.PENDING
        self._tone.reset()
        self._vad = None
        if self._vad_mod is not None:
            self._vad = self._vad_mod.create()
            self._vad_mod.init(self._vad)
            self._vad_mod.set_mode(self._vad, 2)

    def push(self, pcm: np.ndarray) -> VoiceState:
        self._tone.push(pcm)  # the CTCSS tone, measured before the high-pass removes it
        data = np.concatenate((self._buf, self._highpass(pcm)))
        n = len(data) // self.frame
        for i in range(n):
            label = self._classify(data[i * self.frame : (i + 1) * self.frame])
            self._recent.append(label)
            self._counts[label] += 1
        self._buf = data[n * self.frame :]
        if n:
            self._state = self._decide()
        return self._state

    def summary(self) -> dict:
        frames = sum(self._counts.values())
        ratio = self._counts["voice"] / frames if frames else 0.0
        sounded = frames - self._counts["silent"]
        measured = sounded - self._counts["data"]
        cls = "unknown"
        if frames >= MIN_FRAMES:
            if self._counts["data"] / frames >= DATA_SHARE:
                cls = "data"
            elif ratio >= VOICE_SHARE:
                cls = "voice"
        return {
            "class": cls,
            "frames": frames,
            "voice_ratio": round(ratio, 3),
            "flatness": round(self._flat_sum / measured, 3) if measured else None,
            "pitch_hits": self._pitch_hits,
            "state": self._state.value,
            "silent_share": round(self._counts["silent"] / frames, 3) if frames else None,
            "ctcss_hz": self._tone.tone_hz(),
        }

    # -- internals ---------------------------------------------------------------------
    def _decide(self) -> VoiceState:
        if sum(self._counts.values()) < MIN_FRAMES:
            return VoiceState.PENDING
        recent = collections.Counter(self._recent)
        k = len(self._recent)
        if recent["data"] / k >= DATA_SHARE:
            return VoiceState.DATA
        if recent["voice"] / k >= VOICE_SHARE:
            return VoiceState.VOICE
        return VoiceState.NOISE

    def _highpass(self, pcm: np.ndarray) -> np.ndarray:
        """Remove sub-audible tones; the filter state runs across ``push`` calls."""
        x = np.asarray(pcm, dtype=np.float64)
        if not len(x):
            return np.zeros(0, dtype=np.float32)
        if self._zi is None:
            # Start from a steady state on the first sample so a DC offset does not ring.
            self._zi = sosfilt_zi(self._sos) * x[0]
        y, self._zi = sosfilt(self._sos, x, zi=self._zi)
        return y.astype(np.float32)

    def _classify(self, f: np.ndarray) -> str:
        x = f.astype(np.float64)
        x -= x.mean()
        rms = float(np.sqrt(np.mean(x * x)))
        if rms < SILENCE_RMS:
            return "silent"
        p = np.abs(np.fft.rfft(x * self._win)) ** 2
        if self._tonal_share(p) / (float(p.sum()) + 1e-30) >= TONAL_MIN:
            return "data"
        pb = p[self._band] + 1e-20
        flat = float(np.exp(np.mean(np.log(pb))) / np.mean(pb))
        self._flat_sum += flat
        ac = np.correlate(x, x, "full")[len(x) - 1 :]
        pitch = float(ac[self._lag_lo : self._lag_hi + 1].max() / (ac[0] + 1e-30))
        pitched = pitch > PITCH_MIN
        self._pitch_hits += int(pitched)
        if self._vad_votes(x, rms) and (pitched or flat < FLATNESS_MAX):
            return "voice"
        return "noise"

    @staticmethod
    def _tonal_share(p: np.ndarray) -> float:
        """Energy within two bins of the two strongest spectral peaks."""
        taken = np.zeros(len(p), dtype=bool)
        found = 0
        for k in np.argsort(p)[::-1]:
            if taken[k]:
                continue
            taken[max(0, k - 2) : k + 3] = True
            found += 1
            if found == 2:
                break
        return float(p[taken].sum())

    def _vad_votes(self, x: np.ndarray, rms: float) -> bool:
        if self._vad is None:
            return True  # without webrtcvad the pitch and flatness tests decide alone
        pcm = np.clip(x / rms * 3000.0, -32767, 32767).astype(np.int16)
        return bool(self._vad_mod.process(self._vad, self.rate, pcm.tobytes(), self.frame))
