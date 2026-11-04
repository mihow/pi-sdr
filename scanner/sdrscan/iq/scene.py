"""Scene descriptions for the synthetic IQ source.

A scene lists emitters (carriers, bursty NFM and AM voice, FM broadcast, spurs), per-band
noise floors, optional ADC clipping and failure events. Times are seconds from the scene
start, which is the clock time when the scene is built unless ``epoch`` is given. The
scripted bursts are the ground truth that scene tests compare the scanner against.
"""

from __future__ import annotations

import bisect
import dataclasses
import math
from pathlib import Path

import yaml

from ..clock import Clock

KINDS = {"carrier", "nfm_voice", "am_voice", "wfm", "spur"}
_BURSTY = {"nfm_voice", "am_voice"}


class SceneError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Burst:
    hz: int
    kind: str
    t_start: float  # absolute clock seconds
    t_end: float


@dataclasses.dataclass
class Emitter:
    kind: str
    hz: int | None = None
    offset_hz: int | None = None  # spur at a fixed offset from the tuner center
    level_db: float = -40.0
    deviation_hz: float = 2_500.0
    tone_hz: float | None = None  # pure tone audio instead of voice-like audio
    intervals: list[tuple[float, float]] = dataclasses.field(default_factory=list)
    always: bool = True

    def __post_init__(self) -> None:
        self._starts = [a for a, _ in self.intervals]

    def active_at(self, t_rel: float) -> bool:
        if self.always:
            return True
        i = bisect.bisect_right(self._starts, t_rel) - 1
        return i >= 0 and t_rel < self.intervals[i][1]

    def active_spans(self, t0: float, t1: float) -> list[tuple[float, float]]:
        """Active parts of [t0, t1) in scene seconds."""
        if self.always:
            return [(t0, t1)]
        i = max(0, bisect.bisect_right(self._starts, t0) - 1)
        out = []
        while i < len(self.intervals) and self.intervals[i][0] < t1:
            a, b = self.intervals[i]
            lo, hi = max(a, t0), min(b, t1)
            if lo < hi:
                out.append((lo, hi))
            i += 1
        return out


@dataclasses.dataclass
class Scene:
    t0: float
    emitters: list[Emitter]
    noise_default_db: float = -45.0
    noise_bands: list[tuple[int, int, float]] = dataclasses.field(default_factory=list)
    dc_db: float = -30.0
    clip: bool = False
    quantize: bool = True
    fail_at: list[tuple[float, float]] = dataclasses.field(default_factory=list)
    seed: int = 0

    @classmethod
    def load(cls, path: Path, clock: Clock, horizon_s: float = 8 * 86400) -> Scene:
        return cls.from_yaml(Path(path).read_text(), clock=clock, horizon_s=horizon_s)

    @classmethod
    def from_yaml(cls, text: str, clock: Clock, horizon_s: float = 8 * 86400) -> Scene:
        raw = yaml.safe_load(text) or {}
        known = {"epoch", "noise_floor", "dc_db", "clip", "quantize", "fail_at", "emitters", "seed"}
        unknown = set(raw) - known
        if unknown:
            raise SceneError(f"unknown scene keys {sorted(unknown)}")
        nf = raw.get("noise_floor", {}) or {}
        bands = [
            (int(b["lo_hz"]), int(b["hi_hz"]), float(b["level_db"])) for b in nf.get("bands", [])
        ]
        emitters = [_emitter(e, horizon_s) for e in raw.get("emitters", []) or []]
        fails = [(float(f["t_s"]), float(f["recover_s"])) for f in raw.get("fail_at", []) or []]
        return cls(
            t0=float(raw.get("epoch", clock.now())),
            emitters=emitters,
            noise_default_db=float(nf.get("default_db", -45.0)),
            noise_bands=bands,
            dc_db=float(raw.get("dc_db", -30.0)),
            clip=bool(raw.get("clip", False)),
            quantize=bool(raw.get("quantize", True)),
            fail_at=fails,
            seed=int(raw.get("seed", 0)),
        )

    def noise_db(self, hz: float) -> float:
        for lo, hi, level in self.noise_bands:
            if lo <= hz < hi:
                return level
        return self.noise_default_db

    def failing_at(self, t_rel: float) -> bool:
        return any(t <= t_rel < t + rec for t, rec in self.fail_at)

    def scripted_bursts(self, until_s: float) -> list[Burst]:
        """Every voice burst that starts before ``until_s`` scene seconds, by start time."""
        out = []
        for e in self.emitters:
            if e.kind not in _BURSTY or e.always or e.hz is None:
                continue
            for a, b in e.intervals:
                if a < until_s:
                    out.append(Burst(e.hz, e.kind, self.t0 + a, self.t0 + min(b, until_s)))
        return sorted(out, key=lambda b: (b.t_start, b.hz))

    def steady_hz(self) -> list[int]:
        """Frequencies of always-on emitters (carriers, spurs, broadcast)."""
        return sorted(e.hz for e in self.emitters if e.always and e.hz is not None)


def _emitter(raw: dict, horizon_s: float) -> Emitter:
    kind = raw.get("kind")
    if kind not in KINDS:
        raise SceneError(f"unknown emitter kind {kind!r}")
    allowed = {
        "kind",
        "hz",
        "offset_hz",
        "level_db",
        "deviation_hz",
        "tone_hz",
        "bursts",
        "schedule",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise SceneError(f"{kind}: unknown keys {sorted(unknown)}")
    intervals: list[tuple[float, float]] = [
        (float(a), float(a) + float(d)) for a, d in raw.get("bursts", []) or []
    ]
    sched = raw.get("schedule")
    if sched:
        every = float(sched["every_s"])
        dur = float(sched["dur_s"])
        t = float(sched.get("offset_s", 0.0)) + float(sched.get("from_s", 0.0))
        until = float(sched.get("until_s", horizon_s))
        hours = sched.get("hours_utc")  # optional hour-of-day filter, scene-relative
        while t < until:
            if hours is None or int(math.floor(t / 3600)) % 24 in hours:
                intervals.append((t, t + dur))
            t += every
    intervals.sort()
    for (_, b), (a2, _) in zip(intervals, intervals[1:], strict=False):
        if a2 < b:
            raise SceneError(f"{kind} at {raw.get('hz')}: bursts overlap")
    always = kind not in _BURSTY or not (raw.get("bursts") or sched)
    if kind == "spur" and raw.get("hz") is None and raw.get("offset_hz") is None:
        raise SceneError("spur needs hz or offset_hz")
    return Emitter(
        kind=kind,
        hz=int(raw["hz"]) if raw.get("hz") is not None else None,
        offset_hz=int(raw["offset_hz"]) if raw.get("offset_hz") is not None else None,
        level_db=float(raw.get("level_db", -40.0)),
        deviation_hz=float(raw.get("deviation_hz", 2_500.0)),
        tone_hz=float(raw["tone_hz"]) if raw.get("tone_hz") is not None else None,
        intervals=intervals,
        always=always,
    )
