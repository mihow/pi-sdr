from __future__ import annotations

import pytest

from sdrscan.bandplan import BandPlan, Channel
from sdrscan.config import DEFAULT_BANDPLAN, DutyConfig
from sdrscan.lease import DongleLease
from sdrscan.scheduler import CALIBRATE, DWELL, IDLE, SURVEY, Job, Scheduler
from sdrscan.tracker import Track, TrackState

SETTLE = 0.03


@pytest.fixture(scope="module")
def plan() -> BandPlan:
    return BandPlan.load(DEFAULT_BANDPLAN)


class FakeTracks:
    """Stands in for Tracker.active(): a fixed set of persistent voice tracks."""

    def __init__(self, plan: BandPlan, hzs: list[int], unkeyed: list[int] = ()) -> None:
        self._tracks = [
            Track(
                channel=plan.channel_for(h),
                first_t=0,
                last_t=0,
                state=TrackState.ACTIVE,
                keyed=h not in unkeyed,
            )
            for h in [*hzs, *unkeyed]
        ]

    def active(self, policy: str = "dwell", include_candidates: bool = False) -> list[Track]:
        return list(self._tracks)


def drive(sched: Scheduler, clock, seconds: float, tracks=None, mode="normal") -> list[tuple]:
    """Run the scheduler alone: each job takes its duration (plus settle for tuned jobs)."""
    log = []
    end = clock.now() + seconds
    while clock.now() < end:
        job = sched.next_job(clock.now(), tracks, mode)
        log.append((clock.now(), job))
        clock.advance(job.duration_s + (SETTLE if job.kind != IDLE else 0.0))
    return log


def test_quiet_day_is_almost_all_survey(plan, vclock):
    sched = Scheduler(plan, DutyConfig(), vclock)
    drive(sched, vclock, 24 * 3600)
    rep = sched.budget_report()
    assert rep["share"][SURVEY] >= 0.95
    assert rep["share"].get(DWELL, 0.0) == 0.0
    assert 0 < rep["share"].get(CALIBRATE, 0.0) <= 0.05
    assert rep["tier_a_revisit_p95_s"] <= 20.0
    assert 60 <= rep["tier_b_pass_s"] <= 600


def test_persistent_voice_keeps_dwell_under_budget_and_tier_a_fresh(plan, vclock):
    tracks = FakeTracks(plan, [462_562_500, 462_587_500, 462_612_500, 462_637_500, 462_662_500])
    sched = Scheduler(plan, DutyConfig(), vclock)
    drive(sched, vclock, 6 * 3600, tracks=tracks)
    rep = sched.budget_report()
    assert 0.20 <= rep["share"][DWELL] <= 0.40
    assert rep["share"][SURVEY] >= 0.55
    assert rep["tier_a_revisit_p95_s"] <= 20.0


def test_dwell_session_ends_at_sixty_seconds_then_cools_down(plan, vclock):
    tracks = FakeTracks(plan, [462_562_500])
    cfg = DutyConfig()
    sched = Scheduler(plan, cfg, vclock)
    log = drive(sched, vclock, 2 * 3600, tracks=tracks)
    dwells = [(t, j) for t, j in log if j.kind == DWELL]
    assert dwells and len({j.center_hz for _, j in dwells}) == 1
    sessions: dict[int, list[tuple[float, Job]]] = {}
    for t, j in dwells:
        sessions.setdefault(j.session, []).append((t, j))
    ordered = [sessions[k] for k in sorted(sessions)]
    assert len(ordered) >= 3
    for s in ordered[:-1]:  # the last session may be cut off by the end of the run
        assert sum(j.duration_s for _, j in s) == pytest.approx(cfg.dwell_max_s)
    for a, b in zip(ordered, ordered[1:], strict=False):
        end_a = a[-1][0] + a[-1][1].duration_s + SETTLE
        assert b[0][0] - end_a >= cfg.dwell_cooldown_s - 1e-6
    assert all(j.channels and j.channels[0].hz == 462_562_500 for _, j in dwells)


