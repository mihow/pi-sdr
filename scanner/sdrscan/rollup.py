"""Hour, day and all-time rollups, kept incrementally and rebuildable from raw rows.

``on_tx_closed`` adds one transmission to its ``rollup_hour`` rows as it closes. Any hour
can be rebuilt from the raw ``transmissions`` with ``recompute_hour``, and both paths use
the same integer-millisecond split, so they agree exactly. That makes late events and
clock corrections safe. ``rollup_day`` folds a finished day into the permanent
``rollup_day`` table and refreshes ``channel_total``, so week and all-time views survive
the 90-day pruning of raw rows. ``query_window`` answers the dashboard windows (spec
section 7):

    hour  the trailing 60 minutes, from raw transmissions
    day   the last 24 rollup_hour rows
    week  the last 7 rollup_day rows plus today's hours
    all   channel_total plus today's hours
"""

from __future__ import annotations

import dataclasses

from .store import Store
from .tracker import Transmission

H = 3600
DAY = 86400
HOW_EPOCH_OFFSET_H = 72  # 1970-01-01 was a Thursday; hour-of-week 0 is Monday 00:00 UTC
PROFILE_ALPHA_MIN = 0.2
WINDOWS = ("hour", "day", "week", "all")


@dataclasses.dataclass
class ChanStats:
    active_s: float = 0.0
    tx_count: int = 0
    voice_tx: int = 0
    voice_s: float = 0.0
    max_snr_db: float | None = None
    last_ts: int | None = None

    def add(self, active_s, tx_count, voice_tx, voice_s, max_snr, last_ts=None) -> None:
        self.active_s += active_s or 0.0
        self.tx_count += tx_count or 0
        self.voice_tx += voice_tx or 0
        self.voice_s += voice_s or 0.0
        if max_snr is not None:
            self.max_snr_db = max_snr if self.max_snr_db is None else max(self.max_snr_db, max_snr)
        if last_ts is not None:
            self.last_ts = last_ts if self.last_ts is None else max(self.last_ts, last_ts)


@dataclasses.dataclass
class WindowStats:
    window: str
    t0: float
    t1: float
    channels: dict[int, ChanStats]
    series: dict[int, list[int]]  # per channel: active seconds per cell
    cell_s: int


