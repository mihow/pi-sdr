"""Scene runs: the real scheduler, detector, tracker and store against a scripted scene."""

from __future__ import annotations

from pathlib import Path

import pytest

from sdrscan.bandplan import BandPlan
from sdrscan.config import DEFAULT_BANDPLAN, Config, DutyConfig
from sdrscan.iq.scene import Scene
from sdrscan.iq.synthetic import SyntheticSource
from sdrscan.runner import Runner
from sdrscan.scheduler import DWELL
from sdrscan.store import Store

SCENES = Path(__file__).parent / "scenes"
END_MARGIN_S = 120  # bursts still running at the end are cut short; skip them


def _run(tmp_path: Path, vclock, hours: float, scene_text: str | None = None):
    plan = BandPlan.load(DEFAULT_BANDPLAN)
    text = scene_text or (SCENES / "week_basic.yaml").read_text()
    scene = Scene.from_yaml(text, clock=vclock)
    source = SyntheticSource(scene, vclock)
    store = Store.open(tmp_path / "scan.sqlite", vclock)
    store.migrate()
    # Dwell readings every 0.5 s instead of 0.1 s keep a simulated day to seconds; the
    # tracker's 2 s hang still spans several readings.
    cfg = Config(duty=DutyConfig(dwell_block_s=0.5))
    runner = Runner(cfg=cfg, plan=plan, source=source, clock=vclock, store=store, spectrum="psd")
    end = vclock.now() + hours * 3600
    runner.run(until=end)
    runner.shutdown()
    return scene, store, runner, end


def _check_bursts(scene: Scene, store: Store, end: float) -> None:
    txs = store.transmissions()
    bursts = [
        b for b in scene.scripted_bursts(until_s=end - scene.t0) if b.t_end < end - END_MARGIN_S
    ]
    assert bursts
    burst_hz = {b.hz for b in scene.scripted_bursts(until_s=end - scene.t0)}
    for b in bursts:
        matches = [
            t for t in txs if t["hz"] == b.hz and t["t_start"] < b.t_end and t["t_end"] > b.t_start
        ]
        assert len(matches) == 1, (b, matches)
        tx = matches[0]
        assert tx["t_end"] <= b.t_end + 3
        assert tx["dur_ms"] / 1000 <= (b.t_end - b.t_start) + 3
    for t in txs:
        assert t["hz"] in burst_hz, t  # carriers, NOAA, broadcast and the spur never appear
        assert any(
            b.hz == t["hz"] and t["t_start"] < b.t_end + 3 and t["t_end"] > b.t_start - 3
            for b in scene.scripted_bursts(until_s=end - scene.t0)
        ), t


def test_six_hour_scene_finds_every_burst_and_no_carriers(tmp_path, vclock):
    scene, store, runner, end = _run(tmp_path, vclock, hours=6)
    _check_bursts(scene, store, end)
    rep = runner.scheduler.budget_report()
    assert rep["share"].get(DWELL, 0.0) <= 0.40 + 1e-3
    assert rep["tier_a_revisit_p95_s"] <= 20.0
    channels = {c["hz"]: c for c in store.channels()}
    assert channels[151_100_000]["always_on"] == 1
    assert channels[162_550_000]["always_on"] == 1
    cov = store.db.execute(
        "SELECT MIN(coverage_pct) FROM band_hour WHERE coverage_pct IS NOT NULL"
    ).fetchone()[0]
    assert cov is not None and cov >= 80
    counts = store.counts(end - 6 * 3600, end)
    n_tx = len(store.transmissions())
    assert counts["survey_hops"] > 1000 and counts["peaks"] > counts["survey_hops"] * 0.1
    assert counts["opened"] >= n_tx and counts["dwell_s"] > 0


@pytest.mark.slow
def test_week_scene_finds_every_burst_and_marks_the_spur(tmp_path, vclock):
    scene, store, runner, end = _run(tmp_path, vclock, hours=7 * 24)
    _check_bursts(scene, store, end)
    assert runner.spurs.marked()["offset_hz"], "the fixed-offset spur should be mapped"
    assert any(c["spur"] for c in store.channels())


