from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sdrscan.detect import HopResult, Peak
from sdrscan.store import SCHEMA_VERSION, TABLES, Store
from sdrscan.tracker import Transmission

DAY = 86400


def _tx(hz: int, t: float, dur: float = 10.0) -> Transmission:
    return Transmission(
        hz=hz,
        band="gmrs_frs_462",
        mode="nfm",
        t_start=t,
        t_end=t + dur,
        dur_ms=int(dur * 1000),
        peak_snr_db=22.5,
        med_snr_db=18.0,
        floor_db=-48.0,
        bw_hz=11_000.0,
        source="survey",
    )


@pytest.fixture
def store(tmp_path: Path, vclock):
    s = Store.open(tmp_path / "scan.sqlite", vclock)
    s.migrate()
    yield s
    s.close()


def test_migrate_creates_every_table_once(store, tmp_path):
    names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES) <= names
    assert store.meta("schema_version") == str(SCHEMA_VERSION) == "2"
    store.migrate()
    assert store.meta("schema_version") == "2"
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_writes_are_batched_until_flush(store, tmp_path, vclock):
    for i in range(1000):
        store.add_tx(_tx(462_562_500, vclock.now() + i))
    other = sqlite3.connect(tmp_path / "scan.sqlite")
    assert other.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] == 0
    store.flush()
    assert other.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] == 1000
    other.close()


def test_add_tx_returns_distinct_ids_before_flush(store, vclock):
    a = store.add_tx(_tx(462_562_500, vclock.now()))
    b = store.add_tx(_tx(462_562_500, vclock.now() + 1))
    assert b == a + 1
    store.flush()
    assert [r["id"] for r in store.transmissions()] == [a, b]


def test_batch_flushes_itself_after_ten_seconds(store, tmp_path, vclock):
    store.add_tx(_tx(462_562_500, vclock.now()))
    vclock.advance(11)
    store.add_tx(_tx(462_562_500, vclock.now()))
    other = sqlite3.connect(tmp_path / "scan.sqlite")
    assert other.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] >= 1
    other.close()


def test_frequencies_round_trip_as_int(store, vclock):
    store.add_tx(_tx(462_562_500, vclock.now()))
    store.flush()
    row = store.transmissions()[0]
    assert row["hz"] == 462_562_500 and type(row["hz"]) is int
    ch = store.channels()[0]
    assert type(ch["hz"]) is int and ch["band"] == "gmrs_frs_462"


def test_channels_table_tracks_first_and_last_seen(store, vclock):
    t = vclock.now()
    store.add_tx(_tx(462_562_500, t))
    store.add_tx(_tx(462_562_500, t + 500, dur=20))
    store.flush()
    ch = store.channels()[0]
    assert ch["first_seen"] == int(t) and ch["last_seen"] == int(t + 520)
    store.mark_channel(462_562_500, always_on=True)
    store.flush()
    assert store.channels()[0]["always_on"] == 1