def test_dwell_goes_to_key_ups_not_to_hum_that_never_keyed_up(plan, vclock):
    """Hum near the threshold opens many tracks in one window. A window with more such
    tracks must still lose to a window with one real key-up, and hum alone never dwells."""
    hum = [31_020_000, 31_040_000, 31_060_000, 31_080_000]
    sched = Scheduler(plan, DutyConfig(), vclock)
    log = drive(sched, vclock, 1800, tracks=FakeTracks(plan, [456_337_500], unkeyed=hum))
    dwells = [j for _, j in log if j.kind == DWELL]
    assert dwells
    assert all(any(c.hz == 456_337_500 for c in j.channels) for j in dwells)

    sched = Scheduler(plan, DutyConfig(), vclock)
    log = drive(sched, vclock, 1800, tracks=FakeTracks(plan, [], unkeyed=hum))
    assert not [j for _, j in log if j.kind == DWELL]


def test_dwell_window_is_a_survey_hop(plan, vclock):
    tracks = FakeTracks(plan, [462_562_500, 462_637_500])
    sched = Scheduler(plan, DutyConfig(), vclock)
    log = drive(sched, vclock, 600, tracks=tracks)
    centers = {j.center_hz for _, j in log if j.kind == DWELL}
    assert centers and centers <= set(plan.hops("A", sched.usable_hz))


def test_critical_mode_is_tier_a_only_at_one_hop_per_second(plan, vclock):
    tracks = FakeTracks(plan, [462_562_500])
    sched = Scheduler(plan, DutyConfig(), vclock)
    log = drive(sched, vclock, 600, tracks=tracks, mode="critical")
    tier_a = set(plan.hops("A", sched.usable_hz))
    surveys = [(t, j) for t, j in log if j.kind == SURVEY]
    assert {j.kind for _, j in log} <= {SURVEY, IDLE}
    assert all(j.center_hz in tier_a for _, j in surveys)
    starts = [t for t, _ in surveys]
    assert all(b - a >= 1.0 - 1e-6 for a, b in zip(starts, starts[1:], strict=False))
    assert len(surveys) >= 590


def test_very_hot_mode_never_dwells(plan, vclock):
    tracks = FakeTracks(plan, [462_562_500])
    sched = Scheduler(plan, DutyConfig(), vclock)
    log = drive(sched, vclock, 600, tracks=tracks, mode="very_hot")
    assert DWELL not in {j.kind for _, j in log}


def test_hot_mode_lowers_dwell_share(plan, vclock):
    tracks = FakeTracks(plan, [462_562_500, 462_587_500, 462_612_500])
    sched = Scheduler(plan, DutyConfig(), vclock)
    drive(sched, vclock, 3 * 3600, tracks=tracks, mode="hot")
    assert sched.budget_report()["share"][DWELL] <= 0.25


def test_coverage_is_reported_per_hour(plan, vclock):
    sched = Scheduler(plan, DutyConfig(), vclock)
    drive(sched, vclock, 2 * 3600)
    hour = int(vclock.now() // 3600 * 3600) - 3600
    assert sched.coverage(hour) == pytest.approx(100.0, abs=10.0)


def test_survey_jobs_carry_the_band_gain(plan, vclock):
    sched = Scheduler(plan, DutyConfig(), vclock)
    job = sched.next_job(vclock.now(), None, "normal")
    assert job.gain_db == plan.band_for(job.center_hz).gain_db


class FakeSource:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")


class FakeJob:
    name = "fake"
    max_runtime_s = 1.0

    def argv(self) -> list[str]:
        return ["true"]

    def parse(self, line: str) -> list[dict]:
        return []


def test_lease_closes_source_and_reopens_even_on_error(vclock):
    src = FakeSource()
    lease = DongleLease(src, vclock)
    t0 = vclock.now()
    with lease.lease(FakeJob()):
        assert src.calls == ["close"]
        assert lease.active is not None
    assert src.calls == ["close", "open"]
    assert vclock.now() - t0 >= 0.3 - 1e-6
    with pytest.raises(RuntimeError):
        with lease.lease(FakeJob()):
            raise RuntimeError("decoder crashed")
    assert src.calls == ["close", "open", "close", "open"]
    assert lease.active is None


def test_channel_tuple_on_job_is_hashable(plan):
    ch: Channel = plan.channel_for(462_562_500)
    job = Job(kind=DWELL, center_hz=462_506_250, duration_s=1.0, channels=(ch,))
    assert hash(job)
