"""Per-hop signal detection against the hop's own noise floor, plus a spur map.

Every threshold is an offset above the median of the hop's bins. Per-band floors differ by
about 16 dB at a real site, so a global squelch cannot work (v1 used one and found nothing).
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import math

import numpy as np

from .bandplan import BandPlan
from .config import DetectConfig
from .dsp.spectrum import welch_psd


@dataclasses.dataclass(frozen=True)
class Peak:
    hz: float
    width_hz: float
    level_db: float
    snr_db: float


@dataclasses.dataclass
class HopResult:
    peaks: list[Peak]
    floor_db: float
    mad_db: float
    clip_frac: float
    occupancy: dict[int, float]  # 1 MHz bin index (floor(hz / 1e6)) -> fraction of bins hot
    center_hz: int = 0
    lo_hz: int = 0  # analyzed span after cropping
    hi_hz: int = 0
    threshold_db: float = 10.0  # offset used at the hop center

    def to_record(self) -> dict:
        """The JSON form a detections file holds (one line per hop)."""
        return {
            "center_hz": self.center_hz,
            "lo_hz": self.lo_hz,
            "hi_hz": self.hi_hz,
            "floor_db": round(self.floor_db, 2),
            "mad_db": round(self.mad_db, 2),
            "threshold_db": self.threshold_db,
            "clip_frac": round(self.clip_frac, 5),
            "peaks": [
                {
                    "hz": round(p.hz, 1),
                    "width_hz": round(p.width_hz, 1),
                    "level_db": round(p.level_db, 2),
                    "snr_db": round(p.snr_db, 2),
                }
                for p in self.peaks
            ],
        }

    @classmethod
    def from_record(cls, rec: dict) -> HopResult:
        """Rebuild a hop from ``to_record`` output. Occupancy is not kept in the record."""
        return cls(
            peaks=[
                Peak(hz=p["hz"], width_hz=p["width_hz"], level_db=p["level_db"], snr_db=p["snr_db"])
                for p in rec["peaks"]
            ],
            floor_db=rec["floor_db"],
            mad_db=rec["mad_db"],
            clip_frac=rec["clip_frac"],
            occupancy={},
            center_hz=int(rec["center_hz"]),
            lo_hz=int(rec["lo_hz"]),
            hi_hz=int(rec["hi_hz"]),
            threshold_db=rec["threshold_db"],
        )


def clip_fraction(iq: np.ndarray, level: float) -> float:
    rail = (np.abs(iq.real) >= level) | (np.abs(iq.imag) >= level)
    return float(rail.mean()) if len(iq) else 0.0


def analyze(
    iq: np.ndarray,
    center_hz: int,
    rate: int,
    plan: BandPlan,
    cfg: DetectConfig = DetectConfig(),  # noqa: B008  (frozen dataclass, safe default)
) -> HopResult:
    psd = welch_psd(iq, nfft=cfg.nfft, navg=cfg.navg)
    return analyze_psd(psd, center_hz, rate, plan, cfg, clip_frac=clip_fraction(iq, cfg.clip_level))


def analyze_psd(
    psd_db: np.ndarray,
    center_hz: int,
    rate: int,
    plan: BandPlan,
    cfg: DetectConfig = DetectConfig(),  # noqa: B008
    clip_frac: float = 0.0,
) -> HopResult:
    nfft = len(psd_db)
    bin_hz = rate / nfft
    crop = int(round(nfft * cfg.crop_frac))
    mid = nfft // 2
    valid = np.zeros(nfft, dtype=bool)
    valid[crop : nfft - crop] = True
    valid[mid - cfg.dc_mask_bins : mid + cfg.dc_mask_bins + 1] = False
    vals = psd_db[valid]
    floor = _median(vals)
    # The spread is a health figure only, so a quarter of the bins estimate it well enough.
    mad = _median(np.abs(vals[::4] - floor))
    thr = _thresholds(plan, cfg, center_hz, bin_hz, nfft)
    hot = valid & (psd_db > floor + thr)
    lo_hz = int(round(center_hz + (crop - mid) * bin_hz))
    hi_hz = int(round(center_hz + (nfft - crop - mid) * bin_hz))
    peaks = _peaks(psd_db, hot, floor, center_hz, bin_hz, cfg)
    occupancy = _occupancy(hot, valid, center_hz, bin_hz)
    return HopResult(
        peaks=peaks,
        floor_db=floor,
        mad_db=mad,
        clip_frac=clip_frac,
        occupancy=occupancy,
        center_hz=int(center_hz),
        lo_hz=lo_hz,
        hi_hz=hi_hz,
        threshold_db=float(thr[mid]),
    )


def _median(x: np.ndarray) -> float:
    """Median by one partition (np.median carries overhead that shows in week simulations)."""
    n = len(x)
    part = np.partition(x, [(n - 1) // 2, n // 2])
    return float(0.5 * (part[(n - 1) // 2] + part[n // 2]))


def band_threshold(band, cfg: DetectConfig) -> float:
    """The SNR a peak in this band must exceed to be detected at all."""
    if band.threshold_db is not None:
        return band.threshold_db
    return cfg.fm_threshold_db if band.mode == "wfm" else cfg.threshold_db


def _thresholds(plan: BandPlan, cfg: DetectConfig, center: int, bin_hz: float, nfft: int):
    return _thresholds_cached(plan, cfg, int(center), float(bin_hz), nfft)


@functools.lru_cache(maxsize=2048)
def _thresholds_cached(plan: BandPlan, cfg: DetectConfig, center: int, bin_hz: float, nfft: int):
    freqs_lo = center - (nfft // 2) * bin_hz
    thr = np.full(nfft, cfg.threshold_db, dtype=np.float32)
    hi = freqs_lo + nfft * bin_hz
    i = max(0, bisect.bisect_right([b.lo_hz for b in plan.bands], freqs_lo) - 1)
    for band in plan.bands[i:]:
        if band.lo_hz >= hi:
            break
        if band.hi_hz <= freqs_lo:
            continue
        a = max(0, int(math.ceil((band.lo_hz - freqs_lo) / bin_hz)))
        b = min(nfft, int(math.ceil((band.hi_hz - freqs_lo) / bin_hz)))
        thr[a:b] = band_threshold(band, cfg)
    thr.flags.writeable = False
    return thr


def _peaks(psd_db, hot, floor, center_hz, bin_hz, cfg: DetectConfig) -> list[Peak]:
    if not hot.any():
        return []
    edges = np.diff(np.concatenate(([0], hot.view(np.int8), [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)  # exclusive
    gap = max(3, int(math.ceil(2_000 / bin_hz)))
    runs: list[list[int]] = []
    for s, e in zip(starts, ends, strict=True):
        if runs and s - runs[-1][1] <= gap:
            runs[-1][1] = int(e)
        else:
            runs.append([int(s), int(e)])
    nfft = len(psd_db)
    out = []
    for s, e in runs:
        width = (e - s) * bin_hz
        if width < cfg.min_width_hz:
            continue
        seg = psd_db[s:e]
        lin = 10 ** ((seg - floor) / 10)
        idx = np.arange(s, e)
        k = float(np.sum(idx * lin) / np.sum(lin))
        level = float(seg.max())
        out.append(
            Peak(
                hz=float(center_hz + (k - nfft // 2) * bin_hz),
                width_hz=float(width),
                level_db=level,
                snr_db=level - floor,
            )
        )
    return out


@functools.lru_cache(maxsize=2048)
def _mhz_segments(center_hz: int, bin_hz: float, nfft: int, valid_key: bytes):
    """Start index of each 1 MHz run among the valid bins, and the MHz of each run."""
    valid = np.frombuffer(valid_key, dtype=bool)
    freqs = center_hz + (np.arange(nfft) - nfft // 2) * bin_hz
    mhz = np.floor(freqs / 1e6).astype(np.int64)[valid]
    starts = np.flatnonzero(np.r_[True, mhz[1:] != mhz[:-1]])
    counts = np.diff(np.r_[starts, len(mhz)])
    return starts, counts, [int(m) for m in mhz[starts]]


def _occupancy(hot, valid, center_hz, bin_hz) -> dict[int, float]:
    starts, counts, mhzs = _mhz_segments(int(center_hz), float(bin_hz), len(hot), valid.tobytes())
    sums = np.add.reduceat(hot[valid].astype(np.int32), starts)
    return {m: float(s) / int(c) for m, s, c in zip(mhzs, sums, counts, strict=True)}


_SPUR_REFRESH_S = 300.0  # re-evaluate marks at most every 5 virtual minutes


@dataclasses.dataclass
class _SpurStat:
    first_t: float
    first_hop: int
    hits: int = 0
    visits: int = 0
    level_sum: float = 0.0
    level_sq: float = 0.0
    max_width: float = 0.0


class SpurMap:
    """Marks narrow, constant signals that are present in nearly every visit for a day.

    Two kinds are tracked: spurs at a fixed absolute frequency (clock harmonics) and spurs
    at a fixed offset from the tuner center (which appear in every hop). A marked spur
    produces no tracks or transmissions.
    """

    def __init__(
        self,
        min_hours: float = 24.0,
        hit_frac: float = 0.9,
        max_width_hz: float = 3_000.0,
        max_level_sd_db: float = 2.0,
        key_hz: int = 1_000,
    ) -> None:
        self.min_s = min_hours * 3600
        self.hit_frac = hit_frac
        self.max_width = max_width_hz
        self.max_sd = max_level_sd_db
        self.key_hz = key_hz
        self._abs: dict[int, _SpurStat] = {}
        self._abs_keys: list[int] = []
        self._off: dict[int, _SpurStat] = {}
        self._hops = 0
        self._t = 0.0
        self._marked_abs: set[int] = set()
        self._marked_off: set[int] = set()
        self._refreshed_t: float | None = None
        self._start_t: float | None = None
        self._seed_abs: set[int] = set()
        self._seed_off: set[int] = set()

    def seed(self, abs_hz: list[int], offset_hz: list[int]) -> None:
        """Carry marks over a restart (from ``marked()``). Seeds apply at once and are
        dropped once this map has ``min_hours`` of its own evidence."""
        self._seed_abs = {round(h / self.key_hz) for h in abs_hz}
        self._seed_off = {round(h / self.key_hz) for h in offset_hz}

    def update(self, r: HopResult, t: float) -> None:
        self._hops += 1
        self._t = t
        if self._start_t is None:
            self._start_t = t
        lo = bisect.bisect_left(self._abs_keys, r.lo_hz // self.key_hz)
        hi = bisect.bisect_right(self._abs_keys, r.hi_hz // self.key_hz)
        for key in self._abs_keys[lo:hi]:
            self._abs[key].visits += 1
        seen_abs: set[int] = set()
        seen_off: set[int] = set()
        for p in r.peaks:
            if p.width_hz > self.max_width:
                continue
            ka = self._key(self._abs, round(p.hz / self.key_hz))
            if ka not in seen_abs:
                seen_abs.add(ka)
                st = self._abs.get(ka)
                if st is None:
                    st = self._abs[ka] = _SpurStat(first_t=t, first_hop=self._hops, visits=1)
                    bisect.insort(self._abs_keys, ka)
                self._hit(st, p)
            ko = self._key(self._off, round((p.hz - r.center_hz) / self.key_hz))
            if ko not in seen_off:
                seen_off.add(ko)
                st = self._off.get(ko)
                if st is None:
                    st = self._off[ko] = _SpurStat(first_t=t, first_hop=self._hops)
                self._hit(st, p)

    def is_spur(self, hz: float, center_hz: int | None = None) -> bool:
        if self._refreshed_t is None or self._t - self._refreshed_t >= _SPUR_REFRESH_S:
            self._refresh()
        k = round(hz / self.key_hz)
        marked_abs = self._marked_abs | self._seed_abs
        if any(k + d in marked_abs for d in (-1, 0, 1)):
            return True
        if center_hz is not None:
            ko = round((hz - center_hz) / self.key_hz)
            marked_off = self._marked_off | self._seed_off
            if any(ko + d in marked_off for d in (-1, 0, 1)):
                return True
        return False

    def marked(self) -> dict[str, list[int]]:
        self._refresh()
        return {
            "abs_hz": sorted(k * self.key_hz for k in self._marked_abs | self._seed_abs),
            "offset_hz": sorted(k * self.key_hz for k in self._marked_off | self._seed_off),
        }

    @staticmethod
    def _key(table: dict[int, _SpurStat], k: int) -> int:
        for d in (0, -1, 1):
            if k + d in table:
                return k + d
        return k

    @staticmethod
    def _hit(st: _SpurStat, p: Peak) -> None:
        st.hits += 1
        st.level_sum += p.level_db
        st.level_sq += p.level_db**2
        st.max_width = max(st.max_width, p.width_hz)

    def _qualifies(self, st: _SpurStat, visits: int) -> bool:
        if self._t - st.first_t < self.min_s or visits <= 0:
            return False
        if st.hits / visits < self.hit_frac or st.max_width > self.max_width:
            return False
        mean = st.level_sum / st.hits
        var = max(0.0, st.level_sq / st.hits - mean**2)
        return math.sqrt(var) <= self.max_sd

    def _refresh(self) -> None:
        self._refreshed_t = self._t
        if self._start_t is not None and self._t - self._start_t >= self.min_s:
            self._seed_abs, self._seed_off = set(), set()  # own evidence decides from here
        self._marked_abs = {k for k, st in self._abs.items() if self._qualifies(st, st.visits)}
        self._marked_off = {
            k for k, st in self._off.items() if self._qualifies(st, self._hops - st.first_hop + 1)
        }
        # Forget sparse candidates after a day, so noise does not grow the map forever.
        stale = [
            k
            for k, st in self._abs.items()
            if self._t - st.first_t > self.min_s and st.hits < 0.5 * max(1, st.visits)
        ]
        for k in stale:
            del self._abs[k]
            self._abs_keys.remove(k)
        for k in [
            k
            for k, st in self._off.items()
            if self._t - st.first_t > self.min_s
            and st.hits < 0.5 * max(1, self._hops - st.first_hop + 1)
        ]:
            del self._off[k]
