from __future__ import annotations

import numpy as np
import pytest

from sdrscan import AUDIO_RATE
from sdrscan.voice.gate import VoiceGate, VoiceState

from .audio import afsk, ctcss, dtmf, fm_noise, voice


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


def test_voice_under_a_stronger_ctcss_tone_is_voice():
    """A repeater's sub-audible tone must not outvote the speech it rides under."""
    speech = voice(1.5)
    tone = ctcss(1.5)
    assert tone.std() > 3 * speech.std()  # the tone carries most of the energy, as on air
    gate = VoiceGate()
    assert _push_all(gate, speech + tone, 1600) is VoiceState.VOICE
    assert gate.summary()["class"] == "voice"


@pytest.mark.parametrize("hz", [67.0, 107.2, 254.1])
def test_ctcss_tone_alone_is_not_voice(hz):
    """A squelch tail or kerchunk carries the tone and nothing else: no clip, no transcript."""
    gate = VoiceGate()
    assert _push_all(gate, ctcss(1.5, hz=hz), 1600) is VoiceState.NOISE
    assert gate.summary()["class"] == "unknown"


def test_ctcss_tone_over_fm_noise_is_not_voice():
    gate = VoiceGate()
    assert _push_all(gate, fm_noise(1.5) + ctcss(1.5), 1600) is VoiceState.NOISE


def test_dtmf_under_ctcss_is_data():
    """Tone signalling in the voice band stays data when a CTCSS tone is added."""
    gate = VoiceGate()
    assert _push_all(gate, dtmf(1.5) + ctcss(1.5), 1600) is VoiceState.DATA
    assert gate.summary()["class"] == "data"


@pytest.mark.xfail(
    strict=True,
    reason="The tone test finds one or two pure tones; 1200-baud AFSK is wider and reads as voice",
)
@pytest.mark.parametrize("with_ctcss", [False, True])
def test_afsk_1200_is_data(with_ctcss):
    pcm = afsk(1.5) + (ctcss(1.5) if with_ctcss else 0)
    assert _push_all(VoiceGate(), pcm, 1600) is VoiceState.DATA


def _steady_buzz(seconds: float, f0: float = 120.0) -> np.ndarray:
    """A pitched, formant-shaped hum at constant level: voice-like frames, no syllables.

    Field rigs hear this from nearby electronics on VHF low band. Frame by frame it passes
    every voice test (VAD, pitch, flatness), so only its flat envelope gives it away.
    """
    t = np.arange(int(seconds * AUDIO_RATE)) / AUDIO_RATE
    x = voice(seconds, f0=f0) / (0.55 + 0.45 * np.sin(2 * np.pi * 3.0 * t) ** 2)
    rng = np.random.default_rng(3)
    return (0.6 * x + 0.01 * rng.normal(size=len(x))).astype(np.float32)


def test_steady_buzz_with_voice_like_frames_is_not_voice():
    gate = VoiceGate()
    assert _push_all(gate, _steady_buzz(3.0), 1600) is VoiceState.NOISE
    s = gate.summary()
    assert s["class"] != "voice"
    assert s["envelope_std"] < 0.15


def test_voice_reports_a_syllabic_envelope():
    gate = VoiceGate()
    _push_all(gate, voice(3.0), 1600)
    assert gate.summary()["envelope_std"] >= 0.15