def hour_of(t: float) -> int:
    return int(t // H * H)


def day_of(t: float) -> int:
    return int(t // DAY * DAY)


def how_of(hour_ts: int) -> int:
    return (hour_ts // H + HOW_EPOCH_OFFSET_H) % 168


def _split(t_start: int, dur_ms: int) -> list[tuple[int, int, int, int]]:
    """(hour_ts, active_ms, first_ts, last_ts) for each hour a transmission touches."""
    t0_ms = t_start * 1000
    t1_ms = t0_ms + max(0, dur_ms)
    out = []
    h = hour_of(t_start)
    while True:
        lo = max(t0_ms, h * 1000)
        hi = min(t1_ms, (h + H) * 1000)
        out.append((h, max(0, hi - lo), lo // 1000, -(-hi // 1000)))
        h += H
        if h * 1000 >= t1_ms:
            break
    return out


def _tx_parts(t_start: int, dur_ms: int, cls: str | None, snr: float | None):
    voice = cls == "voice"
    for i, (h, ms, first, last) in enumerate(_split(t_start, dur_ms)):
        yield h, ms / 1000, int(i == 0), int(voice and i == 0), (ms / 1000 if voice else 0.0), (
            snr,
            first,
            last,
        )


_UPSERT_HOUR = (
    "INSERT INTO rollup_hour (hz, hour_ts, active_s, tx_count, voice_tx, voice_s, max_snr_db,"
    " first_ts, last_ts) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(hz, hour_ts) DO UPDATE SET"
    " active_s = active_s + excluded.active_s, tx_count = tx_count + excluded.tx_count,"
    " voice_tx = voice_tx + excluded.voice_tx, voice_s = voice_s + excluded.voice_s,"
    " max_snr_db = MAX(COALESCE(max_snr_db, excluded.max_snr_db), excluded.max_snr_db),"
    " first_ts = MIN(first_ts, excluded.first_ts), last_ts = MAX(last_ts, excluded.last_ts)"
)


def on_tx_closed(store: Store, tx: Transmission) -> None:
    """Add a just-stored transmission to its hours (queued with the store's batch)."""
    t0 = int(round(tx.t_start))
    for h, active, n, v, vs, (snr, first, last) in _tx_parts(
        t0, int(tx.dur_ms), tx.cls, tx.peak_snr_db
    ):
        store.defer(_UPSERT_HOUR, (int(tx.hz), h, active, n, v, vs, snr, first, last))


def recompute_hour(store: Store, hour_ts: int) -> None:
    """Rebuild every ``rollup_hour`` row of one hour from the raw transmissions."""
    store.flush()
    hour_ts = int(hour_ts)
    rows = store.db.execute(
        "SELECT hz, t_start, dur_ms, class, peak_snr_db FROM transmissions"
        " WHERE t_start < ? AND t_start * 1000 + dur_ms > ?",
        (hour_ts + H, hour_ts * 1000),
    ).fetchall()
    acc: dict[int, list] = {}
    for r in rows:
        for h, active_s, n, v, vs, (snr, first, last) in _tx_parts(
            r["t_start"], r["dur_ms"], r["class"], r["peak_snr_db"]
        ):
            if h != hour_ts:
                continue
            a = acc.setdefault(r["hz"], [0, 0, 0, 0, None, first, last])
            a[0] += round(active_s * 1000)
            a[1] += n
            a[2] += v
            a[3] += round(vs * 1000)
            if snr is not None:
                a[4] = snr if a[4] is None else max(a[4], snr)
            a[5] = min(a[5], first)
            a[6] = max(a[6], last)
    with store.db:
        store.db.execute("DELETE FROM rollup_hour WHERE hour_ts = ?", (hour_ts,))
        store.db.executemany(
            "INSERT INTO rollup_hour (hz, hour_ts, active_s, tx_count, voice_tx, voice_s,"
            " max_snr_db, first_ts, last_ts) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (hz, hour_ts, a[0] / 1000, a[1], a[2], a[3] / 1000, a[4], a[5], a[6])
                for hz, a in sorted(acc.items())
            ],
        )


def rollup_day(store: Store, day_ts: int) -> None:
    """Fold one UTC day of hours into ``rollup_day`` and refresh ``channel_total``."""
    store.flush()
    day_ts = day_of(day_ts)
    with store.db:
        store.db.execute("DELETE FROM rollup_day WHERE day_ts = ?", (day_ts,))
        store.db.execute(
            "INSERT INTO rollup_day (hz, day_ts, active_s, tx_count, voice_tx, voice_s,"
            " max_snr_db, active_hours) SELECT hz, ?, SUM(active_s), SUM(tx_count),"
            " SUM(voice_tx), SUM(voice_s), MAX(max_snr_db), COUNT(*) FROM rollup_hour"
            " WHERE hour_ts >= ? AND hour_ts < ? GROUP BY hz",
            (day_ts, day_ts, day_ts + DAY),
        )
        store.db.execute("DELETE FROM channel_total")
        store.db.execute(
            "INSERT INTO channel_total (hz, active_s, tx_count, voice_tx, hours_seen)"
            " SELECT hz, SUM(active_s), SUM(tx_count), SUM(voice_tx), SUM(active_hours)"
            " FROM rollup_day GROUP BY hz"
        )
        store.set_meta("rolled_through", str(day_ts + DAY))


def update_profiles(store: Store, hour_ts: int) -> None:
    """Fold one finished hour into each channel's hour-of-week profile (once per hour)."""
    store.flush()
    hour_ts = hour_of(hour_ts)
    done = store.meta("profiles_through")
    if done is not None and hour_ts < int(done):
        return
    how = how_of(hour_ts)
    active = {
        r["hz"]: r["active_s"]
        for r in store.db.execute(
            "SELECT hz, active_s FROM rollup_hour WHERE hour_ts = ?", (hour_ts,)
        )
    }
    known = [
        r["hz"]
        for r in store.db.execute(
            "SELECT hz FROM channels WHERE always_on = 0 AND spur = 0 AND first_seen <= ?"
            " AND last_seen >= ?",
            (hour_ts + H, hour_ts - 30 * DAY),
        )
    ]
    prev = {
        r["hz"]: (r["ema_active_s"], r["ema_var"], r["n"])
        for r in store.db.execute("SELECT * FROM profile_how WHERE how = ?", (how,))
    }
    rows = []
    for hz in sorted(set(known) | set(active)):
        x = float(active.get(hz, 0.0))
        mean, var, n = prev.get(hz, (0.0, 0.0, 0))
        a = max(1.0 / (n + 1), PROFILE_ALPHA_MIN)
        d = x - mean
        mean += a * d
        var = (1 - a) * (var + a * d * d) if n else 0.0
        rows.append((hz, how, mean, var, n + 1))
    with store.db:
        store.db.executemany(
            "INSERT OR REPLACE INTO profile_how (hz, how, ema_active_s, ema_var, n)"
            " VALUES (?,?,?,?,?)",
            rows,
        )
        store.set_meta("profiles_through", str(hour_ts + H))


def apply_clock_correction(store: Store, offset_s: float) -> None:
    """Shift rows recorded with an unsynced clock by ``offset_s`` and rebuild their hours."""
    store.flush()
    off = int(round(offset_s))
    rows = store.db.execute(
        "SELECT t_start, dur_ms FROM transmissions WHERE clock_ok = 0"
    ).fetchall()
    hours: set[int] = set()
    for r in rows:
        for h, *_ in _split(r["t_start"], r["dur_ms"]):
            hours.add(h)
            hours.add(hour_of(h + off))
    with store.db:
        store.db.execute(
            "UPDATE transmissions SET t_start = t_start + ?, t_end = t_end + ?, clock_ok = 1"
            " WHERE clock_ok = 0",
            (off, off),
        )
    for h in sorted(hours):
        recompute_hour(store, h)


# -- windows -----------------------------------------------------------------------------
def query_window(store: Store, window: str, now: float) -> WindowStats:
    store.flush()
    if window == "hour":
        return _hour_window(store, now)
    if window == "day":
        return _day_window(store, now)
    if window == "week":
        return _week_window(store, now)
    if window == "all":
        return _all_window(store, now)
    raise ValueError(f"unknown window {window!r}; expected one of {WINDOWS}")


def _hour_window(store: Store, now: float) -> WindowStats:
    t0 = now - H
    cell = 300
    chans: dict[int, ChanStats] = {}
    series: dict[int, list[int]] = {}
    for r in store.db.execute(
        "SELECT hz, t_start, dur_ms, class, peak_snr_db, t_end FROM transmissions"
        " WHERE t_start * 1000 + dur_ms > ? AND t_start < ?",
        (int(t0 * 1000), int(now) + 1),
    ):
        s0 = max(r["t_start"] * 1000, int(t0 * 1000))
        s1 = min(r["t_start"] * 1000 + r["dur_ms"], int(now * 1000))
        active = max(0, s1 - s0) / 1000
        voice = r["class"] == "voice"
        counted = int(r["t_start"] >= t0)
        chans.setdefault(r["hz"], ChanStats()).add(
            active, counted, int(voice and counted), active if voice else 0.0,
            r["peak_snr_db"], r["t_end"],
        )  # fmt: skip
        cells = series.setdefault(r["hz"], [0] * 12)
        for i in range(12):
            c0 = int(t0 * 1000) + i * cell * 1000
            ov = min(s1, c0 + cell * 1000) - max(s0, c0)
            if ov > 0:
                cells[i] += ov
    series = {hz: [int(round(v / 1000)) for v in cells] for hz, cells in series.items()}
    return WindowStats("hour", t0, now, chans, series, cell)


def _day_window(store: Store, now: float) -> WindowStats:
    h_now = hour_of(now)
    first = h_now - 23 * H
    chans: dict[int, ChanStats] = {}
    series: dict[int, list[int]] = {}
    for r in store.db.execute(
        "SELECT * FROM rollup_hour WHERE hour_ts >= ? AND hour_ts <= ?", (first, h_now)
    ):
        chans.setdefault(r["hz"], ChanStats()).add(
            r["active_s"], r["tx_count"], r["voice_tx"], r["voice_s"], r["max_snr_db"],
            r["last_ts"],
        )  # fmt: skip
        cells = series.setdefault(r["hz"], [0] * 24)
        cells[(r["hour_ts"] - first) // H] += int(round(r["active_s"]))
    return WindowStats("day", first, now, chans, series, H)


def _today_hours(store: Store, now: float, chans: dict[int, ChanStats]) -> dict[int, float]:
    today: dict[int, float] = {}
    for r in store.db.execute(
        "SELECT hz, SUM(active_s) a, SUM(tx_count) n, SUM(voice_tx) v, SUM(voice_s) vs,"
        " MAX(max_snr_db) m, MAX(last_ts) l FROM rollup_hour WHERE hour_ts >= ? GROUP BY hz",
        (day_of(now),),
    ):
        chans.setdefault(r["hz"], ChanStats()).add(r["a"], r["n"], r["v"], r["vs"], r["m"], r["l"])
        today[r["hz"]] = r["a"]
    return today


def _week_window(store: Store, now: float) -> WindowStats:
    today = day_of(now)
    first = today - 7 * DAY
    chans: dict[int, ChanStats] = {}
    series: dict[int, list[int]] = {}
    for r in store.db.execute(
        "SELECT * FROM rollup_day WHERE day_ts >= ? AND day_ts < ?", (first, today)
    ):
        chans.setdefault(r["hz"], ChanStats()).add(
            r["active_s"], r["tx_count"], r["voice_tx"], r["voice_s"], r["max_snr_db"]
        )
        series.setdefault(r["hz"], [0] * 8)[(r["day_ts"] - first) // DAY] += int(
            round(r["active_s"])
        )
    for hz, a in _today_hours(store, now, chans).items():
        series.setdefault(hz, [0] * 8)[7] += int(round(a))
    return WindowStats("week", first, now, chans, series, DAY)


def _all_window(store: Store, now: float) -> WindowStats:
    chans: dict[int, ChanStats] = {}
    for r in store.db.execute("SELECT * FROM channel_total"):
        chans.setdefault(r["hz"], ChanStats()).add(
            r["active_s"], r["tx_count"], r["voice_tx"], 0.0, None
        )
    rolled = int(store.meta("rolled_through") or 0)
    for r in store.db.execute(
        "SELECT hz, SUM(active_s) a, SUM(tx_count) n, SUM(voice_tx) v, MAX(last_ts) l"
        " FROM rollup_hour WHERE hour_ts >= ? GROUP BY hz",
        (max(rolled, day_of(now)),),
    ):
        chans.setdefault(r["hz"], ChanStats()).add(r["a"], r["n"], r["v"], 0.0, None, r["l"])
    first = store.db.execute("SELECT MIN(first_seen) FROM channels").fetchone()[0]
    return WindowStats("all", float(first or now), now, chans, {}, 0)
