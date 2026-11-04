"""The core loop: source -> scheduler -> detector -> tracker -> channelizer/gate -> store.

``Runner.step`` runs one scheduler job. The voice gate and clip store are reached only
through small interfaces; Part A passes ``AlwaysNoiseGate`` and ``NoClips``.

``spectrum="psd"`` asks a source that offers ``read_psd`` (the synthetic source) for the
averaged spectrum directly instead of IQ. Scene simulations of a day or a week use it;
dwell audio is then not produced.
"""

from __future__ import annotations

import typing

import numpy as np

from . import IQ_RATE
from .bandplan import BandPlan
from .clock import Clock
from .config import Config
from .detect import HopResult, SpurMap, analyze, analyze_psd, clip_fraction
from .dsp.channelizer import Channelizer
from .iq.base import IqSource, IqSourceError
from .scheduler import CALIBRATE, DWELL, IDLE, SURVEY, Job, Scheduler
from .store import Store
from .tracker import Tracker, TrackEvent
from .voice.gate import AlwaysNoiseGate, Gate

BACKOFF_START_S = 5.0
BACKOFF_MAX_S = 300.0
MAX_UNVISITED_S = 900.0  # longer than a tier B pass, so tier B tracks are not cut short
PRUNE_EVERY_S = 86400.0


class ClipSink(typing.Protocol):
    def begin(self, hz: int, t: float) -> object | None: ...

    def finish(self, writer: object, meta: dict) -> int | None: ...


class NoClips:
    """Part A stand-in for the clip store: records nothing."""

    def begin(self, hz: int, t: float) -> None:
        return None

    def finish(self, writer: object, meta: dict) -> None:
        return None


