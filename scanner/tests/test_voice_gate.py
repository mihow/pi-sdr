from __future__ import annotations

import numpy as np

from sdrscan import AUDIO_RATE
from sdrscan.voice.gate import VoiceGate, VoiceState

from .audio import dtmf, fm_noise, voice


def _push_all(gate: VoiceGate, pcm: np.ndarray, chunk: int) -> VoiceState:
    state = VoiceState.PENDING
    for i in range(0, len(pcm), chunk):
        state = gate.push(pcm[i : i + chunk])
    return state


def test_voice_like_audio_is_voice():
    gate = VoiceGate()
    assert _push_all(gate, voice(1.5), 1600) is VoiceState.VOICE
    s = gate.summary()
    assert s["voice_ratio"] >= 0.3
    assert s["frames"] == int(1.5 * 1000 / 30)
    assert s["class"] == "voice"


def test_fm_discriminator_noise_is_noise():
    gate = VoiceGate()
    assert _push_all(gate, fm_noise(1.5), 1600) is VoiceState.NOISE
    assert gate.summary()["class"] == "unknown"


def test_dtmf_pair_is_data():
    gate = VoiceGate()
    assert _push_all(gate, dtmf(1.0), 1600) is VoiceState.DATA
    assert gate.summary()["class"] == "data"


def test_silence_is_noise_not_voice():
    gate = VoiceGate()
    assert _push_all(gate, np.zeros(AUDIO_RATE, dtype=np.float32), 1600) is VoiceState.NOISE


def test_small_chunks_accumulate_across_pushes():
    """Regression (v1 bug 3): a 27 ms hold buffer never reached a decision."""
    gate = VoiceGate()
    chunk = int(0.027 * AUDIO_RATE)
    pcm = voice(0.3)
    states = [gate.push(pcm[i : i + chunk]) for i in range(0, len(pcm), chunk)]
    assert states[0] is VoiceState.PENDING  # one 27 ms chunk is less than one frame
    assert gate.summary()["frames"] >= 9
    assert gate.push(voice(0.06)) is VoiceState.VOICE  # ten frames reached, not reset per push


def test_reset_clears_the_state():
    gate = VoiceGate()
    _push_all(gate, voice(1.0), 1600)
    gate.reset()
    assert gate.summary()["frames"] == 0
    assert gate.push(voice(0.05)) is VoiceState.PENDING


def test_gate_rate_is_the_audio_rate():
    assert VoiceGate().rate == AUDIO_RATE
