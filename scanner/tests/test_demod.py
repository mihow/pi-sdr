from __future__ import annotations

import numpy as np
import pytest

from sdrscan import AUDIO_RATE, IQ_RATE
from sdrscan.bandplan import BandPlan
from sdrscan.config import DEFAULT_BANDPLAN
from sdrscan.dsp.channelizer import Channelizer, StreamingDecimator
from sdrscan.dsp.demod import am_demod, nfm_demod
from sdrscan.iq.scene import Scene
from sdrscan.iq.synthetic import SyntheticSource
from sdrscan.voice.gate import AlwaysNoiseGate

CENTER = 462_406_250
CH1 = 462_562_500
CH2 = 462_662_500


@pytest.fixture(scope="module")
def plan() -> BandPlan:
    return BandPlan.load(DEFAULT_BANDPLAN)


def _source(vclock, emitters: str) -> SyntheticSource:
    scene = Scene.from_yaml(
        f"noise_floor: {{default_db: -60}}\ndc_db: -40\nemitters:\n{emitters}", clock=vclock
    )
    src = SyntheticSource(scene, vclock, quantize=False, simulate_settle=False)
    src.open()
    src.tune(CENTER, 30.0)
    return src


def _tone_stats(audio: np.ndarray, rate: int, f: float) -> tuple[float, float]:
    """(frequency of the strongest bin above 100 Hz, dB of power near f versus the rest)."""
    audio = audio[rate // 10 :]  # skip filter start-up
    spec = np.abs(np.fft.rfft(audio * np.hanning(len(audio)))) ** 2
    freqs = np.fft.rfftfreq(len(audio), 1 / rate)
    band = freqs > 100
    peak = float(freqs[band][np.argmax(spec[band])])
    near = band & (np.abs(freqs - f) < 30)
    rest = band & ~near & ~(np.abs(freqs - 2 * f) < 30) & ~(np.abs(freqs - 3 * f) < 30)
    return peak, float(10 * np.log10(spec[near].sum() / spec[rest].sum()))


def _power_at(audio: np.ndarray, rate: int, f: float) -> float:
    audio = audio[rate // 10 :]
    spec = np.abs(np.fft.rfft(audio * np.hanning(len(audio)))) ** 2
    freqs = np.fft.rfftfreq(len(audio), 1 / rate)
    return float(10 * np.log10(spec[np.abs(freqs - f) < 30].sum() + 1e-30))


def test_nfm_tone_demodulates_to_the_tone(plan, vclock):
    src = _source(vclock, f"  - {{kind: nfm_voice, hz: {CH1}, level_db: -20, tone_hz: 1000}}\n")
    ch = Channelizer(IQ_RATE, CENTER, [plan.channel_for(CH1)])
    audio = ch.push(src.read(IQ_RATE))[CH1]
    assert audio.dtype == np.float32
    peak, snr = _tone_stats(audio, AUDIO_RATE, 1000.0)
    assert peak == pytest.approx(1000.0, abs=10.0)
    assert snr >= 20.0


def test_am_tone_demodulates_to_the_tone(plan, vclock):
    hz = 121_500_000
    scene = Scene.from_yaml(
        "noise_floor: {default_db: -60}\ndc_db: -40\nemitters:\n"
        f"  - {{kind: am_voice, hz: {hz}, level_db: -20, tone_hz: 600}}\n",
        clock=vclock,
    )
    src = SyntheticSource(scene, vclock, quantize=False, simulate_settle=False)
    src.open()
    src.tune(121_206_250, 30.0)
    ch = Channelizer(IQ_RATE, 121_206_250, [plan.channel_for(hz)])
    audio = ch.push(src.read(IQ_RATE))[hz]
    peak, snr = _tone_stats(audio, AUDIO_RATE, 600.0)
    assert peak == pytest.approx(600.0, abs=10.0)
    assert snr >= 20.0


def test_audio_rate_matches_the_voice_gate(plan, vclock):
    """Regression: v1 fed 48 kHz audio to a detector that assumed 16 kHz."""
    ch = Channelizer(IQ_RATE, CENTER, [plan.channel_for(CH1)])
    gate = AlwaysNoiseGate()
    assert ch.audio_rate == AUDIO_RATE == gate.rate
    src = _source(vclock, f"  - {{kind: nfm_voice, hz: {CH1}, level_db: -20, tone_hz: 1000}}\n")
    audio = ch.push(src.read(IQ_RATE // 2))[CH1]
    assert len(audio) == pytest.approx(AUDIO_RATE // 2, abs=2)


def test_two_channels_have_no_crosstalk(plan, vclock):
    src = _source(
        vclock,
        f"  - {{kind: nfm_voice, hz: {CH1}, level_db: -20, tone_hz: 1000}}\n"
        f"  - {{kind: nfm_voice, hz: {CH2}, level_db: -20, tone_hz: 700}}\n",
    )
    ch = Channelizer(IQ_RATE, CENTER, [plan.channel_for(CH1), plan.channel_for(CH2)])
    out = ch.push(src.read(IQ_RATE))
    a, b = out[CH1], out[CH2]
    assert _power_at(a, AUDIO_RATE, 700) - _power_at(a, AUDIO_RATE, 1000) < -30
    assert _power_at(b, AUDIO_RATE, 1000) - _power_at(b, AUDIO_RATE, 700) < -30


def test_small_blocks_give_the_same_audio_as_one_block(plan, vclock):
    src = _source(vclock, f"  - {{kind: nfm_voice, hz: {CH1}, level_db: -20}}\n")
    iq = src.read(240_000)
    one = Channelizer(IQ_RATE, CENTER, [plan.channel_for(CH1)]).push(iq)[CH1]
    ch = Channelizer(IQ_RATE, CENTER, [plan.channel_for(CH1)])
    sizes = [24_000 + d for d in (7, -7, 13, -13, 1, -1, 0, 0, 3, -3)]
    parts, pos = [], 0
    for s in sizes:
        parts.append(ch.push(iq[pos : pos + s])[CH1])
        pos += s
    many = np.concatenate(parts)
    assert len(many) == len(one)
    np.testing.assert_allclose(many, one, atol=1e-3)


def test_rf_power_drops_when_the_carrier_stops(plan, vclock):
    src = _source(
        vclock, f"  - {{kind: nfm_voice, hz: {CH1}, level_db: -30, bursts: [[0, 0.2]]}}\n"
    )
    ch = Channelizer(IQ_RATE, CENTER, [plan.channel_for(CH1)])
    ch.push(src.read(240_000))  # 0.0 to 0.1 s, burst on
    on = ch.rf_power_db()[CH1]
    ch.push(src.read(240_000))
    ch.push(src.read(240_000))  # 0.2 to 0.3 s, burst off
    off = ch.rf_power_db()[CH1]
    assert on - off >= 15.0


def test_streaming_decimator_matches_one_shot():
    rng = np.random.default_rng(3)
    x = rng.normal(size=10_007).astype(np.float32)
    h = np.hanning(31).astype(np.float32)
    h /= h.sum()
    ref = StreamingDecimator(h, 5).push(x)
    d = StreamingDecimator(h, 5)
    got = np.concatenate([d.push(x[:1234]), d.push(x[1234:5000]), d.push(x[5000:])])
    np.testing.assert_allclose(got, ref, atol=1e-6)
    full = np.convolve(x, h)[: len(x)][::5]
    np.testing.assert_allclose(ref, full[: len(ref)], atol=1e-5)


def test_one_shot_demod_functions_keep_the_rate():
    rate = 48_000
    t = np.arange(rate) / rate
    fm = np.exp(1j * 2 * np.pi * 2_500 * (-np.cos(2 * np.pi * 1_000 * t) / (2 * np.pi * 1_000)))
    audio = nfm_demod(fm.astype(np.complex64), rate, deviation_hz=2_500)
    assert len(audio) == rate
    assert np.max(np.abs(audio[1000:])) == pytest.approx(1.0, abs=0.25)
    am = (1 + 0.5 * np.sin(2 * np.pi * 600 * t)).astype(np.complex64)
    assert len(am_demod(am, rate)) == rate
