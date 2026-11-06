"""Band plan and channel model.

Every frequency the scanner stores is an integer number of Hz on the raster of its band.
Display names are decoration on top of that key: two channels may share a name, but never
a Hz key (duplicate names corrupted an earlier analysis).
"""

from __future__ import annotations

import bisect
import dataclasses
import math
from pathlib import Path

import yaml

MODES = {"nfm", "am", "wfm", "digital", "unknown"}
TIERS = {"A", "B"}
POLICIES = {"dwell", "occupancy", "decoder", "ignore"}
# Hop centers move off the raster by up to half a raster step (at most this much), so the
# tuner's DC spike lands between channels. Tiling keeps this much margin per window.
_MAX_NUDGE_HZ = 25_000


class BandPlanError(ValueError):
    """The band plan file is invalid."""


@dataclasses.dataclass(frozen=True)
class Band:
    id: str
    lo_hz: int
    hi_hz: int
    name: str = ""
    mode: str = "unknown"
    raster_hz: int = 12_500
    raster_origin_hz: int = 0
    bw_hz: int = 12_500
    gain_db: float = 32.8
    # Offset above the per-hop floor. None means the detector default (DetectConfig).
    threshold_db: float | None = None
    tier: str = "B"
    policy: str = "occupancy"
    deviation_hz: int = 2_500

    def snap(self, hz: float) -> int:
        r, o = self.raster_hz, self.raster_origin_hz
        return int(o + round((hz - o) / r) * r)

    @property
    def voice_capable(self) -> bool:
        return self.policy == "dwell" and self.mode in {"nfm", "am"}


UNPLANNED = Band(id="unplanned", lo_hz=0, hi_hz=0, name="Unplanned")


@dataclasses.dataclass(frozen=True)
class Channel:
    hz: int
    band: str
    mode: str
    bw_hz: int
    name: str
    deviation_hz: int = 2_500
    tier: str = "B"
    policy: str = "occupancy"

    @property
    def voice_capable(self) -> bool:
        return self.policy == "dwell" and self.mode in {"nfm", "am"}


class BandPlan:
    def __init__(self, bands: list[Band], labels: dict[int, str] | None = None) -> None:
        self.bands = sorted(bands, key=lambda b: b.lo_hz)
        _check(self.bands)
        self._los = [b.lo_hz for b in self.bands]
        self._by_id = {b.id: b for b in self.bands}
        self.labels = dict(labels or {})

    @classmethod
    def load(cls, path: Path, labels: Path | None = None) -> BandPlan:
        raw = yaml.safe_load(Path(path).read_text()) or {}
        defaults = raw.get("defaults", {}) or {}
        field_names = {f.name for f in dataclasses.fields(Band)}
        bands = []
        for i, entry in enumerate(raw.get("bands", []) or []):
            merged = {**defaults, **entry}
            unknown = set(merged) - field_names
            if unknown:
                raise BandPlanError(f"band {i}: unknown keys {sorted(unknown)}")
            try:
                band = Band(**merged)
            except TypeError as exc:
                raise BandPlanError(f"band {i}: {exc}") from exc
            bands.append(band)
        # Standard channel numbers from the plan come first; a local labels file overrides them.
        label_map: dict[int, str] = {int(k): str(v) for k, v in (raw.get("channels") or {}).items()}
        if labels is not None:
            loaded = yaml.safe_load(Path(labels).read_text()) or {}
            label_map.update({int(k): str(v) for k, v in loaded.items()})
        return cls(bands, label_map)

    def band(self, band_id: str) -> Band:
        return self._by_id[band_id] if band_id != "unplanned" else UNPLANNED

    def band_for(self, hz: float) -> Band:
        i = bisect.bisect_right(self._los, hz) - 1
        if i >= 0 and self.bands[i].lo_hz <= hz < self.bands[i].hi_hz:
            return self.bands[i]
        return UNPLANNED

    def channel_for(self, hz: float) -> Channel:
        band = self.band_for(hz)
        key = band.snap(hz)
        name = self.labels.get(key) or f"{key / 1e6:.4f} MHz"
        return Channel(
            hz=key,
            band=band.id,
            mode=band.mode,
            bw_hz=band.bw_hz,
            name=name,
            deviation_hz=band.deviation_hz,
            tier=band.tier,
            policy=band.policy,
        )

    def hops(self, tier: str, usable_hz: int) -> list[int]:
        """Hop (tuner) centers that cover every swept band of ``tier``, lowest first.

        Bands closer together than one window are merged, then each merged span is tiled
        with windows of ``usable_hz`` minus a small margin. Each center is then moved to
        the midpoint between two raster points of its band, so the DC spike of the tuner
        never sits on a channel. Ascending order means one pass crosses each V4 input
        switch (28.8 MHz, 250 MHz) once.
        """
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        spans = sorted(
            (b.lo_hz, b.hi_hz) for b in self.bands if b.tier == tier and b.policy != "ignore"
        )
        merged: list[list[int]] = []
        for lo, hi in spans:
            if merged and lo - merged[-1][1] < usable_hz:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        step = usable_hz - 2 * _MAX_NUDGE_HZ
        centers: list[int] = []
        for lo, hi in merged:
            n = max(1, math.ceil((hi - lo) / step))
            first = (lo + hi) / 2 - (n - 1) * step / 2
            for k in range(n):
                centers.append(self._off_raster(int(round(first + k * step))))
        return sorted(set(centers))

    def _off_raster(self, hz: int) -> int:
        band = self.band_for(hz)
        r = band.raster_hz
        if r > 2 * _MAX_NUDGE_HZ:
            return hz
        o = band.raster_origin_hz
        below = o + math.floor((hz - o) / r) * r
        return int(below + r // 2)


def _check(bands: list[Band]) -> None:
    for b in bands:
        if b.lo_hz >= b.hi_hz:
            raise BandPlanError(f"{b.id}: lo_hz must be below hi_hz")
        if b.mode not in MODES:
            raise BandPlanError(f"{b.id}: unknown mode {b.mode!r}")
        if b.tier not in TIERS:
            raise BandPlanError(f"{b.id}: unknown tier {b.tier!r}")
        if b.policy not in POLICIES:
            raise BandPlanError(f"{b.id}: unknown policy {b.policy!r}")
        if b.raster_hz <= 0:
            raise BandPlanError(f"{b.id}: raster_hz must be positive")
    for a, b in zip(bands, bands[1:], strict=False):
        if a.hi_hz > b.lo_hz:
            raise BandPlanError(f"bands {a.id} and {b.id} overlap")
    ids = [b.id for b in bands]
    if len(ids) != len(set(ids)):
        raise BandPlanError("band ids must be unique")
