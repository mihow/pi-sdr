"""Voice gate interface. The classifying ``VoiceGate`` arrives in Part B (plan task B1).

The runner talks to a gate only through ``Gate``. ``AlwaysNoiseGate`` is the Part A stand-in:
it says every transmission is noise, so nothing is recorded.
"""

from __future__ import annotations

import enum
import typing

import numpy as np

from .. import AUDIO_RATE


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
        return {"voice_ratio": 0.0, "frames": self._samples // (self.rate * 30 // 1000)}

    def reset(self) -> None:
        self._samples = 0
