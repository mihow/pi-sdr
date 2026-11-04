"""Rolling clip store: recent voice recordings under a strict byte quota.

A clip is staged as 16-bit PCM under ``stage_root`` (tmpfs on the Pi), so a partial
recording never touches the SD card. ``finish`` encodes it (Opus at 8 kbit/s by default),
copies it into ``clips/YYYY/MM/DD/<hz>-<ts>.<ext>`` through a ``.partial`` name and an
atomic rename, and then enforces the quota (spec section 9):

1. While the clips use more than the quota, or the filesystem has less than 15% free,
   delete the oldest clip of the lowest tier. Tiers: 0 noise or low confidence, 1 voice,
   2 pinned. Pinned clips go only below 10% free.
2. No channel may hold more than 10% of the quota.
3. An evicted clip keeps its row with ``path = NULL`` and ``evicted_at`` set, so counts
   and summaries stay correct.

Below 5% free, ``finish`` stores nothing and ``disk_low()`` is true.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import os
import shutil
import subprocess
import tempfile
import typing
import wave
from pathlib import Path

import numpy as np

from . import AUDIO_RATE
from .clock import Clock
from .config import QuotaConfig
from .store import Store

MAX_CLIP_S = 120.0
TIER_NOISE, TIER_VOICE, TIER_PINNED = 0, 1, 2


class Encoder(typing.Protocol):
    codec: str
    ext: str

    def encode(self, pcm: np.ndarray, out: Path) -> None: ...


def _write_wav(path: Path, pcm16: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        w.writeframes(pcm16.astype("<i2").tobytes())


def to_pcm16(pcm: np.ndarray) -> np.ndarray:
    return (np.clip(np.asarray(pcm, dtype=np.float64), -1, 1) * 32767).astype("<i2")


class OpusEncoder:
    """``opusenc --bitrate 8 --framesize 60 --speech``: about 60 KB per minute of speech."""

    codec = "opus"
    ext = "opus"

    def __init__(self, binary: str = "opusenc", bitrate_kbps: int = 8) -> None:
        self.binary = binary
        self.bitrate = bitrate_kbps

    def encode(self, pcm: np.ndarray, out: Path) -> None:
        with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
            wav = Path(tmp) / "in.wav"
            _write_wav(wav, to_pcm16(pcm))
            subprocess.run(
                [
                    self.binary,
                    "--quiet",
                    "--bitrate",
                    str(self.bitrate),
                    "--framesize",
                    "60",
                    "--speech",
                    str(wav),
                    str(out),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )


class WavEncoder:
    """Uncompressed fallback for hosts without opusenc (about 32 KB per second)."""

    codec = "wav"
    ext = "wav"

    def encode(self, pcm: np.ndarray, out: Path) -> None:
        _write_wav(out, to_pcm16(pcm))


class FakeEncoder:
    """Writes exactly ``nbytes`` bytes, so quota tests can count clips."""

    codec = "opus"
    ext = "opus"

    def __init__(self, nbytes: int) -> None:
        self.nbytes = nbytes

    def encode(self, pcm: np.ndarray, out: Path) -> None:
        out.write_bytes(b"\0" * self.nbytes)


def default_encoder() -> Encoder:
    return OpusEncoder() if shutil.which("opusenc") else WavEncoder()


class ClipWriter:
    def __init__(self, hz: int, t_start: float, path: Path) -> None:
        self.hz = int(hz)
        self.t_start = t_start
        self.path = path
        self.samples = 0
        self.pieces = 0
        self._fh: typing.BinaryIO | None = open(path, "wb")  # noqa: SIM115 - kept open

    @property
    def seconds(self) -> float:
        return self.samples / AUDIO_RATE

    def write(self, pcm: np.ndarray) -> None:
        if self._fh is None or self.seconds >= MAX_CLIP_S:
            return
        room = int(MAX_CLIP_S * AUDIO_RATE) - self.samples
        data = to_pcm16(pcm[:room])
        self._fh.write(data.tobytes())
        self.samples += len(data)
        self.pieces += 1

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def read(self) -> np.ndarray:
        self.close()
        return np.frombuffer(self.path.read_bytes(), dtype="<i2").astype(np.float32) / 32767


@dataclasses.dataclass
class _Clip:
    id: int
    hz: int
    t_start: int
    nbytes: int
    tier: int
    path: str


class ClipStore:
    def __init__(
        self,
        store: Store,
        cfg: QuotaConfig,
        *,
        data_root: Path,
        stage_root: Path,
        clock: Clock,
        encoder: Encoder | None = None,
        statvfs: typing.Callable[[str], typing.Any] = os.statvfs,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.data_root = Path(data_root)
        self.clip_root = self.data_root / "clips"
        self.stage_root = Path(stage_root)
        self.clock = clock
        self.encoder = encoder or default_encoder()
        self._statvfs = statvfs
        self.stage_root.mkdir(parents=True, exist_ok=True)
        self._cleanup()
        store.flush()
        self._clips: dict[int, _Clip] = {
            r["id"]: _Clip(r["id"], r["hz"], r["t_start"], r["bytes"], r["tier"], r["path"])
            for r in store.db.execute(
                "SELECT id, hz, t_start, bytes, tier, path FROM clips WHERE path IS NOT NULL"
            )
        }

    # -- recording ---------------------------------------------------------------------
    def begin(self, hz: int, t: float) -> ClipWriter:
        name = f"{int(hz)}-{int(t)}-{os.getpid()}.pcm"
        return ClipWriter(hz, t, self.stage_root / name)

    def abort(self, w: ClipWriter) -> None:
        w.close()
        w.path.unlink(missing_ok=True)

    def finish(self, w: ClipWriter, meta: dict) -> int | None:
        """Encode and keep the clip. ``meta``: class, confidence (optional), pinned."""
        pcm = w.read()
        try:
            if self.disk_low() or w.samples == 0:
                return None
            ts = datetime.datetime.fromtimestamp(int(w.t_start), datetime.UTC)
            day_dir = self.clip_root / f"{ts:%Y}" / f"{ts:%m}" / f"{ts:%d}"
            day_dir.mkdir(parents=True, exist_ok=True)
            final = day_dir / f"{w.hz}-{int(w.t_start)}.{self.encoder.ext}"
            encoded = w.path.with_suffix("." + self.encoder.ext)
            self.encoder.encode(pcm, encoded)
            partial = final.with_name(final.name + ".partial")
            shutil.copyfile(encoded, partial)
            os.replace(partial, final)
            encoded.unlink(missing_ok=True)
        finally:
            w.path.unlink(missing_ok=True)
        data = final.read_bytes()
        pinned = bool(meta.get("pinned"))
        tier = self._tier(meta)
        rel = str(final.relative_to(self.data_root))
        clip_id = self.store.add_clip(
            hz=w.hz,
            t_start=w.t_start,
            dur_ms=int(round(w.seconds * 1000)),
            nbytes=len(data),
            codec=self.encoder.codec,
            path=rel,
            tier=tier,
            sha256=hashlib.sha256(data).hexdigest(),
            pinned=pinned,
        )
        self._clips[clip_id] = _Clip(clip_id, w.hz, int(w.t_start), len(data), tier, rel)
        self.enforce()
        return clip_id if clip_id in self._clips else None

    def set_tier(self, clip_id: int, meta: dict) -> None:
        """Re-rank a kept clip, for example after its transcript arrives."""
        c = self._clips.get(clip_id)
        if c is not None and c.tier != TIER_PINNED:
            c.tier = self._tier(meta)
            self.store.update_clip_tier(clip_id, c.tier)

    # -- quota -------------------------------------------------------------------------
    def used_bytes(self) -> int:
        return sum(c.nbytes for c in self._clips.values())

    def channel_bytes(self, hz: int) -> int:
        return sum(c.nbytes for c in self._clips.values() if c.hz == int(hz))

    def free_frac(self) -> float:
        self.data_root.mkdir(parents=True, exist_ok=True)
        st = self._statvfs(str(self.data_root))
        return st.f_bavail / st.f_blocks if st.f_blocks else 0.0

    def disk_low(self) -> bool:
        return self.free_frac() < self.cfg.free_stop_frac

    def path_for(self, clip_id: int) -> Path | None:
        c = self._clips.get(clip_id)
        return None if c is None else self.data_root / c.path

    def clip_info(self, clip_id: int) -> dict | None:
        c = self._clips.get(clip_id)
        return None if c is None else dataclasses.asdict(c)

    def enforce(self) -> list[int]:
        evicted: list[int] = []
        cap = self.cfg.per_channel_share * self.cfg.clip_quota_bytes
        by_channel: dict[int, int] = {}
        for c in self._clips.values():
            by_channel[c.hz] = by_channel.get(c.hz, 0) + c.nbytes
        for hz, used in by_channel.items():
            while used > cap:
                victim = self._victim(hz=hz, allow_pinned=False)
                if victim is None:
                    break
                used -= victim.nbytes
                evicted.append(self._evict(victim))
        while True:
            free = self.free_frac()
            if self.used_bytes() <= self.cfg.clip_quota_bytes and free >= self.cfg.free_evict_frac:
                break
            victim = self._victim(allow_pinned=free < self.cfg.free_pinned_frac)
            if victim is None:
                break
            evicted.append(self._evict(victim))
        return evicted

    # -- internals ---------------------------------------------------------------------
    def _tier(self, meta: dict) -> int:
        if meta.get("pinned"):
            return TIER_PINNED
        conf = meta.get("confidence")
        if meta.get("class") != "voice" or (conf is not None and conf < 0.6):
            return TIER_NOISE
        return TIER_VOICE

    def _victim(self, hz: int | None = None, allow_pinned: bool = False) -> _Clip | None:
        pool = [
            c
            for c in self._clips.values()
            if (hz is None or c.hz == hz) and (allow_pinned or c.tier != TIER_PINNED)
        ]
        return min(pool, key=lambda c: (c.tier, c.t_start, c.id)) if pool else None

    def _evict(self, c: _Clip) -> int:
        (self.data_root / c.path).unlink(missing_ok=True)
        del self._clips[c.id]
        self.store.evict_clip(c.id, self.clock.now())
        return c.id

    def _cleanup(self) -> None:
        """Remove staging files and half-copied clips left by a crash."""
        for p in self.stage_root.iterdir():
            if p.is_file():
                p.unlink(missing_ok=True)
        if self.clip_root.exists():
            for p in self.clip_root.rglob("*.partial"):
                p.unlink(missing_ok=True)
