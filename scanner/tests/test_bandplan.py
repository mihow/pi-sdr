from __future__ import annotations

from pathlib import Path

import pytest

from sdrscan.bandplan import BandPlan, BandPlanError
from sdrscan.config import DEFAULT_BANDPLAN


@pytest.fixture(scope="module")
def plan() -> BandPlan:
    return BandPlan.load(DEFAULT_BANDPLAN)


def test_gmrs_peak_snaps_to_integer_channel(plan: BandPlan):
    ch = plan.channel_for(462_562_500 + 1_200.0)
    assert ch.hz == 462_562_500
    assert isinstance(ch.hz, int)
    assert ch.band == "gmrs_frs_462"
    assert ch.mode == "nfm"


def test_airband_guard_channel_is_am(plan: BandPlan):
    ch = plan.channel_for(121_500_000 + 3_000.0)
    assert ch.hz == 121_500_000
    assert ch.mode == "am"
    assert plan.band_for(121_500_000).id == "airband"


def test_frequency_outside_every_band_is_unplanned(plan: BandPlan):
    band = plan.band_for(10_000_000)
    assert band.id == "unplanned"
    assert band.raster_hz == 12_500
    ch = plan.channel_for(10_006_000.0)
    assert ch.hz == 10_000_000


def test_noaa_and_marine_use_wide_deviation(plan: BandPlan):
    assert plan.channel_for(162_550_000).deviation_hz == 5_000
    assert plan.channel_for(156_800_000).deviation_hz == 5_000
    assert plan.channel_for(462_562_500).deviation_hz == 2_500


def test_bands_are_sorted_and_do_not_overlap(plan: BandPlan):
    bands = plan.bands
    for a, b in zip(bands, bands[1:], strict=False):
        assert a.hi_hz <= b.lo_hz, (a.id, b.id)


def test_overlapping_bands_fail_validation(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text(
        "bands:\n"
        "  - {id: a, lo_hz: 100_000_000, hi_hz: 101_000_000}\n"
        "  - {id: b, lo_hz: 100_500_000, hi_hz: 102_000_000}\n"
    )
    with pytest.raises(BandPlanError, match="overlap"):
        BandPlan.load(p)


def test_bad_mode_fails_validation(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text("bands:\n  - {id: a, lo_hz: 1, hi_hz: 2, mode: usb}\n")
    with pytest.raises(BandPlanError, match="mode"):
        BandPlan.load(p)


def test_tier_a_hops_cover_every_tier_a_band_in_ascending_order(plan: BandPlan):
    usable = 1_920_000
    hops = plan.hops("A", usable)
    assert hops == sorted(hops)
    assert len(hops) == len(set(hops))
    tier_a = [b for b in plan.bands if b.tier == "A" and b.policy != "ignore"]
    assert tier_a
    for band in tier_a:
        for f in range(band.lo_hz, band.hi_hz, 5_000):
            assert any(abs(f - h) <= usable // 2 for h in hops), (band.id, f)
    # HF/VHF before UHF: the sweep crosses each tuner input switch once per pass.
    assert hops[0] < 250_000_000 < hops[-1]


def test_hop_centers_avoid_raster_points(plan: BandPlan):
    for h in plan.hops("A", 1_920_000):
        band = plan.band_for(h)
        ch = plan.channel_for(h)
        assert abs(ch.hz - h) >= min(band.raster_hz // 4, 2_000), (h, band.id)


def test_tier_b_hops_do_not_repeat_tier_a(plan: BandPlan):
    a = plan.hops("A", 1_920_000)
    b = plan.hops("B", 1_920_000)
    assert b and not set(a) & set(b)


def test_labels_overlay_renames_without_changing_hz(tmp_path: Path):
    labels = tmp_path / "labels.yaml"
    labels.write_text("462562500: Ranch channel 1\n")
    plan = BandPlan.load(DEFAULT_BANDPLAN, labels=labels)
    ch = plan.channel_for(462_562_900)
    assert ch.hz == 462_562_500
    assert ch.name == "Ranch channel 1"
    other = plan.channel_for(462_587_500)
    assert other.name != "Ranch channel 1"


@pytest.mark.parametrize(
    ("hz", "name"),
    [
        (462_562_500, "Ch 1"),
        (462_712_500, "Ch 7"),
        (467_562_500, "Ch 8"),
        (467_712_500, "Ch 14"),
        (462_550_000, "Ch 15"),
        (462_700_000, "Ch 21"),
        (462_725_000, "Ch 22"),
        (467_550_000, "Ch 15R input"),
        (467_725_000, "Ch 22R input"),
    ],
)
def test_gmrs_frs_channels_carry_their_standard_numbers(plan: BandPlan, hz: int, name: str):
    """A listener tunes a handheld by channel number, so GMRS/FRS keys show it."""
    assert plan.channel_for(hz).name == name


def test_channels_without_a_standard_number_show_only_mhz(plan: BandPlan):
    assert plan.channel_for(456_337_500).name == "456.3375 MHz"


def test_local_labels_override_standard_channel_names(tmp_path: Path):
    labels = tmp_path / "labels.yaml"
    labels.write_text("462550000: Valley repeater\n")
    plan = BandPlan.load(DEFAULT_BANDPLAN, labels=labels)
    assert plan.channel_for(462_550_000).name == "Valley repeater"
    assert plan.channel_for(462_700_000).name == "Ch 21"
