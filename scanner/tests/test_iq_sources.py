from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

from sdrscan import IQ_RATE
from sdrscan.dsp.spectrum import welch_psd
from sdrscan.iq.base import IqSource, IqSourceError
from sdrscan.iq.file import FileSource, write_cf32
from sdrscan.iq.scene import Scene
from sdrscan.iq.synthetic import SyntheticSource

ONE_CARRIER = """
noise_floor: {default_db: -50}
emitters:
  - {kind: carrier, hz: 462_562_500, level_db: -30}
"""


def _strongest_hz(iq: np.ndarray, center_hz: int, rate: int, nfft: int = 4096) -> float:
    psd = welch_psd(iq, nfft=nfft, navg=20)
    freqs = center_hz + (np.arange(nfft) - nfft // 2) * rate / nfft
    mid = nfft // 2
    psd[mid - 3 : mid + 4] = -300  # ignore the DC spike
    return float(freqs[int(np.argmax(psd))])


def test_file_source_round_trips_cf32_and_sidecar(tmp_path: Path):
    rng = np.random.default_rng(1)
    iq = (rng.normal(size=5000) + 1j * rng.normal(size=5000)).astype(np.complex64)
    path = tmp_path / "cap.cf32"
    write_cf32(path, iq, rate=IQ_RATE, center_hz=146_000_000, gain_db=29.7)
    src = FileSource(path, loop=False)
    assert isinstance(src, IqSource)
    src.open()
    src.tune(146_000_000, 29.7)
    got = np.concatenate([src.read(2000), src.read(2000), src.read(1000)])
    np.testing.assert_array_equal(got, iq)
    with pytest.raises(EOFError):
        src.read(10)
    src.close()


def test_file_source_loops_when_asked(tmp_path: Path):
    iq = np.arange(10, dtype=np.float32).astype(np.complex64)
    path = tmp_path / "cap.cf32"
    write_cf32(path, iq, rate=IQ_RATE, center_hz=1_000_000, gain_db=0.0)
    src = FileSource(path, loop=True)
    src.open()
    np.testing.assert_array_equal(
        src.read(25).real, np.r_[np.arange(10), np.arange(10), np.arange(5)]
    )


def test_file_source_refuses_wrong_tuning(tmp_path: Path):
    path = tmp_path / "cap.cf32"
    write_cf32(path, np.zeros(10, np.complex64), rate=IQ_RATE, center_hz=1_000_000, gain_db=0.0)
    src = FileSource(path)
    src.open()
    with pytest.raises(ValueError, match="center"):
        src.tune(2_000_000, 0.0)


def test_synthetic_carrier_lands_on_its_frequency(vclock):
    scene = Scene.from_yaml(ONE_CARRIER, clock=vclock)
    src = SyntheticSource(scene, vclock)
    src.open()
    src.tune(462_412_500, 30.0)
    iq = src.read(4096 * 20)
    assert iq.dtype == np.complex64
    assert abs(_strongest_hz(iq, 462_412_500, IQ_RATE) - 462_562_500) <= 1_000


def test_synthetic_read_advances_virtual_time(vclock):
    scene = Scene.from_yaml(ONE_CARRIER, clock=vclock)
    src = SyntheticSource(scene, vclock)
    src.open()
    src.tune(462_412_500, 30.0)
    t0 = vclock.now()
    src.read(240_000)
    assert vclock.now() - t0 == pytest.approx(0.1)


def test_synthetic_is_quantized_to_eight_bits(vclock):
    scene = Scene.from_yaml(ONE_CARRIER, clock=vclock)
    src = SyntheticSource(scene, vclock)
    src.open()
    src.tune(462_412_500, 30.0)
    iq = src.read(10_000)
    codes = np.round(iq.real * 127.5 + 127.5)
    np.testing.assert_allclose(iq.real * 127.5 + 127.5, codes, atol=1e-3)


def test_synthetic_has_dc_spike_at_tuner_center(vclock):
    scene = Scene.from_yaml("noise_floor: {default_db: -50}\ndc_db: -20\n", clock=vclock)
    src = SyntheticSource(scene, vclock)
    src.open()
    src.tune(150_000_000, 30.0)
    psd = welch_psd(src.read(4096 * 10), nfft=4096, navg=10)
    assert psd[2048] > np.median(psd) + 20


def test_burst_follows_virtual_time(vclock):
    scene = Scene.from_yaml(
        "noise_floor: {default_db: -50}\n"
        "emitters:\n"
        "  - {kind: nfm_voice, hz: 462_562_500, level_db: -30, bursts: [[10, 5]]}\n",
        clock=vclock,
    )
    assert not scene.emitters[0].active_at(9.9)
    assert scene.emitters[0].active_at(12.0)
    assert not scene.emitters[0].active_at(15.1)
    bursts = scene.scripted_bursts(until_s=100)
    assert [(b.hz, b.t_start - vclock.now(), b.t_end - vclock.now()) for b in bursts] == [
        (462_562_500, 10.0, 15.0)
    ]


def test_schedule_expands_to_bursts(vclock):
    scene = Scene.from_yaml(
        "emitters:\n"
        "  - kind: nfm_voice\n"
        "    hz: 146_520_000\n"
        "    level_db: -30\n"
        "    schedule: {every_s: 100, dur_s: 20, offset_s: 5, until_s: 350}\n",
        clock=vclock,
    )
    starts = [b.t_start - vclock.now() for b in scene.scripted_bursts(until_s=1000)]
    assert starts == [5.0, 105.0, 205.0, 305.0]


def test_scene_fail_at_raises_then_recovers(vclock):
    scene = Scene.from_yaml("fail_at: [{t_s: 1.0, recover_s: 5.0}]\n", clock=vclock)
    src = SyntheticSource(scene, vclock)
    src.open()
    src.tune(150_000_000, 30.0)
    src.read(240_000)  # t = 0.1
    vclock.advance(1.0)
    with pytest.raises(IqSourceError):
        src.read(240_000)
    with pytest.raises(IqSourceError):
        src.open()
    vclock.advance(5.0)
    src.open()
    src.tune(150_000_000, 30.0)
    assert len(src.read(1000)) == 1000
    assert src.health()["reopen_count"] >= 1


def test_clipped_scene_hits_the_adc_rail(vclock):
    scene = Scene.from_yaml("clip: true\n", clock=vclock)
    src = SyntheticSource(scene, vclock)
    src.open()
    src.tune(150_000_000, 30.0)
    iq = src.read(100_000)
    rail = (np.abs(iq.real) >= 0.99) | (np.abs(iq.imag) >= 0.99)
    assert rail.mean() > 0.01


def test_fast_psd_agrees_with_rendered_iq(vclock):
    scene = Scene.from_yaml(
        "noise_floor: {default_db: -50}\n"
        "emitters:\n"
        "  - {kind: carrier, hz: 150_300_000, level_db: -40}\n"
        "  - {kind: nfm_voice, hz: 150_600_000, level_db: -40}\n",
        clock=vclock,
    )
    src = SyntheticSource(scene, vclock, quantize=False)
    src.open()
    src.tune(150_000_000, 30.0)
    nfft = 4096
    slow = welch_psd(src.read(nfft * 50), nfft=nfft, navg=50)
    fast, clip = src.read_psd(nfft, navg=50)
    assert clip == 0.0
    assert np.median(fast) == pytest.approx(np.median(slow), abs=1.0)
    k_car = nfft // 2 + round(300_000 / (IQ_RATE / nfft))
    assert fast[k_car] == pytest.approx(slow[k_car], abs=3.0)
    k_nfm = nfft // 2 + round(600_000 / (IQ_RATE / nfft))
    band = slice(k_nfm - 5, k_nfm + 6)
    assert 10 * np.log10(np.mean(10 ** (fast[band] / 10))) == pytest.approx(
        10 * np.log10(np.mean(10 ** (slow[band] / 10))), abs=3.0
    )


class _FakeRtlSdr:
    instances: list[_FakeRtlSdr] = []

    def __init__(self, device_index: int = 0) -> None:
        self.sample_rate = 0
        self.center_freq = 0
        self.gain: object = None
        self.closed = False
        _FakeRtlSdr.instances.append(self)

    def read_samples(self, n: int) -> np.ndarray:
        return np.full(n, 0.1 + 0.1j)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_rtlsdr(monkeypatch):
    mod = types.ModuleType("rtlsdr")
    mod.RtlSdr = _FakeRtlSdr
    monkeypatch.setitem(sys.modules, "rtlsdr", mod)
    _FakeRtlSdr.instances.clear()
    return _FakeRtlSdr


def test_rtl_tune_rejects_automatic_gain(fake_rtlsdr, vclock):
    """Regression: v1 ran with AGC and logged its gain as 0 dB, so levels were incomparable."""
    from sdrscan.iq.rtl import RtlSource

    src = RtlSource(clock=vclock, threaded=False)
    src.open()
    with pytest.raises(ValueError, match="manual"):
        src.tune(146_000_000, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="manual"):
        src.tune(146_000_000, "auto")  # type: ignore[arg-type]
    src.tune(146_000_000, 29.7)
    assert fake_rtlsdr.instances[-1].gain == 29.7
    assert fake_rtlsdr.instances[-1].center_freq == 146_000_000
    src.close()


def test_rtl_discards_settle_samples_longer_across_input_switch(fake_rtlsdr, vclock):
    from sdrscan.iq.rtl import RtlSource, settle_s

    assert settle_s(146_000_000, 147_000_000) == pytest.approx(0.030)
    assert settle_s(146_000_000, 433_000_000) == pytest.approx(0.060)
    assert settle_s(27_000_000, 30_000_000) == pytest.approx(0.060)
    src = RtlSource(clock=vclock, threaded=False)
    src.open()
    src.tune(146_000_000, 29.7)
    assert src.health()["settle_discarded"] == int(0.060 * IQ_RATE)  # first tune: worst case
    iq = src.read(1000)
    assert iq.dtype == np.complex64 and len(iq) == 1000
    src.close()
    assert fake_rtlsdr.instances[-1].closed


def test_rtl_without_library_raises_source_error(monkeypatch, vclock):
    monkeypatch.setitem(sys.modules, "rtlsdr", None)
    from sdrscan.iq.rtl import RtlSource

    with pytest.raises(IqSourceError, match="pyrtlsdr"):
        RtlSource(clock=vclock).open()


@pytest.mark.hw
def test_rtl_real_dongle_reads_samples():
    from sdrscan.clock import RealClock
    from sdrscan.iq.rtl import RtlSource

    src = RtlSource(clock=RealClock())
    src.open()
    src.tune(100_000_000, 20.7)
    assert len(src.read(240_000)) == 240_000
    src.close()