def test_prune_keeps_aggregates_and_drops_old_raw_rows(store, vclock):
    now = vclock.now()
    store.add_tx(_tx(462_562_500, now - 91 * DAY))
    store.add_tx(_tx(462_562_500, now - 89 * DAY))
    store.add_health({"ts": now - 91 * DAY, "temp_c": 50.0, "mode": "normal"})
    store.add_health({"ts": now - 1, "temp_c": 51.0, "mode": "normal"})
    store.flush()
    db = store.db
    db.execute("INSERT INTO rollup_day (hz, day_ts, active_s) VALUES (1, 0, 5)")
    db.execute("INSERT INTO channel_total (hz, active_s) VALUES (1, 5)")
    db.execute("INSERT INTO profile_how (hz, how, ema_active_s) VALUES (1, 3, 1.0)")
    db.execute("INSERT INTO rollup_hour (hz, hour_ts, active_s) VALUES (1, 0, 5)")
    db.commit()
    store.prune(now)
    assert db.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM health").fetchone()[0] == 1
    for table in ("rollup_day", "channel_total", "profile_how"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM rollup_hour").fetchone()[0] == 0  # 400-day rule


def test_add_hop_accumulates_spectrum_and_band_hours(store, vclock):
    t = vclock.now()
    r = HopResult(
        peaks=[Peak(462_562_500.0, 11_000.0, -30.0, 18.0)],
        floor_db=-48.0,
        mad_db=0.5,
        clip_frac=0.0,
        occupancy={462: 0.25, 463: 0.0},
        center_hz=462_406_250,
        lo_hz=461_446_484,
        hi_hz=463_366_016,
    )
    store.add_hop(r, t, band="gmrs_frs_462")
    store.add_hop(r, t + 1, band="gmrs_frs_462")
    store.flush()
    bins = store.spectrum_hour(int(t // 3600 * 3600))
    assert len(bins) == 1742
    assert bins[462 - 24] == 25 and bins[463 - 24] == 0
    row = store.db.execute("SELECT * FROM band_hour").fetchone()
    assert row["band"] == "gmrs_frs_462" and row["peak_count"] == 2
    assert row["floor_med_db"] == pytest.approx(-48.0)


V1_TABLES = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE transmissions (
  id INTEGER PRIMARY KEY, hz INTEGER NOT NULL, t_start INTEGER NOT NULL, t_end INTEGER NOT NULL,
  dur_ms INTEGER, peak_snr_db REAL, med_snr_db REAL, floor_db REAL, bw_hz INTEGER, source TEXT,
  voice_ratio REAL, class TEXT, clip_id INTEGER, transcript_id INTEGER, clock_ok INTEGER,
  boot_id TEXT);
CREATE TABLE rollup_hour (
  hz INTEGER, hour_ts INTEGER, active_s REAL DEFAULT 0, tx_count INTEGER DEFAULT 0,
  voice_tx INTEGER DEFAULT 0, voice_s REAL DEFAULT 0, max_snr_db REAL, first_ts INTEGER,
  last_ts INTEGER, PRIMARY KEY (hz, hour_ts));
INSERT INTO meta VALUES ('schema_version', '1');
INSERT INTO transmissions (id, hz, t_start, t_end, dur_ms, class)
  VALUES (1, 5, 10, 20, 10000, 'voice');
"""


def test_schema_1_database_gains_the_new_columns_and_keeps_its_rows(tmp_path, vclock):
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript(V1_TABLES)
    old.close()
    s = Store.open(path, vclock)
    s.migrate()
    assert s.meta("schema_version") == "2"
    tx = _tx(462_562_500, vclock.now())
    tx.ctcss_hz = 100.0
    s.add_tx(tx)
    s.flush()
    rows = s.transmissions()
    assert [r["id"] for r in rows] == [1, 2] and rows[1]["ctcss_hz"] == 100.0
    cols = {r[1] for r in s.db.execute("PRAGMA table_info(rollup_hour)")}
    assert "keyup_tx" in cols
    s.close()


def test_call_signs_are_stored_raw_and_totalled_per_channel(store, vclock):
    t = vclock.now()
    got = store.add_calls_from_transcript(
        "kilo seven alpha bravo charlie this is W1AW", 0.9, hz=146_520_000, band="ham_2m", t=t
    )
    assert [c.call for c in got] == ["K7ABC", "W1AW"]
    store.add_calls_from_transcript("K7ABC", 0.9, hz=146_940_000, band="ham_2m", t=t + 60)
    store.add_calls_from_transcript("K7ABC", 0.3, hz=146_940_000, band="ham_2m", t=t + 90)
    store.flush()
    raw = store.db.execute("SELECT call, confidence FROM callsigns ORDER BY id").fetchall()
    assert [tuple(r) for r in raw][-1] == ("K7ABC", 0.3)  # capped by the transcript's
    totals = {(r["call"], r["hz"]): r["n"] for r in store.db.execute("SELECT * FROM callsign_hz")}
    # The 0.3 sighting is kept raw but is too unsure for the totals.
    assert totals == {("K7ABC", 146_520_000): 1, ("W1AW", 146_520_000): 1,
                      ("K7ABC", 146_940_000): 1}  # fmt: skip


def test_stage_counters_add_up_per_hour(store, vclock):
    t = vclock.now()
    store.count("survey_hops", t=t)
    store.count("survey_hops", 2, t=t + 10)
    store.count("dwell_s", 0.5, t=t + 3600)
    tx = _tx(462_562_500, t)
    tx.cls = "keyup"
    store.add_tx(tx)
    assert store.counts(t, t) == {"survey_hops": 3, "keyups": 1}
    store.count("survey_hops", t=t)  # a later flush adds to the stored hour
    assert store.counts(t, t + 3600) == {"survey_hops": 4, "keyups": 1, "dwell_s": 0.5}


def test_recall_calls_finds_calls_an_older_extractor_missed_once(store, vclock):
    """Transcripts stored before an extractor fix get their calls on a re-scan, and a
    second re-scan adds nothing, so the per-channel totals are not double counted."""
    from sdrscan.tracker import Transmission

    t = vclock.now()
    tx = Transmission(hz=462_550_000, band="gmrs_frs_462", mode="nfm", t_start=t, t_end=t + 5,
                      dur_ms=5000, peak_snr_db=20.0, med_snr_db=18.0, floor_db=-60.0,
                      bw_hz=11_000.0, source="dwell", cls="voice")  # fmt: skip
    tx_id = store.add_tx(tx)
    store.add_transcript(1, tx_id, "whisper", "this is WRAB 123 checking in", 0.9, 0.5, t + 10)
    tid = store.add_transcript(2, None, "whisper", "WRCD 456 here", 0.9, 0.5, t + 20)
    store.add_callsign(
        "WRAB123", "gmrs", hz=462_550_000, t=t + 10, confidence=0.9, tx_id=tx_id, transcript_id=99
    )  # fmt: skip  (another transcript)
    store.flush()
    band_of = lambda hz: "gmrs_frs_462"  # noqa: E731
    assert store.recall_calls(band_of) == 1
    assert store.recall_calls(band_of) == 0
    totals = {(r["call"], r["hz"]): r["n"] for r in store.db.execute("SELECT * FROM callsign_hz")}
    assert totals == {("WRAB123", 462_550_000): 2}
    assert tid  # the transcript with no transmission has no channel and is skipped
