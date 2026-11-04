from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from sdrscan.clips import ClipStore, FakeEncoder, OpusEncoder, WavEncoder
from sdrscan.config import QuotaConfig
from sdrscan.store import Store

from .audio import voice

KB = 1000


class FakeFs:
    """A statvfs stand-in whose free fraction the test sets."""

    def __init__(self, free_frac: float = 0.5) -> None:
        self.free_frac = free_frac

    def __call__(self, path):
        class R:
            f_frsize = 4096
            f_blocks = 1_000_000
            f_bavail = int(1_000_000 * self.free_frac)

        return R()


@pytest.fixture
def env(tmp_path, vclock):
    store = Store.open(tmp_path / "db" / "scan.sqlite", vclock)
    store.migrate()
    fs = FakeFs()

    def make(quota=1_000_000, nbytes=50 * KB, **kw):
        cfg = QuotaConfig(clip_quota_bytes=quota, **kw)
        return ClipStore(
            store,
            cfg,
            data_root=tmp_path / "data",
            stage_root=tmp_path / "stage",
            clock=vclock,
            encoder=FakeEncoder(nbytes),
            statvfs=fs,
        )

    return store, fs, make, tmp_path


def _clip(clips: ClipStore, vclock, hz: int, cls: str = "voice", seconds: float = 2.0, **meta):
    w = clips.begin(hz, vclock.now())
    w.write(voice(seconds))
    vclock.advance(10)
    return clips.finish(w, {"class": cls, **meta})


def _rows(store: Store) -> dict[int, dict]:
    store.flush()
    return {r["id"]: dict(r) for r in store.db.execute("SELECT * FROM clips")}


def test_21st_clip_evicts_the_oldest_noise_tier_clip(env, vclock):
    store, fs, make, _ = env
    clips = make()
    ids = []
    for i in range(20):
        cls = "noise" if i in (3, 7) else "voice"
        ids.append(_clip(clips, vclock, 462_000_000 + i * 12_500, cls))
    assert clips.used_bytes() == 1_000_000
    new = _clip(clips, vclock, 463_000_000)
    rows = _rows(store)
    evicted = [i for i, r in rows.items() if r["evicted_at"] is not None]
    assert evicted == [ids[3]], "oldest noise-tier clip goes first, before older voice clips"
    assert rows[new]["path"] is not None
    assert clips.used_bytes() <= 1_000_000


def test_evicted_clip_keeps_its_row(env, vclock):
    store, fs, make, tmp = env
    clips = make(quota=100 * KB, per_channel_share=1.0)
    first = _clip(clips, vclock, 462_000_000)
    _clip(clips, vclock, 462_012_500)
    _clip(clips, vclock, 462_025_000)
    row = _rows(store)[first]
    assert row["path"] is None and row["evicted_at"] is not None and row["bytes"] == 50 * KB
    assert len(list((tmp / "data" / "clips").rglob("*.opus"))) == 2


def test_pinned_clips_survive_until_free_space_is_below_ten_percent(env, vclock):
    store, fs, make, _ = env
    clips = make(quota=100 * KB, per_channel_share=1.0)
    pinned = _clip(clips, vclock, 462_000_000, pinned=True)
    for i in range(1, 5):
        _clip(clips, vclock, 462_000_000 + i * 12_500)
    assert _rows(store)[pinned]["evicted_at"] is None
    fs.free_frac = 0.09
    clips.enforce()
    assert _rows(store)[pinned]["evicted_at"] is not None


def test_one_channel_holds_at_most_ten_percent_of_the_quota(env, vclock):
    store, fs, make, _ = env
    clips = make(quota=1_000_000)
    for _ in range(5):
        _clip(clips, vclock, 462_562_500)
    assert clips.channel_bytes(462_562_500) <= 100 * KB
    assert sum(r["evicted_at"] is not None for r in _rows(store).values()) == 3


def test_low_free_space_evicts_even_under_quota(env, vclock):
    store, fs, make, _ = env
    clips = make(quota=10_000_000)
    a = _clip(clips, vclock, 462_000_000)
    _clip(clips, vclock, 462_012_500)
    fs.free_frac = 0.14
    clips.enforce()
    assert clips.used_bytes() == 0, "below 15% free every unpinned clip may go"
    assert _rows(store)[a]["evicted_at"] is not None


def test_below_five_percent_free_nothing_is_stored(env, vclock):
    store, fs, make, tmp = env
    clips = make()
    fs.free_frac = 0.04
    assert _clip(clips, vclock, 462_000_000) is None
    assert not _rows(store)
    assert not list((tmp / "stage").iterdir())
    assert clips.disk_low()


def test_staging_stays_under_stage_root_and_a_crash_leaves_no_clip(env, vclock):
    store, fs, make, tmp = env
    clips = make()
    w = clips.begin(462_000_000, vclock.now())
    w.write(voice(1.0))
    assert w.path.is_relative_to(tmp / "stage")
    assert not (tmp / "data" / "clips").exists() or not list((tmp / "data" / "clips").rglob("*"))
    # A crash here: a new ClipStore on the same roots cleans the partial staging file.
    make()
    assert not list((tmp / "stage").iterdir())
    assert not (tmp / "data" / "clips").exists() or not list(
        (tmp / "data" / "clips").rglob("*.opus")
    )


def test_clip_path_layout(env, vclock):
    store, fs, make, tmp = env
    clips = make()
    cid = _clip(clips, vclock, 462_562_500)
    path = Path(_rows(store)[cid]["path"])
    assert path.parts[-4:-1] == ("2026", "06", "01")
    assert path.name.startswith("462562500-") and path.suffix == ".opus"
    assert clips.path_for(cid).exists()


def test_wav_encoder_round_trip(tmp_path):
    enc = WavEncoder()
    out = tmp_path / "x.wav"
    enc.encode(voice(1.0), out)
    assert out.stat().st_size == 44 + 2 * 16_000


@pytest.mark.skipif(shutil.which("opusenc") is None, reason="opusenc not installed")
def test_real_opus_encoder_is_about_8_kbit(tmp_path):
    out = tmp_path / "x.opus"
    OpusEncoder().encode(voice(10.0), out)
    assert 5_000 < out.stat().st_size < 16_000
    assert np.isfinite(out.stat().st_size)


def test_a_finished_clip_row_is_committed_before_finish_returns(env, vclock):
    """A crash right after a clip is kept (the watchdog exits without flushing) must not
    leave a file the quota cannot see: the row is on disk as soon as finish() returns."""
    store, fs, make, tmp = env
    clips = make()
    ids = []
    for i in range(5):  # no clock advance, so the 10 s batch timer never fires
        w = clips.begin(462_000_000 + i * 12_500, vclock.now())
        w.write(voice(2.0))
        ids.append(clips.finish(w, {"class": "voice"}))
    other = Store.open(tmp / "db" / "scan.sqlite", vclock)  # another connection: disk only
    n = other.db.execute("SELECT COUNT(*) FROM clips WHERE path IS NOT NULL").fetchone()[0]
    other.db.close()
    assert n == len(ids)


def test_startup_deletes_clip_files_that_have_no_row(env, vclock):
    """Files left by an older build (or a lost batch) are removed, so the quota and the
    free-space eviction account for every byte under clips/."""
    store, fs, make, tmp = env
    clips = make()
    kept = _clip(clips, vclock, 462_000_000)
    day = clips.path_for(kept).parent
    orphan = day / "462012500-1780272000.opus"
    orphan.write_bytes(b"x" * 50 * KB)
    again = make()
    assert not orphan.exists()
    assert again.path_for(kept).exists()
    assert again.used_bytes() == 50 * KB
