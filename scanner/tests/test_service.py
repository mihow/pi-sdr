"""Service-level behaviour on a virtual clock: start-up with the shipped configuration,
catch-up after downtime, and state that must survive a restart."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from sdrscan.config import load_config
from sdrscan.iq.scene import Scene
from sdrscan.iq.synthetic import SyntheticSource
from sdrscan.service import Service

from .test_clips import FakeFs

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _template_cfg(tmp_path: Path, uplink_env: str | None):
    """The shipped template, with the rig paths moved under ``tmp_path``."""
    cfg = load_config(DEPLOY / "config.template.yaml")
    env = tmp_path / "uplink.env"
    if uplink_env is not None:
        env.write_text(uplink_env)
    return dataclasses.replace(
        cfg,
        data_root=tmp_path / "data",
        stage_root=tmp_path / "stage",
        uplink=dataclasses.replace(cfg.uplink, secrets_env=env),
    )


def _service(cfg, vclock, sys_root, **kw) -> Service:
    src = SyntheticSource(Scene.from_yaml("emitters: []\n", vclock), vclock)
    return Service(cfg, vclock, src, spectrum="psd", statvfs=FakeFs(0.5), sys_root=sys_root, **kw)


@pytest.mark.parametrize(
    "uplink_env",
    [None, "", "# SDRSCAN_S3_BUCKET=<bucket>\n", "SDRSCAN_S3_BUCKET=b\n"],
    ids=["missing", "empty", "placeholder", "incomplete"],
)
def test_an_unconfigured_uplink_disables_pushes_but_the_scanner_runs(
    tmp_path, vclock, sys_root, uplink_env
):
    """A fresh install has no upload credentials yet. The scanner must still scan, write
    the local digest and serve the live page, and say plainly that the uplink is off."""
    vclock.set(vclock.now() + 3500)  # 100 s before an hour boundary, so a digest is due
    svc = _service(_template_cfg(tmp_path, uplink_env), vclock, sys_root)
    assert svc.publisher is None and svc.uplink_error
    svc.run(until=vclock.now() + 200)
    assert svc.digests >= 1
    assert (tmp_path / "data" / "www" / "digest.json.gz").exists()
    assert svc.last_digest["health"]["uplink"]["state"] == "unconfigured"
    assert "uplink_unconfigured" in svc.last_digest["health"]["flags"]
    svc.shutdown()


DAY = 86400
GMRS = 462_562_500
BURSTY = """
seed: 1
emitters:
  - {kind: nfm_voice, hz: 462_562_500, level_db: -40, bursts: [[60, 40], [400, 40]]}
"""


def _plain_cfg(tmp_path):
    from sdrscan.config import Config

    return Config(data_root=tmp_path / "data", stage_root=tmp_path / "stage")


def _scene_service(cfg, vclock, sys_root, scene: str = BURSTY) -> Service:
    src = SyntheticSource(Scene.from_yaml(scene, vclock), vclock)
    return Service(cfg, vclock, src, spectrum="psd", statvfs=FakeFs(0.5), sys_root=sys_root)


def test_a_day_the_service_was_down_at_midnight_still_reaches_week_and_all(
    tmp_path, vclock, sys_root
):
    """The day rollup used to run only when the service itself crossed midnight. A rig that
    stopped at 22:30 and came back at 01:10 lost that day from the week and all-time
    views for good."""
    from sdrscan import rollup

    day0 = vclock.now()
    vclock.set(day0 + 22 * 3600)
    cfg = _plain_cfg(tmp_path)
    svc = _scene_service(cfg, vclock, sys_root)
    svc.run(until=vclock.now() + 1800)
    svc.shutdown()
    vclock.set(day0 + DAY + 3600 + 600)  # down over midnight
    svc = _scene_service(cfg, vclock, sys_root, scene="emitters: []\n")
    now = vclock.now()
    week = rollup.query_window(svc.store, "week", now)
    every = rollup.query_window(svc.store, "all", now)
    assert GMRS in week.channels and week.channels[GMRS].tx_count >= 2
    assert GMRS in every.channels and every.channels[GMRS].tx_count >= 2
    assert int(svc.store.meta("rolled_through")) == day0 + DAY
    svc.shutdown()


def test_an_hour_cut_short_by_a_restart_is_rebuilt_from_raw_rows(tmp_path, vclock, sys_root):
    """Hours the service never finished (it stopped mid-hour) are recomputed on start."""
    from sdrscan import rollup

    day0 = vclock.now()
    vclock.set(day0 + 10 * 3600)
    cfg = _plain_cfg(tmp_path)
    svc = _scene_service(cfg, vclock, sys_root)
    svc.run(until=vclock.now() + 1200)
    svc.shutdown()
    hour = rollup.hour_of(day0 + 10 * 3600)
    import sqlite3

    db = sqlite3.connect(tmp_path / "data" / "db" / "scan.sqlite")
    with db:
        db.execute("DELETE FROM rollup_hour WHERE hour_ts = ?", (hour,))  # a lost batch
    db.close()
    vclock.set(day0 + 13 * 3600)
    svc = _scene_service(cfg, vclock, sys_root, scene="emitters: []\n")
    n = svc.store.db.execute(
        "SELECT tx_count FROM rollup_hour WHERE hour_ts = ? AND hz = ?", (hour, GMRS)
    ).fetchone()
    assert n is not None and n[0] >= 2
    svc.shutdown()


class _WatchedClock:
    """A virtual clock whose sleeps run the progress watchdog every 10 s, as the real
    watchdog thread does while the scan loop sits inside one step."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.on_tick = lambda: None

    def now(self) -> float:
        return self.inner.now()

    def monotonic(self) -> float:
        return self.inner.monotonic()

    def sleep(self, s: float) -> None:
        while s > 0:
            step = min(10.0, s)
            self.inner.advance(step)
            s -= step
            self.on_tick()


class _DeadDongle:
    opens = 0

    def open(self) -> None:
        from sdrscan.iq.base import IqSourceError

        self.opens += 1
        raise IqSourceError("usb_claim_interface error -4")

    def close(self) -> None:
        pass

    def health(self) -> dict:
        return {}


def test_a_lost_dongle_backs_off_without_tripping_the_watchdog_then_exits_cleanly(
    tmp_path, vclock, sys_root
):
    """Deliberate backoff is progress, not a hang: the watchdog stays quiet through the
    full 5 s to 300 s ladder. After MAX_OPEN_FAILURES the service stops on purpose, so
    systemd restarts the container and it rescans the USB bus."""
    from sdrscan.runner import MAX_OPEN_FAILURES, DongleLost

    clock = _WatchedClock(vclock)
    src = _DeadDongle()
    svc = Service(
        _plain_cfg(tmp_path), clock, src, spectrum="iq", statvfs=FakeFs(0.5), sys_root=sys_root
    )
    codes: list[int] = []
    clock.on_tick = lambda: svc.watchdog_check(codes.append)
    with pytest.raises(DongleLost):
        svc.run(until=vclock.now() + 7 * 86400)
    assert codes == [], "the watchdog fired during a deliberate backoff"
    assert src.opens == MAX_OPEN_FAILURES
    assert svc.runner.status["mode"] == "no_dongle"
    svc.shutdown()


def test_the_watchdog_still_fires_when_a_read_hangs(tmp_path, vclock, sys_root):
    svc = _service(_plain_cfg(tmp_path), vclock, sys_root)
    codes: list[int] = []
    vclock.advance(130)  # no step completed, and no backoff in progress
    assert not svc.watchdog_check(codes.append) and codes == [70]
    svc.shutdown()
