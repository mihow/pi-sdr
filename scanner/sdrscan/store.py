"""SQLite event store (WAL) with batched writes.

Frequencies are INTEGER Hz and times INTEGER unix seconds. Raw rows (transmissions, health,
transcripts) are kept 90 days; hourly aggregates 400 days; day rollups, per-channel totals
and hour-of-week profiles forever, so week and all-time views survive pruning. Writes
are queued in memory and committed every ``batch_s`` seconds or on ``flush()``, which keeps
SD card writes few and large.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import statistics
import uuid
from pathlib import Path

import numpy as np

from .clock import Clock
from .detect import HopResult
from .tracker import Transmission

SCHEMA_VERSION = 1
SPECTRUM_LO_MHZ = 24
SPECTRUM_BINS = 1742  # 1 MHz bins from 24 MHz to 1766 MHz
DAY_S = 86400
AGG_RETENTION_DAYS = 400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS channels (
  hz INTEGER PRIMARY KEY, band TEXT, mode TEXT, bw_hz INTEGER, name TEXT,
  first_seen INTEGER, last_seen INTEGER, always_on INTEGER DEFAULT 0, spur INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS transmissions (
  id INTEGER PRIMARY KEY, hz INTEGER NOT NULL, t_start INTEGER NOT NULL, t_end INTEGER NOT NULL,
  dur_ms INTEGER, peak_snr_db REAL, med_snr_db REAL, floor_db REAL, bw_hz INTEGER, source TEXT,
  voice_ratio REAL, class TEXT, clip_id INTEGER, transcript_id INTEGER, clock_ok INTEGER,
  boot_id TEXT);
CREATE INDEX IF NOT EXISTS transmissions_t ON transmissions (t_start);
CREATE INDEX IF NOT EXISTS transmissions_hz ON transmissions (hz, t_start);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY, hz INTEGER, t_start INTEGER, dur_ms INTEGER, bytes INTEGER, codec TEXT,
  path TEXT, tier INTEGER, pinned INTEGER DEFAULT 0, sha256 TEXT, evicted_at INTEGER);
CREATE TABLE IF NOT EXISTS transcripts (
  id INTEGER PRIMARY KEY, clip_id INTEGER, engine TEXT, text TEXT, confidence REAL, rtf REAL,
  created INTEGER);
CREATE TABLE IF NOT EXISTS rollup_hour (
  hz INTEGER, hour_ts INTEGER, active_s REAL DEFAULT 0, tx_count INTEGER DEFAULT 0,
  voice_tx INTEGER DEFAULT 0, voice_s REAL DEFAULT 0, max_snr_db REAL, first_ts INTEGER,
  last_ts INTEGER, PRIMARY KEY (hz, hour_ts));
CREATE TABLE IF NOT EXISTS band_hour (
  band TEXT, hour_ts INTEGER, occupancy_pct REAL, peak_count INTEGER, floor_med_db REAL,
  coverage_pct REAL, PRIMARY KEY (band, hour_ts));
CREATE TABLE IF NOT EXISTS spectrum_hour (hour_ts INTEGER PRIMARY KEY, bins BLOB);
CREATE TABLE IF NOT EXISTS rollup_day (
  hz INTEGER, day_ts INTEGER, active_s REAL DEFAULT 0, tx_count INTEGER DEFAULT 0,
  voice_tx INTEGER DEFAULT 0, voice_s REAL DEFAULT 0, max_snr_db REAL,
  active_hours INTEGER DEFAULT 0, PRIMARY KEY (hz, day_ts));
CREATE TABLE IF NOT EXISTS profile_how (
  hz INTEGER, how INTEGER, ema_active_s REAL, ema_var REAL DEFAULT 0, n INTEGER DEFAULT 0,
  PRIMARY KEY (hz, how));
CREATE TABLE IF NOT EXISTS channel_total (
  hz INTEGER PRIMARY KEY, active_s REAL DEFAULT 0, tx_count INTEGER DEFAULT 0,
  voice_tx INTEGER DEFAULT 0, hours_seen INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS health (
  ts INTEGER PRIMARY KEY, temp_c REAL, throttled INTEGER, disk_free_mb REAL, quota_used_mb REAL,
  dongle_ok INTEGER, drops INTEGER, clip_frac REAL, mode TEXT, clock_ok INTEGER);
CREATE TABLE IF NOT EXISTS uplink_log (
  ts INTEGER, target TEXT, bytes_out INTEGER, bytes_in INTEGER, status INTEGER);
"""
TABLES = (
    "meta", "channels", "transmissions", "clips", "transcripts", "rollup_hour", "band_hour",
    "spectrum_hour", "rollup_day", "profile_how", "channel_total", "health", "uplink_log",
)  # fmt: skip
_HEALTH_COLS = (
    "ts", "temp_c", "throttled", "disk_free_mb", "quota_used_mb", "dongle_ok", "drops",
    "clip_frac", "mode", "clock_ok",
)  # fmt: skip


