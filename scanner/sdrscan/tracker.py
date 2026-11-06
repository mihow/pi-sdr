"""One state machine per channel (integer Hz) that turns peaks into transmissions.

    IDLE -> CANDIDATE (seen once)
         -> ACTIVE (seen in 2 of the last 3 survey visits, or one strong dwell reading)
         -> closed (1 survey miss, or ``hang_s`` of dwell readings without it)

A channel whose survey duty cycle stays above ``always_on_duty`` for an hour is an
always-on carrier: it counts as occupancy and never produces transmissions.
"""

from __future__ import annotations

import bisect
import collections
import dataclasses
import enum
import statistics

from .bandplan import BandPlan, Channel
from .clock import Clock
from .config import DetectConfig
from .detect import Peak

_TRACKED_POLICIES = {"dwell", "decoder"}
_DUTY_WINDOW_S = 3600.0
_DUTY_BUCKET_S = 60.0
_ALWAYS_ON_CLEAR = 0.5
_MAX_SNR_SAMPLES = 600


class TrackState(enum.Enum):
    IDLE = "idle"
    CANDIDATE = "candidate"
    ACTIVE = "active"


@dataclasses.dataclass
class Transmission:
    hz: int
    band: str
    mode: str
    t_start: float
    t_end: float
    dur_ms: int
    peak_snr_db: float
    med_snr_db: float
    floor_db: float
    bw_hz: float
    source: str  # "survey" or "dwell" (dwell when any dwell reading saw it)
    voice_ratio: float | None = None
    cls: str = "unknown"  # voice | keyup | data | carrier | unknown
    clock_ok: bool = True
    clip_id: int | None = None
    ctcss_hz: float | None = None  # the standard CTCSS tone under it, when one was heard


@dataclasses.dataclass
class Track:
    channel: Channel
    first_t: float
    last_t: float
    state: TrackState = TrackState.CANDIDATE
    last_visit_t: float = 0.0
    visits: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=3)
    )
    snrs: list[float] = dataclasses.field(default_factory=list)
    floors: list[float] = dataclasses.field(default_factory=list)
    peak_snr: float = -1e9
    bw_hz: float = 0.0
    dwelt: bool = False
    dwell_run_start: float | None = None

    @property
    def hz(self) -> int:
        return self.channel.hz


@dataclasses.dataclass(frozen=True)
class TrackEvent:
    kind: str  # opened | closed | always_on | always_off
    hz: int
    t: float
    tx: Transmission | None = None