def test_source_failure_backs_off_and_recovers(tmp_path, vclock):
    text = (SCENES / "week_basic.yaml").read_text() + "fail_at: [{t_s: 600, recover_s: 40}]\n"
    scene, store, runner, end = _run(tmp_path, vclock, hours=0.5, scene_text=text)
    assert runner.status["errors"] >= 1
    assert runner.status["mode"] == "normal"
    assert runner.source.health()["reopen_count"] >= 1
    assert store.transmissions(t0=scene.t0 + 700), "scanning resumed after the failure"


def test_iq_mode_runs_the_channelizer_during_dwells(tmp_path, vclock):
    """A short run on rendered IQ (not the fast PSD path) exercises demod in dwells."""
    plan = BandPlan.load(DEFAULT_BANDPLAN)
    scene = Scene.from_yaml(
        "noise_floor: {default_db: -50}\n"
        "emitters:\n"
        "  - {kind: nfm_voice, hz: 462_562_500, level_db: -40, bursts: [[2, 14]]}\n",
        clock=vclock,
    )
    store = Store.open(tmp_path / "scan.sqlite", vclock)
    store.migrate()
    runner = Runner(
        cfg=Config(), plan=plan, source=SyntheticSource(scene, vclock), clock=vclock, store=store
    )
    runner.run(until=vclock.now() + 25)
    runner.shutdown()
    assert runner.status["dwell_audio_s"] > 1.0
    txs = store.transmissions()
    assert [t["hz"] for t in txs] == [462_562_500]


def test_spur_and_always_on_marks_survive_a_restart(tmp_path, vclock):
    """A restarted runner must not record a known spur or carrier as new transmissions for
    the day (spur) or hour (carrier) it would take to learn them again."""
    from sdrscan.detect import Peak

    plan = BandPlan.load(DEFAULT_BANDPLAN)
    source = SyntheticSource(Scene.from_yaml("emitters: []\n", clock=vclock), vclock)
    store = Store.open(tmp_path / "scan.sqlite", vclock)
    store.migrate()
    cfg = Config()
    a = Runner(cfg=cfg, plan=plan, source=source, clock=vclock, store=store, spectrum="psd")
    a.spurs.seed(abs_hz=[146_400_000], offset_hz=[312_000])  # as if learned over a day
    carrier = plan.channel_for(162_550_000).hz
    store.mark_channel(carrier, band=plan.band_for(carrier).id, always_on=True)
    a.shutdown()
    b = Runner(cfg=cfg, plan=plan, source=source, clock=vclock, store=store, spectrum="psd")
    assert b.spurs.is_spur(146_400_000)
    assert b.spurs.is_spur(150_312_000, center_hz=150_000_000)
    assert b.tracker.is_always_on(carrier)
    t = vclock.now()
    ev = b.tracker.update([Peak(float(carrier), 6_000.0, -30.0, 25.0)], t, "survey")
    assert not [e for e in ev if e.kind == "opened"] and not b.tracker.tracks()


class _SlowSource(SyntheticSource):
    """A source whose every survey read costs extra time, like a busy Raspberry Pi 5."""

    def __init__(self, *args, extra_s: float, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.extra_s = extra_s

    def read_psd(self, nfft: int, navg: int):
        out = super().read_psd(nfft, navg)
        self.clock.sleep(self.extra_s)
        return out


def test_slow_hops_still_leave_time_to_dwell(tmp_path, vclock):
    """When a voice-band sweep alone fills the revisit target, dwells keep a minimum share.

    On the Pi a hop takes about 0.24 s, so the 54 tier A hops need 13 s of the 13.5 s plan.
    The sweep stretches instead of starving every dwell: voice is then never recorded.
    """
    plan = BandPlan.load(DEFAULT_BANDPLAN)
    scene = Scene.from_yaml((SCENES / "week_basic.yaml").read_text(), clock=vclock)
    store = Store.open(tmp_path / "scan.sqlite", vclock)
    store.migrate()
    cfg = Config(duty=DutyConfig(dwell_block_s=0.5))
    source = _SlowSource(scene, vclock, extra_s=0.13)
    runner = Runner(cfg=cfg, plan=plan, source=source, clock=vclock, store=store, spectrum="psd")
    runner.run(until=vclock.now() + 20 * 60)
    runner.shutdown()
    rep = runner.scheduler.budget_report()
    assert rep["hop_s"] > 0.22
    assert rep["share"].get(DWELL, 0.0) >= cfg.duty.dwell_min_share * 0.8
    assert any(t["source"] == "dwell" for t in store.transmissions())
    assert rep["tier_a_revisit_p95_s"] <= 22.0
