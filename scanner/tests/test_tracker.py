from __future__ import annotations

import pytest

from sdrscan.bandplan import BandPlan
from sdrscan.config import DEFAULT_BANDPLAN
from sdrscan.detect import Peak
from sdrscan.tracker import Tracker, TrackState

GMRS = 462_562_500
SPAN = (461_600_000, 463_500_000)


@pytest.fixture(scope="module")
def plan() -> BandPlan:
    return BandPlan.load(DEFAULT_BANDPLAN)


def pk(hz: float, snr: float = 15.0, width: float = 12_000.0) -> Peak:
    return Peak(hz=hz, width_hz=width, level_db=-50.0 + snr, snr_db=snr)


def survey(tr: Tracker, t: float, peaks: list[Peak]):
    return tr.update(peaks, t, "survey", span=SPAN)


def test_seen_once_is_candidate_then_two_of_three_opens(plan, vclock):
    tr = Tracker(plan, vclock)
    assert survey(tr, 0.0, [pk(GMRS + 900)]) == []
    assert tr.state(GMRS) is TrackState.CANDIDATE
    assert survey(tr, 15.0, []) == []
    ev = survey(tr, 30.0, [pk(GMRS - 400)])
    assert [e.kind for e in ev] == ["opened"]
    assert ev[0].hz == GMRS and tr.state(GMRS) is TrackState.ACTIVE


def test_candidate_dropped_after_two_misses(plan, vclock):
    tr = Tracker(plan, vclock)
    survey(tr, 0.0, [pk(GMRS)])
    survey(tr, 15.0, [])
    assert survey(tr, 30.0, []) == []
    assert tr.state(GMRS) is TrackState.IDLE


def test_single_strong_dwell_reading_opens_immediately(plan, vclock):
    tr = Tracker(plan, vclock)
    ev = tr.update([pk(GMRS, snr=25.0)], 0.0, "dwell", span=SPAN)
    assert [e.kind for e in ev] == ["opened"]


def test_weak_dwell_reading_does_not_open_at_once(plan, vclock):
    tr = Tracker(plan, vclock)
    assert tr.update([pk(GMRS, snr=12.0)], 0.0, "dwell", span=SPAN) == []
    assert tr.state(GMRS) is TrackState.CANDIDATE


def test_dwell_track_closes_after_hang_with_burst_duration(plan, vclock):
    tr = Tracker(plan, vclock)
    events = []
    t = 100.0
    while t < 130.0:  # a 30 s burst read at 10 Hz
        events += tr.update([pk(GMRS, snr=24.0)], t, "dwell", span=SPAN)
        t += 0.1
    while t < 133.0:
        events += tr.update([], t, "dwell", span=SPAN)
        t += 0.1
    closed = [e for e in events if e.kind == "closed"]
    assert len(closed) == 1
    tx = closed[0].tx
    assert tx.hz == GMRS and isinstance(tx.hz, int)
    assert tx.dur_ms / 1000 == pytest.approx(30.0, abs=2.0)
    assert tx.t_start == pytest.approx(100.0)
    assert tx.source == "dwell"
    assert tx.peak_snr_db == pytest.approx(24.0)
    assert tx.floor_db == pytest.approx(-50.0)
    assert closed[0].t == pytest.approx(130.0 + 2.0, abs=0.15)


def test_survey_miss_closes_active_track_once(plan, vclock):
    tr = Tracker(plan, vclock)
    survey(tr, 0.0, [pk(GMRS)])
    survey(tr, 15.0, [pk(GMRS)])
    survey(tr, 30.0, [pk(GMRS)])
    ev = survey(tr, 45.0, [])
    assert [e.kind for e in ev] == ["closed"]
    assert ev[0].tx.dur_ms == 30_000
    assert ev[0].tx.source == "survey"
    assert survey(tr, 60.0, []) == []


def test_visits_outside_span_are_not_misses(plan, vclock):
    tr = Tracker(plan, vclock)
    survey(tr, 0.0, [pk(GMRS)])
    survey(tr, 15.0, [pk(GMRS)])
    assert tr.update([], 20.0, "survey", span=(146_000_000, 148_000_000)) == []
    assert tr.state(GMRS) is TrackState.ACTIVE


def test_always_on_channel_is_flagged_and_never_emits(plan, vclock):
    tr = Tracker(plan, vclock)
    events = []
    t = 0.0
    while t <= 3700.0:
        events += survey(tr, t, [pk(GMRS, snr=30.0, width=1_000.0)])
        t += 15.0
    kinds = [e.kind for e in events]
    assert kinds.count("always_on") == 1
    assert "closed" not in kinds
    assert tr.is_always_on(GMRS)
    events = survey(tr, t, []) + tr.close_all(t + 1)
    assert [e for e in events if e.kind == "closed"] == []


def test_bursty_channel_is_not_always_on(plan, vclock):
    tr = Tracker(plan, vclock)
    t = 0.0
    k = 0
    while t <= 2 * 3600:
        survey(tr, t, [pk(GMRS)] if k % 4 < 2 else [])
        t += 15.0
        k += 1
    assert not tr.is_always_on(GMRS)


def test_two_peaks_on_one_channel_are_one_track(plan, vclock):
    tr = Tracker(plan, vclock)
    ev = tr.update([pk(GMRS - 2_000, 22.0), pk(GMRS + 2_500, 26.0)], 0.0, "dwell", span=SPAN)
    assert [e.kind for e in ev] == ["opened"]
    assert [t.hz for t in tr.tracks()] == [GMRS]


def test_occupancy_bands_are_not_tracked(plan, vclock):
    tr = Tracker(plan, vclock)
    span = (97_000_000, 99_000_000)
    for t in (0.0, 15.0, 30.0):
        assert tr.update([pk(98_100_000, 30.0)], t, "survey", span=span) == []
    assert tr.tracks() == []


def test_close_all_emits_active_but_not_candidates(plan, vclock):
    tr = Tracker(plan, vclock)
    tr.update([pk(GMRS, 25.0)], 0.0, "dwell", span=SPAN)
    tr.update([pk(462_587_500, 12.0)], 0.0, "dwell", span=SPAN)
    ev = tr.close_all(5.0)
    assert [(e.kind, e.hz) for e in ev] == [("closed", GMRS)]
    assert tr.tracks() == []


def test_unvisited_active_track_expires(plan, vclock):
    tr = Tracker(plan, vclock)
    tr.update([pk(GMRS, 25.0)], 0.0, "dwell", span=SPAN)
    assert tr.expire(60.0, max_unvisited_s=120.0) == []
    ev = tr.expire(200.0, max_unvisited_s=120.0)
    assert [e.kind for e in ev] == ["closed"]


def test_active_lists_voice_tracks(plan, vclock):
    tr = Tracker(plan, vclock)
    tr.update([pk(GMRS, 25.0)], 0.0, "dwell", span=SPAN)
    tr.update([pk(462_587_500, 12.0)], 0.0, "dwell", span=SPAN)
    assert [t.hz for t in tr.active("dwell")] == [GMRS]
    assert [t.hz for t in tr.active("dwell", include_candidates=True)] == [GMRS, 462_587_500]
