"""Pick the tuner's next job: survey hops, dwells on active voice, calibration, idle pacing.

The plan works in cycles. Each cycle must visit every tier A hop within the revisit target.
Inside a cycle, the time not needed for the remaining tier A hops (the slack) goes first to
a dwell slice, then to calibration, then to tier B hops. A dwell session on one window may
be split into several slices; it ends after ``dwell_max_s`` of dwell time and the window
then cools down. A token bucket caps the dwell share: tokens accrue at the share rate, so
cumulative dwell time can never exceed that share of elapsed time.
"""

from __future__ import annotations

import bisect
import collections
import dataclasses
import typing

import numpy as np

from .bandplan import BandPlan, Channel
from .clock import Clock
from .config import DutyConfig

SURVEY = "SURVEY"
DWELL = "DWELL"
CALIBRATE = "CALIBRATE"
EXTERNAL = "EXTERNAL"
IDLE = "IDLE"

USABLE_HZ = 1_920_000  # 2.4 MHz minus 10% cropped on each side
_PLAN_MARGIN = 0.9  # plan cycles at 90% of the revisit target
_MIN_SLICE_S = 1.0
_HOP_EST_S = 0.13  # first estimate of one survey hop (read plus settle) until measured
_SETTLE_RESERVE_S = 0.1  # dwell tokens kept back for the retune before a slice

# Dwell share per governor mode; None means the configured dwell_max_share.
_DWELL_SHARE = {"normal": None, "hot": 0.25, "very_hot": 0.0, "critical": 0.0}
_PACE_S = {"very_hot": 0.5}


@dataclasses.dataclass(frozen=True)
class Job:
    kind: str
    center_hz: int = 0
    duration_s: float = 0.0
    channels: tuple[Channel, ...] = ()
    tier: str | None = None
    gain_db: float = 0.0
    session: int = 0  # dwell session id; slices of one session share it


class _Tracks(typing.Protocol):
    def active(self, policy: str = "dwell", include_candidates: bool = False) -> list: ...


