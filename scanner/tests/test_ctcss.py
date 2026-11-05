"""CTCSS tone measurement on demodulated audio, before the voice gate's high-pass."""

from __future__ import annotations

import numpy as np
import pytest

from sdrscan import AUDIO_RATE
from sdrscan.voice.ctcss import CTCSS_TONES, ToneMeter
from sdrscan.voice.gate import VoiceGate

from .audio import ctcss, dtmf, fm_noise, voice


def _measure(pcm: np.ndarray, chunk: int = 1600) -> float | None:
    m = ToneMeter()
    for i in range(0, len(pcm), chunk):
        m.push(pcm[i : i + chunk])
    return m.tone_hz()


@pytest.mark.parametrize("hz", [67.0, 100.0, 107.2, 151.4, 156.7, 203.5, 254.1])
def test_standard_tone_is_found(hz):
    assert _measure(ctcss(1.0, hz=hz, amp=0.1) + 0.02 * fm_noise(1.0)) == hz


def test_neighbouring_tones_are_told_apart():
    assert _measure(ctcss(1.5, hz=150.0, amp=0.1)) == 150.0
    assert _measure(ctcss(1.5, hz=151.4, amp=0.1)) == 151.4


def test_tone_under_speech_is_found():
    assert _measure(voice(2.0) + ctcss(2.0, hz=100.0, amp=0.15)) == 100.0


@pytest.mark.parametrize(
    "pcm",
    [voice(1.5), fm_noise(1.5), dtmf(1.0), np.zeros(AUDIO_RATE, np.float32)],
    ids=["voice", "fm-noise", "dtmf", "silence"],
)
def test_no_tone_without_one(pcm):
    assert _measure(pcm) is None


def test_too_short_to_measure():
    assert _measure(ctcss(0.2, hz=100.0)) is None


def test_off_list_tone_is_not_reported():
    assert _measure(ctcss(1.0, hz=120.9, amp=0.1)) is None


def test_gate_reports_the_tone_it_filters_out():
    gate = VoiceGate()
    pcm = ctcss(1.0, hz=100.0, amp=0.1)
    for i in range(0, len(pcm), 1600):
        gate.push(pcm[i : i + 1600])
    s = gate.summary()
    assert s["ctcss_hz"] == 100.0
    assert s["class"] == "unknown"  # the tone alone is not voice or data
    gate.reset()
    assert gate.summary()["ctcss_hz"] is None


def test_tone_list_is_the_standard_one():
    assert len(CTCSS_TONES) == 50
    assert CTCSS_TONES[0] == 67.0 and CTCSS_TONES[-1] == 254.1
    assert 100.0 in CTCSS_TONES and 107.2 in CTCSS_TONES
