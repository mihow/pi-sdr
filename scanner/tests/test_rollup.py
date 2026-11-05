from __future__ import annotations

import pytest

from sdrscan import rollup
from sdrscan.store import Store
from sdrscan.tracker import Transmission

from .conftest import T0

H = 3600
DAY = 86400
A, B = 462_562_500, 146_520_000


def _tx(hz: int, t: float, dur: float = 10.0, cls: str = "unknown", snr: float = 20.0):
    return Transmission(
        hz=hz,
        band="b",
        mode="nfm",
        t_start=t,
        t_end=t + dur,
        dur_ms=int(round(dur * 1000)),
        peak_snr_db=snr,
        med_snr_db=snr - 3,
        floor_db=-50.0,
        bw_hz=11_000.0,
        source="survey",
        cls=cls,
    )


@pytest.fixture
def store(tmp_path, vclock):
    s = Store.open(tmp_path / "scan.sqlite", vclock)
    s.migrate()
    yield s
    s.close()


def _add(store: Store, tx: Transmission) -> int:
    i = store.add_tx(tx)
    rollup.on_tx_closed(store, tx)
    return i


def _hours(store: Store) -> list[tuple]:
    store.flush()
    return [
        (
            r["hz"],
            r["hour_ts"],
            round(r["active_s"], 3),
            r["tx_count"],
            r["voice_tx"],
            round(r["voice_s"], 3),
            r["max_snr_db"],
            r["first_ts"],
            r["last_ts"],
            r["keyup_tx"],
        )
        for r in store.db.execute("SELECT * FROM rollup_hour ORDER BY hz, hour_ts")
    ]


def test_incremental_and_recompute_agree(store):
    for i in range(40):
        cls = "keyup" if i % 4 == 0 else "voice"
        _add(store, _tx(A if i % 3 else B, T0 + i * 211.7, dur=13.3 + i % 5, cls=cls))
    before = _hours(store)
    assert sum(row[-1] for row in before) == 10
    for h in {row[1] for row in before}:
        rollup.recompute_hour(store, h)
    assert _hours(store) == before
    rollup.recompute_hour(store, before[0][1])  # twice is a no-op
    assert _hours(store) == before


def test_transmission_across_an_hour_splits_its_seconds(store):
    _add(store, _tx(A, T0 + H - 20, dur=50, cls="voice"))
    rows = _hours(store)
    assert [(r[1], r[2], r[3], r[5]) for r in rows] == [
        (int(T0), 20.0, 1, 20.0),
        (int(T0 + H), 30.0, 0, 30.0),
    ]


def test_late_event_and_recompute_change_only_that_hour(store):
    _add(store, _tx(A, T0 + 100))
    _add(store, _tx(A, T0 + H + 100))
    before = _hours(store)
    store.add_tx(_tx(A, T0 + 500, dur=7))  # late row, no incremental update
    rollup.recompute_hour(store, int(T0))
    after = _hours(store)
    assert after[1] == before[1]
    assert after[0][2] == 17.0 and after[0][3] == 2


def test_week_is_seven_days_plus_today(store, vclock):
    for d in range(9):
        _add(store, _tx(A, T0 + d * DAY + 600, dur=60))
        _add(store, _tx(B, T0 + d * DAY + 7200, dur=30))
    now = T0 + 8 * DAY + 3 * H
    vclock.set(now)
    for d in range(8):
        rollup.rollup_day(store, int(T0 + d * DAY))
    w = rollup.query_window(store, "week", now)
    days = store.db.execute(
        "SELECT hz, SUM(active_s) s FROM rollup_day WHERE day_ts >= ? GROUP BY hz",
        (int(T0 + DAY),),
    ).fetchall()
    for r in days:
        today = 60 if r["hz"] == A else 30
        assert w.channels[r["hz"]].active_s == pytest.approx(r["s"] + today)
    assert w.channels[A].tx_count == 8


def test_all_time_survives_pruning(store, vclock):
    _add(store, _tx(A, T0 + 100, dur=40))
    rollup.rollup_day(store, int(T0))
    now = T0 + 500 * DAY
    vclock.set(now)
    store.prune(now)
    assert not store.transmissions()
    assert not store.db.execute("SELECT * FROM rollup_hour").fetchall()
    w = rollup.query_window(store, "all", now)
    assert w.channels[A].active_s == pytest.approx(40)
    assert w.channels[A].tx_count == 1


def test_hour_and_day_windows(store, vclock):
    now = T0 + 5 * H + 1800
    _add(store, _tx(A, now - 600, dur=20))
    _add(store, _tx(A, now - 4000, dur=20))  # outside the trailing hour, inside the day
    _add(store, _tx(B, now - 30 * H, dur=20))  # outside the day
    vclock.set(now)
    hour = rollup.query_window(store, "hour", now)
    day = rollup.query_window(store, "day", now)
    assert hour.channels[A].tx_count == 1 and B not in hour.channels
    assert day.channels[A].tx_count == 2 and B not in day.channels
    assert len(day.series[A]) == 24 and sum(day.series[A]) == 40


def test_clock_correction_moves_rows_into_the_right_hours(store):
    bad = _tx(A, T0 + 100)
    bad.clock_ok = False
    _add(store, bad)
    _add(store, _tx(B, T0 + 200))
    rollup.apply_clock_correction(store, offset_s=2 * H)
    rows = _hours(store)
    assert (A, int(T0 + 2 * H)) in [(r[0], r[1]) for r in rows]
    assert (A, int(T0)) not in [(r[0], r[1]) for r in rows]
    assert (B, int(T0)) in [(r[0], r[1]) for r in rows]
    store.flush()
    assert (
        store.db.execute("SELECT COUNT(*) FROM transmissions WHERE clock_ok = 0").fetchone()[0] == 0
    )


def test_profiles_track_hour_of_week(store):
    for week in range(3):
        _add(store, _tx(A, T0 + week * 7 * DAY + 3 * H + 60, dur=120))
        for h in range(24):
            rollup.update_profiles(store, int(T0 + week * 7 * DAY + h * H))
    store.flush()
    row = store.db.execute("SELECT * FROM profile_how WHERE hz = ? AND how = 3", (A,)).fetchone()
    assert row["n"] == 3 and row["ema_active_s"] == pytest.approx(120)
    rollup.update_profiles(store, int(T0 + 2 * 7 * DAY + 3 * H))  # repeat is a no-op
    store.flush()
    again = store.db.execute("SELECT n FROM profile_how WHERE hz = ? AND how = 3", (A,)).fetchone()
    assert again["n"] == 3


def test_key_ups_are_counted_in_every_window(store, vclock):
    for d in range(3):
        _add(store, _tx(B, T0 + d * DAY + 600, dur=1.2, cls="keyup"))
        _add(store, _tx(B, T0 + d * DAY + 900, dur=8.0, cls="voice"))
    for d in range(2):
        rollup.rollup_day(store, int(T0 + d * DAY))
    now = T0 + 2 * DAY + 1800
    vclock.set(now)
    want = {"hour": 1, "day": 1, "week": 3, "all": 3}
    for w, n in want.items():
        c = rollup.query_window(store, w, now).channels[B]
        assert (c.keyup_tx, c.voice_tx) == (n, n), w
        assert c.tx_count == 2 * n, w