class Runner:
    def __init__(
        self,
        *,
        cfg: Config,
        plan: BandPlan,
        source: IqSource,
        clock: Clock,
        store: Store,
        scheduler: Scheduler | None = None,
        tracker: Tracker | None = None,
        spurs: SpurMap | None = None,
        gate_factory: typing.Callable[[], Gate] = AlwaysNoiseGate,
        clips: ClipSink | None = None,
        mode_fn: typing.Callable[[], str] | None = None,
        spectrum: str = "iq",
        rate: int = IQ_RATE,
    ) -> None:
        if spectrum not in {"iq", "psd"}:
            raise ValueError("spectrum must be 'iq' or 'psd'")
        if spectrum == "psd" and not hasattr(source, "read_psd"):
            raise ValueError("this source has no fast spectrum path; use spectrum='iq'")
        self.cfg = cfg
        self.plan = plan
        self.source = source
        self.clock = clock
        self.store = store
        self.scheduler = scheduler or Scheduler(plan, cfg.duty, clock)
        self.tracker = tracker or Tracker(plan, clock, cfg.detect)
        self.spurs = spurs or SpurMap()
        self.gate_factory = gate_factory
        self.clips = clips or NoClips()
        self.mode_fn = mode_fn or (lambda: "normal")
        self.spectrum = spectrum
        self.rate = rate
        self.status: dict = {
            "mode": "normal",
            "errors": 0,
            "last_error": None,
            "jobs": 0,
            "dwell_audio_s": 0.0,
            "last_clip_frac": 0.0,
        }
        self._open = False
        self._backoff = BACKOFF_START_S
        self._retry_at = -1e18
        self._channelizer: Channelizer | None = None
        self._chan_key: tuple | None = None
        self._gates: dict[int, Gate] = {}
        self._hour = int(clock.now() // 3600 * 3600)
        self._last_prune = clock.now()
        self._marked_spurs: set[int] = set()

    # -- loop --------------------------------------------------------------------------
    def run(self, until: float) -> None:
        while self.clock.now() < until:
            self.step()

    def step(self) -> None:
        if not self._ensure_open():
            return
        mode = self.mode_fn()
        self.status["mode"] = mode
        job = self.scheduler.next_job(self.clock.now(), self.tracker, mode)
        try:
            self._execute(job)
        except IqSourceError as exc:
            self._fail(exc)
            return
        self.status["jobs"] += 1
        self._handle(self.tracker.expire(self.clock.now(), MAX_UNVISITED_S))
        self._housekeeping()

    def shutdown(self) -> None:
        self._handle(self.tracker.close_all(self.clock.now()))
        self._roll_hour(force=True)
        self.store.flush()
        if self._open:
            self.source.close()
            self._open = False

    # -- jobs --------------------------------------------------------------------------
    def _execute(self, job: Job) -> None:
        if job.kind == IDLE:
            self.clock.sleep(job.duration_s)
            return
        self.source.tune(job.center_hz, job.gain_db)
        if job.kind == SURVEY:
            r = self._measure(job.center_hz, int(job.duration_s * self.rate))
            self._survey_result(r, "survey")
        elif job.kind == CALIBRATE:
            r = self._measure(job.center_hz, int(job.duration_s * self.rate))
            t = self.clock.now()
            self.spurs.update(r, t)
            self.store.add_hop(r, t, band=self.plan.band_for(job.center_hz).id)
        elif job.kind == DWELL:
            self._dwell(job)
        else:  # EXTERNAL jobs arrive with the first decoder (plan task B12)
            raise ValueError(f"unsupported job kind {job.kind}")

    def _measure(self, center: int, n: int, iq: np.ndarray | None = None) -> HopResult:
        d = self.cfg.detect
        if self.spectrum == "psd":
            navg = max(1, min(d.navg, n // d.nfft))
            psd, clip = self.source.read_psd(d.nfft, navg)  # type: ignore[attr-defined]
            return analyze_psd(psd, center, self.rate, self.plan, d, clip_frac=clip)
        if iq is None:
            iq = self.source.read(n)
        return analyze(iq, center, self.rate, self.plan, d)

    def _survey_result(self, r: HopResult, source: str) -> None:
        t = self.clock.now()
        self.status["last_clip_frac"] = r.clip_frac
        self.spurs.update(r, t)
        peaks = []
        for p in r.peaks:
            if self.spurs.is_spur(p.hz, r.center_hz):
                self._mark_spur(p.hz, t)
            else:
                peaks.append(p)
        self._handle(self.tracker.update(peaks, t, source, span=(r.lo_hz, r.hi_hz)))
        if source == "survey":
            self.store.add_hop(r, t, band=self.plan.band_for(r.center_hz).id)

    def _dwell(self, job: Job) -> None:
        block_s = self.cfg.duty.dwell_block_s
        n = int(block_s * self.rate)
        blocks = max(1, int(round(job.duration_s / block_s)))
        voice = [c for c in job.channels if c.voice_capable]
        if self.spectrum == "iq" and voice:
            key = (job.center_hz, tuple(c.hz for c in voice))
            if key != self._chan_key:
                self._channelizer = Channelizer(self.rate, job.center_hz, voice)
                self._chan_key = key
        for _ in range(blocks):
            if self.spectrum == "psd":
                r = self._measure(job.center_hz, n)
            else:
                iq = self.source.read(n)
                r = analyze(iq, job.center_hz, self.rate, self.plan, self.cfg.detect)
                r.clip_frac = clip_fraction(iq, self.cfg.detect.clip_level)
                if self._channelizer is not None and voice:
                    for hz, pcm in self._channelizer.push(iq).items():
                        gate = self._gates.get(hz)
                        if gate is None:
                            gate = self._gates[hz] = self.gate_factory()
                        gate.push(pcm)
                        self.status["dwell_audio_s"] += len(pcm) / gate.rate
            self._survey_result(r, "dwell")

    # -- events ------------------------------------------------------------------------
    def _handle(self, events: list[TrackEvent]) -> None:
        for ev in events:
            if ev.kind == "closed" and ev.tx is not None:
                gate = self._gates.pop(ev.hz, None)
                if gate is not None:
                    summary = gate.summary()
                    ev.tx.voice_ratio = summary.get("voice_ratio")
                    gate.reset()
                self.store.add_tx(ev.tx)
            elif ev.kind == "always_on":
                band = self.plan.band_for(ev.hz).id
                self.store.mark_channel(ev.hz, band=band, always_on=True, t=ev.t)

    def _mark_spur(self, hz: float, t: float) -> None:
        ch = self.plan.channel_for(hz)
        if ch.hz not in self._marked_spurs:
            self._marked_spurs.add(ch.hz)
            self.store.mark_channel(ch.hz, band=ch.band, spur=True, t=t)

    # -- failure and housekeeping ------------------------------------------------------
    def _ensure_open(self) -> bool:
        if self._open:
            return True
        now = self.clock.monotonic()
        if now < self._retry_at:
            self.clock.sleep(self._retry_at - now)
        try:
            self.source.open()
        except IqSourceError as exc:
            self._fail(exc, opened=False)
            return False
        self._open = True
        self._backoff = BACKOFF_START_S
        if self.status["mode"] == "no_dongle":
            self.status["mode"] = "normal"
        return True

    def _fail(self, exc: Exception, opened: bool = True) -> None:
        self.status["errors"] += 1
        self.status["last_error"] = str(exc)
        self.status["mode"] = "no_dongle"
        if opened and self._open:
            try:
                self.source.close()
            finally:
                self._open = False
        self._chan_key = None
        self._retry_at = self.clock.monotonic() + self._backoff
        self._backoff = min(BACKOFF_MAX_S, self._backoff * 2)

    def _roll_hour(self, force: bool = False) -> None:
        hour = int(self.clock.now() // 3600 * 3600)
        if hour != self._hour or force:
            cov = self.scheduler.coverage(self._hour)
            if cov is not None:
                self.store.set_coverage(self._hour, cov)
            self._hour = hour

    def _housekeeping(self) -> None:
        self._roll_hour()
        now = self.clock.now()
        if now - self._last_prune >= PRUNE_EVERY_S:
            self.store.prune(now)
            self._last_prune = now