@dataclasses.dataclass
class _HourAcc:
    occ_sum: np.ndarray
    occ_n: np.ndarray
    bands: dict[str, list]  # band -> [occupancy sum, hops, peaks, floors]


class Store:
    def __init__(
        self,
        db: sqlite3.Connection,
        clock: Clock,
        batch_s: float = 10.0,
        retention_days: int = 90,
        boot_id: str | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.batch_s = batch_s
        self.retention_days = retention_days
        self.boot_id = boot_id or uuid.uuid4().hex[:12]
        self._pending: list[tuple[str, tuple]] = []
        self._last_flush = clock.monotonic()
        self._next_id: dict[str, int] = {}
        self._hours: dict[int, _HourAcc] = {}

    @classmethod
    def open(cls, path: Path, clock: Clock, **kw) -> Store:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA journal_size_limit=4194304")
        db.execute("PRAGMA synchronous=NORMAL")
        return cls(db, clock, **kw)

    def close(self) -> None:
        self.flush()
        self.db.close()

    # -- schema ------------------------------------------------------------------------
    def migrate(self) -> None:
        self.db.executescript(_SCHEMA)
        current = self.meta("schema_version")
        if current is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif int(current) > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {current} is newer than this build")
        self.db.commit()
        for table in ("transmissions", "clips", "transcripts"):
            row = self.db.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()
            self._next_id[table] = int(row[0]) + 1

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # -- writes (batched) --------------------------------------------------------------
    def _queue(self, sql: str, params: tuple) -> None:
        self._pending.append((sql, params))
        if self.clock.monotonic() - self._last_flush >= self.batch_s:
            self.flush()

    def defer(self, sql: str, params: tuple) -> None:
        """Queue a write with the batch (rollups use it, so they commit with their rows)."""
        self._queue(sql, params)

    def _new_id(self, table: str) -> int:
        i = self._next_id[table]
        self._next_id[table] = i + 1
        return i

    def add_tx(self, tx: Transmission) -> int:
        tx_id = self._new_id("transmissions")
        t0, t1 = int(round(tx.t_start)), int(round(tx.t_end))
        self._queue(
            "INSERT INTO transmissions (id, hz, t_start, t_end, dur_ms, peak_snr_db, med_snr_db,"
            " floor_db, bw_hz, source, voice_ratio, class, clip_id, transcript_id, clock_ok,"
            " boot_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tx_id, int(tx.hz), t0, t1, int(tx.dur_ms), tx.peak_snr_db, tx.med_snr_db,
                tx.floor_db, int(tx.bw_hz), tx.source, tx.voice_ratio, tx.cls, tx.clip_id, None,
                int(tx.clock_ok), self.boot_id,
            ),
        )  # fmt: skip
        self._queue(
            "INSERT INTO channels (hz, band, mode, bw_hz, name, first_seen, last_seen)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(hz) DO UPDATE SET"
            " last_seen = MAX(last_seen, excluded.last_seen),"
            " first_seen = MIN(first_seen, excluded.first_seen)",
            (int(tx.hz), tx.band, tx.mode, int(tx.bw_hz), None, t0, t1),
        )
        return tx_id

    def mark_channel(
        self,
        hz: int,
        *,
        band: str | None = None,
        always_on: bool | None = None,
        spur: bool | None = None,
        t: float | None = None,
    ) -> None:
        ts = int(t if t is not None else self.clock.now())
        self._queue(
            "INSERT INTO channels (hz, band, first_seen, last_seen) VALUES (?,?,?,?)"
            " ON CONFLICT(hz) DO NOTHING",
            (int(hz), band, ts, ts),
        )
        if always_on is not None:
            self._queue("UPDATE channels SET always_on = ? WHERE hz = ?", (int(always_on), hz))
        if spur is not None:
            self._queue("UPDATE channels SET spur = ? WHERE hz = ?", (int(spur), hz))

    def add_clip(
        self,
        *,
        hz: int,
        t_start: float,
        dur_ms: int,
        nbytes: int,
        codec: str,
        path: str | None,
        tier: int,
        sha256: str,
        pinned: bool = False,
    ) -> int:
        clip_id = self._new_id("clips")
        self._queue(
            "INSERT INTO clips (id, hz, t_start, dur_ms, bytes, codec, path, tier, pinned,"
            " sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                clip_id,
                int(hz),
                int(t_start),
                dur_ms,
                nbytes,
                codec,
                path,
                tier,
                int(pinned),
                sha256,
            ),
        )
        return clip_id

    def evict_clip(self, clip_id: int, t: float) -> None:
        self._queue(
            "UPDATE clips SET path = NULL, evicted_at = ? WHERE id = ?", (int(t), int(clip_id))
        )

    def update_clip_tier(self, clip_id: int, tier: int) -> None:
        self._queue("UPDATE clips SET tier = ? WHERE id = ?", (int(tier), int(clip_id)))

    def add_transcript(
        self,
        clip_id: int,
        tx_id: int | None,
        engine: str,
        text: str,
        confidence: float,
        rtf: float | None,
        created: float,
    ) -> int:
        tid = self._new_id("transcripts")
        self._queue(
            "INSERT INTO transcripts (id, clip_id, engine, text, confidence, rtf, created)"
            " VALUES (?,?,?,?,?,?,?)",
            (tid, int(clip_id), engine, text, float(confidence), rtf, int(created)),
        )
        if tx_id is not None:
            self._queue(
                "UPDATE transmissions SET transcript_id = ? WHERE id = ?", (tid, int(tx_id))
            )
        return tid

    def add_health(self, h: dict) -> None:
        row = tuple(h.get(c) for c in _HEALTH_COLS)
        row = (int(row[0]),) + row[1:]
        cols = ", ".join(_HEALTH_COLS)
        marks = ",".join("?" * len(_HEALTH_COLS))
        self._queue(f"INSERT OR REPLACE INTO health ({cols}) VALUES ({marks})", row)

    def add_uplink(self, ts: float, target: str, bytes_out: int, bytes_in: int, status: int):
        self._queue(
            "INSERT INTO uplink_log (ts, target, bytes_out, bytes_in, status) VALUES (?,?,?,?,?)",
            (int(ts), target, bytes_out, bytes_in, status),
        )

    def add_hop(self, r: HopResult, t: float, band: str | None = None) -> None:
        """Accumulate a hop into the hour's spectrum and band rows (written on flush)."""
        hour = int(t // 3600 * 3600)
        acc = self._hours.get(hour)
        if acc is None:
            acc = self._hours[hour] = _HourAcc(
                occ_sum=np.zeros(SPECTRUM_BINS), occ_n=np.zeros(SPECTRUM_BINS), bands={}
            )
        occ_vals = []
        for mhz, frac in r.occupancy.items():
            i = mhz - SPECTRUM_LO_MHZ
            if 0 <= i < SPECTRUM_BINS:
                acc.occ_sum[i] += frac
                acc.occ_n[i] += 1
            occ_vals.append(frac)
        if band is not None:
            b = acc.bands.setdefault(band, [0.0, 0, 0, []])
            b[0] += float(np.mean(occ_vals)) if occ_vals else 0.0
            b[1] += 1
            b[2] += len(r.peaks)
            if len(b[3]) < 2000:
                b[3].append(r.floor_db)
        if len(self._hours) > 1 and self.clock.monotonic() - self._last_flush >= self.batch_s:
            self.flush()

    def _write_hours(self) -> None:
        now_hour = int(self.clock.now() // 3600 * 3600)
        for hour, acc in list(self._hours.items()):
            pct = np.where(acc.occ_n > 0, acc.occ_sum / np.maximum(acc.occ_n, 1) * 100, 0)
            bins = np.clip(np.round(pct), 0, 100).astype(np.uint8).tobytes()
            self.db.execute(
                "INSERT OR REPLACE INTO spectrum_hour (hour_ts, bins) VALUES (?, ?)", (hour, bins)
            )
            for band, (occ, hops, peaks, floors) in acc.bands.items():
                self.db.execute(
                    "INSERT INTO band_hour (band, hour_ts, occupancy_pct, peak_count,"
                    " floor_med_db) VALUES (?,?,?,?,?) ON CONFLICT(band, hour_ts) DO UPDATE SET"
                    " occupancy_pct = excluded.occupancy_pct, peak_count = excluded.peak_count,"
                    " floor_med_db = excluded.floor_med_db",
                    (
                        band,
                        hour,
                        round(occ / hops * 100, 1) if hops else 0.0,
                        peaks,
                        round(statistics.median(floors), 1) if floors else None,
                    ),
                )
            if hour < now_hour:
                del self._hours[hour]

    def set_coverage(self, hour_ts: int, coverage_pct: float) -> None:
        self._queue(
            "UPDATE band_hour SET coverage_pct = ? WHERE hour_ts = ?",
            (round(coverage_pct, 1), int(hour_ts)),
        )

    def flush(self) -> None:
        pending, self._pending = self._pending, []
        with self.db:
            # Hour rows first, so a queued coverage update finds its rows.
            self._write_hours()
            for sql, params in pending:
                self.db.execute(sql, params)
        self._last_flush = self.clock.monotonic()

    def prune(self, now: float) -> None:
        """Drop raw rows past retention. Day rollups, totals and profiles are kept forever."""
        self.flush()
        raw = int(now - self.retention_days * DAY_S)
        agg = int(now - AGG_RETENTION_DAYS * DAY_S)
        with self.db:
            self.db.execute("DELETE FROM transmissions WHERE t_start < ?", (raw,))
            self.db.execute("DELETE FROM health WHERE ts < ?", (raw,))
            self.db.execute("DELETE FROM transcripts WHERE created < ?", (raw,))
            for table, col in (
                ("rollup_hour", "hour_ts"),
                ("band_hour", "hour_ts"),
                ("spectrum_hour", "hour_ts"),
                ("uplink_log", "ts"),
            ):
                self.db.execute(f"DELETE FROM {table} WHERE {col} < ?", (agg,))

    # -- reads -------------------------------------------------------------------------
    def transmissions(self, t0: float | None = None, t1: float | None = None) -> list[dict]:
        sql = "SELECT * FROM transmissions WHERE t_start >= ? AND t_start < ? ORDER BY id"
        lo = int(t0) if t0 is not None else -(2**62)
        hi = int(t1) if t1 is not None else 2**62
        return [dict(r) for r in self.db.execute(sql, (lo, hi))]

    def channels(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM channels ORDER BY hz")]

    def spectrum_hour(self, hour_ts: int) -> bytes:
        row = self.db.execute(
            "SELECT bins FROM spectrum_hour WHERE hour_ts = ?", (int(hour_ts),)
        ).fetchone()
        return b"" if row is None else bytes(row[0])