class _Duty:
    """Survey hits and visits per minute bucket over the trailing hour."""

    def __init__(self, t: float) -> None:
        self.first_t = t
        self.homes: set[tuple[int, int]] = set()  # window spans that have seen this channel
        self.buckets: collections.deque[list] = collections.deque()
        self.always_on = False
        self.last_hit_t = t

    def record(self, t: float, hit: bool) -> None:
        b = int(t // _DUTY_BUCKET_S)
        if self.buckets and self.buckets[-1][0] == b:
            self.buckets[-1][1] += 1
            self.buckets[-1][2] += int(hit)
        else:
            self.buckets.append([b, 1, int(hit)])
        horizon = int((t - _DUTY_WINDOW_S) // _DUTY_BUCKET_S)
        while self.buckets and self.buckets[0][0] <= horizon:
            self.buckets.popleft()
        if hit:
            self.last_hit_t = t

    def duty(self) -> float:
        visits = sum(b[1] for b in self.buckets)
        return sum(b[2] for b in self.buckets) / visits if visits else 0.0


class Tracker:
    def __init__(self, plan: BandPlan, clock: Clock, cfg: DetectConfig | None = None) -> None:
        self.plan = plan
        self.clock = clock
        self.cfg = cfg or DetectConfig()
        self._tracks: dict[int, Track] = {}
        self._duty: dict[int, _Duty] = {}
        self._keys: list[int] = []  # sorted keys of _duty
        # Bumped whenever a track appears, changes state or goes away, so callers such as
        # the scheduler can cache what they derive from ``active()``.
        self.version = 0

    # -- queries -----------------------------------------------------------------------
    def state(self, hz: int) -> TrackState:
        tr = self._tracks.get(hz)
        return tr.state if tr else TrackState.IDLE

    def is_always_on(self, hz: int) -> bool:
        d = self._duty.get(hz)
        return bool(d and d.always_on)

    def seed_always_on(self, hzs: list[int], t: float) -> None:
        """Carry always-on marks over a restart. A seeded channel clears as usual, once its
        survey duty falls below the clear threshold after it is seen again."""
        for hz in hzs:
            self._ensure_duty(int(hz), t).always_on = True

    def always_on(self) -> list[int]:
        return sorted(hz for hz, d in self._duty.items() if d.always_on)

    def tracks(self) -> list[Track]:
        return [self._tracks[k] for k in sorted(self._tracks)]

    def active(self, policy: str = "dwell", include_candidates: bool = False) -> list[Track]:
        """Tracks of one band policy. For ``dwell``, only voice-capable modes count."""
        ok = {TrackState.ACTIVE}
        if include_candidates:
            ok.add(TrackState.CANDIDATE)
        return [
            t
            for t in sorted(self._tracks.values(), key=lambda tr: tr.channel.hz)
            if t.state in ok
            and t.channel.policy == policy
            and (policy != "dwell" or t.channel.voice_capable)
        ]

    # -- updates -----------------------------------------------------------------------
    def update(
        self,
        peaks: list[Peak],
        t: float,
        source: str,
        span: tuple[int, int] | None = None,
    ) -> list[TrackEvent]:
        """Feed one reading of a window. ``span`` is the analyzed [lo, hi) range.

        A track in the span with no peak counts as missed, but only if this window has seen
        it before. A spur at a fixed tuner offset shows up at a different channel in each
        window; without that rule, every overlapping window would close it as a
        transmission. Without a span, nothing counts as missed.
        """
        hits = self._hits(peaks)
        events: list[TrackEvent] = []
        keys = set(hits)
        if span is not None:
            lo = bisect.bisect_left(self._keys, span[0])
            hi = bisect.bisect_left(self._keys, span[1])
            keys.update(self._keys[lo:hi])
        for hz in sorted(keys):
            peak_ch = hits.get(hz)
            if span is not None:
                if peak_ch is not None:
                    self._ensure_duty(hz, t).homes.add(span)
                elif span not in self._duty[hz].homes:
                    continue
            if source == "survey":
                events += self._record_duty(hz, t, peak_ch is not None)
            if self.is_always_on(hz):
                continue
            if peak_ch is not None:
                events += self._seen(hz, peak_ch[0], peak_ch[1], t, source)
            else:
                events += self._missed(hz, t, source)
        return events

    def expire(self, t: float, max_unvisited_s: float) -> list[TrackEvent]:
        """Close active tracks whose window has not been read for ``max_unvisited_s``."""
        events = []
        for hz, tr in list(self._tracks.items()):
            if t - max(tr.last_visit_t, tr.last_t) > max_unvisited_s:
                if tr.state is TrackState.ACTIVE:
                    events.append(self._close(tr, t))
                else:
                    del self._tracks[hz]
                    self.version += 1
        return events

    def close_all(self, t: float) -> list[TrackEvent]:
        events = [
            self._close(tr, t)
            for tr in self.tracks()
            if tr.state is TrackState.ACTIVE and not self.is_always_on(tr.hz)
        ]
        self._tracks.clear()
        self.version += 1
        return events

    # -- internals ---------------------------------------------------------------------
    def _hits(self, peaks: list[Peak]) -> dict[int, tuple[Peak, Channel]]:
        out: dict[int, tuple[Peak, Channel]] = {}
        for p in peaks:
            ch = self.plan.channel_for(p.hz)
            if ch.policy not in _TRACKED_POLICIES and ch.band != "unplanned":
                continue
            ch = self._sticky(ch, p.hz)
            prev = out.get(ch.hz)
            if prev is None or p.snr_db > prev[0].snr_db:
                out[ch.hz] = (p, ch)
        return out

    def _sticky(self, ch: Channel, hz: float) -> Channel:
        """Keep a peak near a raster midpoint on the channel it already belongs to.

        A signal halfway between two raster points snaps to either one depending on noise;
        without this, each flip reads as a miss on one channel and a new signal on the next.
        """
        if ch.hz in self._duty:
            return ch
        try:  # a channel restored from older rows can name a band the plan no longer has
            raster = self.plan.band(ch.band).raster_hz if ch.band != "unplanned" else 12_500
        except KeyError:
            raster = 12_500
        for other in (ch.hz - raster, ch.hz + raster):
            if other in self._duty and abs(hz - other) <= 0.75 * raster:
                return self.plan.channel_for(other)
        return ch

    def _ensure_duty(self, hz: int, t: float) -> _Duty:
        d = self._duty.get(hz)
        if d is None:
            d = self._duty[hz] = _Duty(t)
            bisect.insort(self._keys, hz)
        return d

    def _record_duty(self, hz: int, t: float, hit: bool) -> list[TrackEvent]:
        d = self._duty.get(hz)
        if d is None:
            if not hit:
                return []
            d = self._ensure_duty(hz, t)
        d.record(t, hit)
        events: list[TrackEvent] = []
        duty = d.duty()
        if not d.always_on and t - d.first_t >= _DUTY_WINDOW_S and duty > self.cfg.always_on_duty:
            d.always_on = True
            if self._tracks.pop(hz, None) is not None:
                self.version += 1
            events.append(TrackEvent("always_on", hz, t))
        elif d.always_on and duty < _ALWAYS_ON_CLEAR:
            d.always_on = False
            events.append(TrackEvent("always_off", hz, t))
        if not d.always_on and t - d.last_hit_t > _DUTY_WINDOW_S and hz not in self._tracks:
            del self._duty[hz]
            self._keys.remove(hz)
        return events

    def _seen(self, hz: int, p: Peak, ch: Channel, t: float, source: str) -> list[TrackEvent]:
        self._ensure_duty(hz, t)
        tr = self._tracks.get(hz)
        if tr is None:
            tr = self._tracks[hz] = Track(channel=ch, first_t=t, last_t=t)
            self.version += 1
        tr.last_t = t
        tr.last_visit_t = t
        if len(tr.snrs) < _MAX_SNR_SAMPLES:
            tr.snrs.append(p.snr_db)
            tr.floors.append(p.level_db - p.snr_db)
        tr.peak_snr = max(tr.peak_snr, p.snr_db)
        tr.bw_hz = max(tr.bw_hz, p.width_hz)
        if source == "dwell":
            tr.dwelt = True
            if tr.dwell_run_start is None:
                tr.dwell_run_start = t
        else:
            tr.visits.append(True)
            tr.dwell_run_start = None
        if tr.state is TrackState.CANDIDATE:
            confirmed = (
                sum(tr.visits) >= 2
                or (source == "dwell" and p.snr_db >= self.cfg.dwell_open_snr_db)
                or (source == "dwell" and t - (tr.dwell_run_start or t) >= 1.0)
            )
            if confirmed:
                tr.state = TrackState.ACTIVE
                self.version += 1
                return [TrackEvent("opened", hz, t)]
        return []

    def _missed(self, hz: int, t: float, source: str) -> list[TrackEvent]:
        tr = self._tracks.get(hz)
        if tr is None:
            return []
        tr.last_visit_t = t
        if source == "dwell":
            tr.dwell_run_start = None
            if t - tr.last_t >= self.cfg.hang_s:
                if tr.state is TrackState.ACTIVE:
                    return [self._close(tr, t)]
                del self._tracks[hz]
                self.version += 1
            return []
        tr.visits.append(False)
        if tr.state is TrackState.ACTIVE:
            return [self._close(tr, t)]
        if sum(tr.visits) + (3 - len(tr.visits)) < 2 or list(tr.visits)[-2:] == [False, False]:
            del self._tracks[hz]
            self.version += 1
        return []

    def _close(self, tr: Track, t: float) -> TrackEvent:
        self._tracks.pop(tr.hz, None)
        self.version += 1
        tx = Transmission(
            hz=tr.hz,
            band=tr.channel.band,
            mode=tr.channel.mode,
            t_start=tr.first_t,
            t_end=tr.last_t,
            dur_ms=int(round((tr.last_t - tr.first_t) * 1000)),
            peak_snr_db=round(tr.peak_snr, 1),
            med_snr_db=round(statistics.median(tr.snrs), 1),
            floor_db=round(statistics.median(tr.floors), 1),
            bw_hz=tr.bw_hz,
            source="dwell" if tr.dwelt else "survey",
        )
        return TrackEvent("closed", tr.hz, t, tx)
