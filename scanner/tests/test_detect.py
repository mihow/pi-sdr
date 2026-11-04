from __future__ import annotations

import numpy as np
import pytest

from sdrscan import IQ_RATE
from sdrscan.bandplan import BandPlan
from sdrscan.config import DEFAULT_BANDPLAN, DetectConfig
from sdrscan.detect import HopResult, Peak, SpurMap, analyze
from sdrscan.dsp.spectrum import welch_psd
from sdrscan.iq.scene import Scene
from sdrscan.iq.synthetic import SyntheticSource

NFFT = 4096
N = NFFT * 50


@pytest.fixture(scope="module")
def plan() -> BandPlan:
    return BandPlan.load(DEFAULT_BANDPLAN)


def _noise(level_db: float, n: int = N, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    s = np.sqrt(10 ** (level_db / 10) / 2)
    return (rng.normal(0, s, n) + 1j * rng.normal(0, s, n)).astype(np.complex64)


def _tone(off_hz: float, bin_snr_db: float, noise_db: float, n: int = N) -> np.ndarray:
    """A tone whose peak PSD bin sits ``bin_snr_db`` above a noise floor of ``noise_db``."""
    p = 10 ** ((noise_db + bin_snr_db) / 10) / (2 * NFFT / 3)
    t = np.arange(n) / IQ_RATE
    return (np.sqrt(p) * np.exp(2j * np.pi * off_hz * t)).astype(np.complex64)


def _bin_centered(off_hz: float) -> float:
    b = IQ_RATE / NFFT
    return round(off_hz / b) * b


def test_welch_scale_reads_noise_variance():
    psd = welch_psd(_noise(-37.0))
    assert np.median(psd) == pytest.approx(-37.0, abs=0.5)


def test_pure_noise_gives_no_peaks_and_floor_at_generated_level(plan):
    r = analyze(_noise(-42.0), 146_002_500, IQ_RATE, plan)
    assert isinstance(r, HopResult)
    assert r.peaks == []
    assert r.floor_db == pytest.approx(-42.0, abs=1.0)
    assert 0 < r.mad_db < 2


@pytest.mark.parametrize("noise_db", [20.0, -60.0])
def test_snr_detection_is_relative_to_the_floor_not_absolute(plan, noise_db):
    """Regression: v1 compared an absolute squelch with positive dB values and found nothing."""
    off = _bin_centered(300_000)
    iq = _noise(noise_db) + _tone(off, 20.0, noise_db)
    r = analyze(iq, 146_002_500, IQ_RATE, plan)
    assert len(r.peaks) == 1
    p = r.peaks[0]
    assert p.hz == pytest.approx(146_002_500 + off, abs=600)
    assert p.snr_db == pytest.approx(20.0, abs=1.5)


def test_dc_spike_at_tuner_center_is_not_a_peak(plan):
    iq = _noise(-50.0) + np.complex64(0.05)
    r = analyze(iq, 146_002_500, IQ_RATE, plan)
    assert r.peaks == []


def test_two_carriers_fifty_khz_apart_are_two_peaks(plan):
    a, b = _bin_centered(200_000), _bin_centered(250_000)
    iq = _noise(-50.0) + _tone(a, 25.0, -50.0) + _tone(b, 25.0, -50.0)
    r = analyze(iq, 146_002_500, IQ_RATE, plan)
    assert len(r.peaks) == 2


def test_one_wide_nfm_signal_is_one_peak(plan, vclock):
    scene = Scene.from_yaml(
        "noise_floor: {default_db: -55}\n"
        "dc_db: -80\n"
        "emitters:\n"
        "  - {kind: nfm_voice, hz: 462_562_500, level_db: -25, deviation_hz: 5000}\n",
        clock=vclock,
    )
    src = SyntheticSource(scene, vclock, quantize=False)
    src.open()
    src.tune(462_006_250, 30.0)
    r = analyze(src.read(N), 462_006_250, IQ_RATE, plan)
    assert len(r.peaks) == 1
    assert r.peaks[0].width_hz >= 8_000
    assert plan.channel_for(r.peaks[0].hz).hz == 462_562_500


def test_fm_broadcast_hop_uses_fifteen_db_offset(plan):
    off = _bin_centered(300_000)
    iq = _noise(-50.0) + _tone(off, 12.0, -50.0)
    fm = analyze(iq, 98_000_000, IQ_RATE, plan)
    assert fm.threshold_db == pytest.approx(15.0)
    assert fm.peaks == []
    vhf = analyze(iq, 146_002_500, IQ_RATE, plan)
    assert vhf.threshold_db == pytest.approx(10.0)
    assert len(vhf.peaks) == 1


def test_band_threshold_overrides_config(plan):
    cfg = DetectConfig(threshold_db=13.0)
    r = analyze(_noise(-50.0), 146_002_500, IQ_RATE, plan, cfg=cfg)
    assert r.threshold_db == pytest.approx(13.0)


def test_occupancy_counts_hot_share_per_megahertz(plan):
    off = _bin_centered(300_000)
    iq = _noise(-50.0) + _tone(off, 25.0, -50.0)
    r = analyze(iq, 146_002_500, IQ_RATE, plan)
    mhz = int((146_002_500 + off) // 1_000_000)
    assert r.occupancy[mhz] > 0
    assert all(0.0 <= v <= 1.0 for v in r.occupancy.values())
    assert r.lo_hz < 146_002_500 + off < r.hi_hz


def test_clipped_scene_reports_clip_fraction(plan, vclock):
    scene = Scene.from_yaml("clip: true\n", clock=vclock)
    src = SyntheticSource(scene, vclock)
    src.open()
    src.tune(146_002_500, 30.0)
    r = analyze(src.read(N), 146_002_500, IQ_RATE, plan)
    assert r.clip_frac > 0.01


def _hop(center: int, peaks: list[Peak]) -> HopResult:
    return HopResult(
        peaks=peaks,
        floor_db=-50.0,
        mad_db=0.5,
        clip_frac=0.0,
        occupancy={},
        center_hz=center,
        lo_hz=center - 960_000,
        hi_hz=center + 960_000,
        threshold_db=10.0,
    )


def test_spur_at_fixed_tuner_offset_is_marked_after_a_day():
    spurs = SpurMap()
    centers = [146_000_000 + k * 1_920_000 for k in range(10)]
    t = 0.0
    while t <= 24 * 3600 + 60:
        for c in centers:
            spurs.update(_hop(c, [Peak(c + 312_000.0, 1_200.0, -30.0, 20.0)]), t)
        t += 60.0
    assert spurs.is_spur(centers[3] + 312_100, center_hz=centers[3])
    assert not spurs.is_spur(centers[3] + 500_000, center_hz=centers[3])


def test_spur_at_fixed_frequency_is_marked_but_not_before_a_day():
    spurs = SpurMap()
    t = 0.0
    while t <= 23 * 3600:
        spurs.update(_hop(146_000_000, [Peak(146_400_000.0, 1_200.0, -30.0, 20.0)]), t)
        spurs.update(_hop(150_000_000, []), t)
        t += 60.0
    assert not spurs.is_spur(146_400_000, center_hz=146_000_000)
    while t <= 25 * 3600:
        spurs.update(_hop(146_000_000, [Peak(146_400_000.0, 1_200.0, -30.0, 20.0)]), t)
        t += 60.0
    assert spurs.is_spur(146_400_000)


def test_intermittent_or_wide_signals_are_not_spurs():
    spurs = SpurMap()
    t = 0.0
    k = 0
    while t <= 25 * 3600:
        peaks = [Peak(146_700_000.0, 16_000.0, -30.0, 20.0)]
        if k % 2:
            peaks.append(Peak(146_300_000.0, 1_200.0, -30.0, 20.0))
        spurs.update(_hop(146_000_000, peaks), t)
        t += 60.0
        k += 1
    assert not spurs.is_spur(146_700_000)
    assert not spurs.is_spur(146_300_000)