class Scheduler:
    def __init__(
        self,
        plan: BandPlan,
        cfg: DutyConfig,
        clock: Clock,
        usable_hz: int = USABLE_HZ,
    ) -> None:
        self.plan = plan
        self.cfg = cfg
        self.clock = clock
        self.usable_hz = usable_hz
        self.hops_a = plan.hops("A", usable_hz)
        self.hops_b = plan.hops("B", usable_hz)
        self._all_hops = sorted(self.hops_a + self.hops_b)
        self._ia = 0
        self._ib = 0
        self._icalib = 0
        t = clock.now()
        self._cycle_start = t
        self._last_now: float | None = None
        self._last_kind: str | None = None
        self._last_survey_start = -1e18
        self._hop_t = _HOP_EST_S
        self._totals: collections.Counter[str] = collections.Counter()
        self._tokens = 0.0
        self._last_visit: dict[int, float] = {}
        self._revisits: collections.deque[float] = collections.deque(maxlen=50_000)
        self._b_wrap_t: float | None = None
        self._b_pass_s: float | None = None
        self._last_cal = t
        self._session: dict | None = None
        self._session_id = 0
        self._cooldown: dict[int, float] = {}
        self._win_cache: tuple | None = None
        self._a_visits_hour: collections.Counter[int] = collections.Counter()
        self._hour_elapsed: collections.Counter[int] = collections.Counter()

    # -- public ------------------------------------------------------------------------
    def next_job(self, now: float, tracks: _Tracks | None, mode: str) -> Job:
        share = _DWELL_SHARE.get(mode)
        share = self.cfg.dwell_max_share if share is None else share
        self._account(now, share)
        if mode == "critical":
            return self._paced(now, self.cfg.critical_hop_interval_s, tier_a_only=True)
        if mode in _PACE_S:
            return self._paced(now, _PACE_S[mode], tier_a_only=False)
        slack = self._slack(now)
        if share > 0 and tracks is not None and slack >= _MIN_SLICE_S + self._hop_t:
            job = self._dwell(now, tracks, slack - self._hop_t)
            if job is not None:
                return self._issue(job, now)
        if self._session is not None and self._session_ended(
            now, self._windows(tracks, now) if tracks is not None else None
        ):
            self._session = None
        if now - self._last_cal >= self.cfg.calibrate_interval_s and slack >= (
            self.cfg.calibrate_s + 2 * self._hop_t
        ):
            self._last_cal = now
            hop = self._all_hops[self._icalib % len(self._all_hops)]
            self._icalib += 1
            return self._issue(self._job(CALIBRATE, hop, self.cfg.calibrate_s), now)
        if slack >= 2 * self._hop_t and self.hops_b:
            return self._issue(self._survey_b(), now)
        return self._issue(self._survey_a(now), now)

    def budget_report(self) -> dict:
        total = sum(self._totals.values()) or 1.0
        rev = np.asarray(self._revisits) if self._revisits else np.zeros(1)
        return {
            "share": {k: v / total for k, v in sorted(self._totals.items())},
            "seconds": dict(sorted(self._totals.items())),
            "tier_a_hops": len(self.hops_a),
            "tier_b_hops": len(self.hops_b),
            "tier_a_revisit_p95_s": float(np.percentile(rev, 95)),
            "tier_a_revisit_max_s": float(rev.max()),
            "tier_b_pass_s": self._b_pass_s,
            "hop_s": self._hop_t,
            "tier_a_cycle_s": self._cycle_target(),
        }

    def coverage(self, hour_ts: int) -> float | None:
        """Share (percent) of planned tier A revisits that happened in that hour.

        None when the scheduler ran for less than a minute of that hour, because a figure
        from a few hops would read as a gap in coverage.
        """
        elapsed = self._hour_elapsed.get(int(hour_ts), 0.0)
        if elapsed < 60.0:
            return None
        planned = elapsed / self.cfg.tier_a_revisit_s * len(self.hops_a)
        return min(100.0, 100.0 * self._a_visits_hour.get(int(hour_ts), 0) / planned)

    # -- internals ---------------------------------------------------------------------
    def _account(self, now: float, share: float) -> None:
        if self._last_now is not None and self._last_kind is not None:
            dt = max(0.0, now - self._last_now)
            self._totals[self._last_kind] += dt
            self._hour_elapsed[int(self._last_now // 3600 * 3600)] += dt
            self._tokens = min(self.cfg.dwell_max_s, self._tokens + dt * share)
            if self._last_kind == DWELL:
                self._tokens -= dt  # charged on elapsed time, so settle counts too
            if self._last_kind == SURVEY:
                self._hop_t = 0.9 * self._hop_t + 0.1 * dt
        self._last_now = now

    def _issue(self, job: Job, now: float) -> Job:
        self._last_kind = job.kind
        return job

    def _job(self, kind: str, hop: int, duration: float, tier: str | None = None) -> Job:
        return Job(
            kind=kind,
            center_hz=hop,
            duration_s=duration,
            tier=tier,
            gain_db=self.plan.band_for(hop).gain_db,
        )

    def _slack(self, now: float) -> float:
        target = self._cycle_target()
        remaining = len(self.hops_a) - self._ia
        return self._cycle_start + target - now - remaining * self._hop_t

    def _cycle_target(self) -> float:
        """Planned length of one tier A sweep, in seconds.

        Normally 90% of the revisit target. When the hops are slow enough that the sweep
        alone would fill that, the period stretches so dwell_min_share and calibration stay
        free; otherwise no dwell could ever start and voice would never be recorded.
        """
        sweep = len(self.hops_a) * self._hop_t
        free = 1.0 - self.cfg.dwell_min_share - self.cfg.calibrate_share
        return max(self.cfg.tier_a_revisit_s * _PLAN_MARGIN, sweep / max(free, 0.05))

    def _visit_a(self, hop: int, now: float) -> None:
        prev = self._last_visit.get(hop)
        if prev is not None:
            self._revisits.append(now - prev)
        self._last_visit[hop] = now
        self._a_visits_hour[int(now // 3600 * 3600)] += 1

    def _survey_a(self, now: float) -> Job:
        hop = self.hops_a[self._ia]
        self._visit_a(hop, now)
        self._ia += 1
        if self._ia >= len(self.hops_a):
            self._ia = 0
            self._cycle_start = now + self._hop_t
        self._last_survey_start = now
        return self._job(SURVEY, hop, self.cfg.survey_hop_s, tier="A")

    def _survey_b(self) -> Job:
        hop = self.hops_b[self._ib]
        self._ib += 1
        if self._ib >= len(self.hops_b):
            self._ib = 0
            now = self._last_now or 0.0
            if self._b_wrap_t is not None:
                self._b_pass_s = now - self._b_wrap_t
            self._b_wrap_t = now
        return self._job(SURVEY, hop, self.cfg.survey_hop_s, tier="B")

    def _paced(self, now: float, interval: float, tier_a_only: bool) -> Job:
        wait = self._last_survey_start + interval - now
        if wait > 1e-9:
            return self._issue(Job(kind=IDLE, duration_s=wait), now)
        if tier_a_only or self._ia % 2 == 0 or not self.hops_b:
            job = self._survey_a(now)
        else:
            job = self._survey_b()
            self._last_survey_start = now
        return self._issue(job, now)

    def _windows(self, tracks: _Tracks, now: float) -> dict[int, list[Channel]]:
        """Survey hops that cover at least one voice track and are not cooling down."""
        version = getattr(tracks, "version", None)
        if (
            version is None
            or self._win_cache is None
            or self._win_cache[0]
            != (
                id(tracks),
                version,
            )
        ):
            half = self.usable_hz // 2
            raw: dict[int, list[Channel]] = {}
            for tr in tracks.active("dwell", include_candidates=True):
                i = bisect.bisect_left(self._all_hops, tr.hz - half)
                while i < len(self._all_hops) and self._all_hops[i] <= tr.hz + half:
                    hop = self._all_hops[i]
                    if abs(tr.hz - hop) < half:
                        raw.setdefault(hop, []).append(tr.channel)
                    i += 1
            self._win_cache = ((id(tracks), version), raw)
        raw = self._win_cache[1]
        return {h: c for h, c in raw.items() if self._cooldown.get(h, -1e18) <= now}

    def _session_ended(self, now: float, windows: dict[int, list[Channel]] | None) -> bool:
        s = self._session
        assert s is not None
        if s["used"] >= self.cfg.dwell_max_s - 1e-9:
            self._cooldown[s["hop"]] = now + self.cfg.dwell_cooldown_s
            return True
        return windows is None or s["hop"] not in windows

    def _dwell(self, now: float, tracks: _Tracks, room: float) -> Job | None:
        windows = self._windows(tracks, now)
        if self._session is not None and self._session_ended(now, windows):
            self._session = None
            windows = self._windows(tracks, now)  # the ended window is now cooling down
        if not windows:
            return None
        if self._session is None:
            hop = max(windows, key=lambda h: (len(windows[h]), -h))
            self._session_id += 1
            self._session = {"hop": hop, "used": 0.0, "id": self._session_id}
        s = self._session
        remaining = self.cfg.dwell_max_s - s["used"]
        slice_s = min(room, self._tokens - _SETTLE_RESERVE_S, remaining)
        if slice_s < _MIN_SLICE_S and slice_s < remaining:
            return None
        if slice_s <= 0:
            return None
        s["used"] += slice_s
        chans = tuple(sorted(windows[s["hop"]], key=lambda c: c.hz))
        job = self._job(DWELL, s["hop"], slice_s)
        return dataclasses.replace(job, channels=chans, session=s["id"])
