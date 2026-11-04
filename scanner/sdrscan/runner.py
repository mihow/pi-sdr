"""The core loop: source -> scheduler -> detector -> tracker -> channelizer/gate -> store.

``Runner.step`` runs one scheduler job. During a dwell, each voice-capable channel's audio
goes to its voice gate and to a clip writer. When the track closes, the gate's verdict
becomes the transmission's class; a voice transmission of at least ``min_clip_s`` keeps its
clip, anything else is discarded. The closed transmission then goes to the store and its
hour rollup, and to ``on_tx`` (the service queues speech-to-text there).

``spectrum`` picks how the detector sees the band. ``iq`` renders and analyzes real IQ for
everything. ``psd`` asks a source that offers ``read_psd`` (the synthetic source) for the
averaged spectrum directly, which keeps week-long simulations fast but produces no dwell
audio. ``hybrid`` uses the fast spectrum for survey hops and real IQ for dwells, so a
simulation also exercises demodulation, the voice gate and clips.
"""

from __future__ import annotations

import json
import typing

import numpy as np

from . import IQ_RATE, rollup
from .bandplan import BandPlan
from .clock import Clock
from .config import Config
from .detect import HopResult, SpurMap, analyze, analyze_psd, clip_fraction
from .dsp.channelizer import Channelizer
from .iq.base import IqSource, IqSourceError
from .scheduler import CALIBRATE, DWELL, IDLE, SURVEY, Job, Scheduler
from .store import Store
from .tracker import Tracker, TrackEvent, TrackState, Transmission
from .voice.gate import Gate, VoiceGate, VoiceState

BACKOFF_START_S = 5.0
BACKOFF_MAX_S = 300.0
# Consecutive failed opens (about 10 minutes of backoff) before the runner gives up, so the
# container restarts and sees the USB bus afresh after a replug.
MAX_OPEN_FAILURES = 8
MAX_UNVISITED_S = 900.0  # longer than a tier B pass, so tier B tracks are not cut short
PRUNE_EVERY_S = 86400.0
SPUR_MARKS_KEY = "spur_marks"


NOW_S = 300.0


class DongleLost(RuntimeError):
    """The source failed to open ``MAX_OPEN_FAILURES`` times in a row."""


class ClipSink(typing.Protocol):
    def begin(self, hz: int, t: float) -> typing.Any: ...

    def finish(self, writer: typing.Any, meta: dict) -> int | None: ...

    def abort(self, writer: typing.Any) -> None: ...

    def disk_low(self) -> bool: ...


class NoClips:
    """A clip sink that records nothing (tests and runs without storage)."""

    def begin(self, hz: int, t: float) -> None:
        return None

    def finish(self, writer: object, meta: dict) -> None:
        return None

    def abort(self, writer: object) -> None:
        return None

    def disk_low(self) -> bool:
        return False


# (transmission id, transmission, kept clip audio at AUDIO_RATE or None)
OnTx = typing.Callable[[int, Transmission, np.ndarray | None], None]


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
        gate_factory: typing.Callable[[], Gate] = VoiceGate,
        clips: ClipSink | None = None,
        on_tx: OnTx | None = None,
        mode_fn: typing.Callable[[], str] | None = None,
        spectrum: str = "iq",
        rate: int = IQ_RATE,
    ) -> None:
        if spectrum not in {"iq", "psd", "hybrid"}:
            raise ValueError("spectrum must be 'iq', 'psd' or 'hybrid'")
        if spectrum != "iq" and not hasattr(source, "read_psd"):
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
        self._survey_psd = spectrum in {"psd", "hybrid"}
        self._dwell_iq = spectrum in {"iq", "hybrid"}
        self.on_tx = on_tx
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
        self.backing_off = False  # inside a deliberate backoff sleep (not a hang)
        self.open_failures = 0
        self._backoff = BACKOFF_START_S
        self._retry_at = -1e18
        self._channelizer: Channelizer | None = None
        self._chan_key: tuple | None = None
        self._gates: dict[int, Gate] = {}
        self._writers: dict[int, typing.Any] = {}
        self._tuner: dict = {}
        self._hour = int(clock.now() // 3600 * 3600)
        self._last_prune = clock.now()
        self._marked_spurs: set[int] = set()
        self._restore_marks(seed_spurs=spurs is None)

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
        self._tuner = {"center_hz": int(job.center_hz), "kind": job.kind.lower()}
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
        if self._survey_psd and iq is None:
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
        if self._dwell_iq and voice:
            key = (job.center_hz, tuple(c.hz for c in voice))
            if key != self._chan_key:
                self._channelizer = Channelizer(self.rate, job.center_hz, voice)
                self._chan_key = key
        for _ in range(blocks):
            if not self._dwell_iq:
                r = self._measure(job.center_hz, n)
            else:
                iq = self.source.read(n)
                r = analyze(iq, job.center_hz, self.rate, self.plan, self.cfg.detect)
                r.clip_frac = clip_fraction(iq, self.cfg.detect.clip_level)
                if self._channelizer is not None and voice:
                    self._audio(self._channelizer.push(iq))
            self._survey_result(r, "dwell")

    def _audio(self, chunks: dict[int, np.ndarray]) -> None:
        """Feed each active channel's audio to its gate and, while it may be voice, a clip."""
        t = self.clock.now()
        for hz, pcm in chunks.items():
            if self.tracker.state(hz) is not TrackState.ACTIVE:
                continue  # the channel's track is gone or not yet confirmed
            gate = self._gates.get(hz)
            if gate is None:
                gate = self._gates[hz] = self.gate_factory()
            state = gate.push(pcm)
            self.status["dwell_audio_s"] += len(pcm) / gate.rate
            if state in (VoiceState.PENDING, VoiceState.VOICE):
                w = self._writers.get(hz)
                if w is None:
                    w = self._writers[hz] = self.clips.begin(hz, t - len(pcm) / gate.rate)
                if w is not None:
                    w.write(pcm)

    def now_state(self) -> dict:
        """Channels heard in the last 5 minutes and the tuner state (the digest's ``now``)."""
        t = self.clock.now()
        active = []
        for tr in self.tracker.tracks():
            if tr.state is not TrackState.ACTIVE or t - tr.last_t > NOW_S:
                continue
            gate = self._gates.get(tr.hz)
            cls = "voice" if gate is not None and gate.summary().get("class") == "voice" else None
            item = {"hz": tr.hz, "age_s": int(max(0, t - tr.last_t)), "snr": round(tr.peak_snr, 1)}
            item["cls"] = cls or "unknown"
            active.append(item)
        return {"active": active, "tuner": dict(self._tuner, mode=self.status["mode"])}

    # -- events ------------------------------------------------------------------------
    def _close_tx(self, tx: Transmission) -> None:
        gate = self._gates.pop(tx.hz, None)
        writer = self._writers.pop(tx.hz, None)
        if gate is not None:
            summary = gate.summary()
            tx.voice_ratio = summary.get("voice_ratio")
            tx.cls = summary.get("class", "unknown")
            gate.reset()
        clip_id = None
        pcm = None
        keep = tx.cls == "voice" and tx.dur_ms / 1000 >= self.cfg.quota.min_clip_s
        if writer is not None:
            if keep:
                pcm = writer.read()
                clip_id = self.clips.finish(writer, {"class": tx.cls})
            else:
                self.clips.abort(writer)
        tx.clip_id = clip_id
        if self.clips.disk_low():
            self.status["dropped_tx"] = self.status.get("dropped_tx", 0) + 1
            return  # below the free-space floor: rollups, health and digests continue
        tx_id = self.store.add_tx(tx)
        rollup.on_tx_closed(self.store, tx)
        if self.on_tx is not None:
            self.on_tx(tx_id, tx, pcm if clip_id is not None else None)

    def _handle(self, events: list[TrackEvent]) -> None:
        for ev in events:
            if ev.kind == "closed" and ev.tx is not None:
                self._close_tx(ev.tx)
            elif ev.kind in ("always_on", "always_off"):
                band = self.plan.band_for(ev.hz).id
                on = ev.kind == "always_on"
                self.store.mark_channel(ev.hz, band=band, always_on=on, t=ev.t)

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
            self.backing_off = True
            try:
                self.clock.sleep(self._retry_at - now)
            finally:
                self.backing_off = False
        try:
            self.source.open()
        except IqSourceError as exc:
            self._fail(exc, opened=False)
            self.open_failures += 1
            if self.open_failures >= MAX_OPEN_FAILURES:
                raise DongleLost(
                    f"the radio failed to open {self.open_failures} times: {exc}"
                ) from exc
            return False
        self._open = True
        self.open_failures = 0
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
            self._save_marks()

    # -- marks that must survive a restart ---------------------------------------------
    def _restore_marks(self, seed_spurs: bool) -> None:
        """Seed the spur map and the always-on carriers from the store. Without this, a
        restart records known spurs for a day and carriers for an hour."""
        self.store.flush()
        raw = self.store.meta(SPUR_MARKS_KEY)
        if seed_spurs and raw:
            try:
                marks = json.loads(raw)
                self.spurs.seed(marks.get("abs_hz", []), marks.get("offset_hz", []))
            except (ValueError, AttributeError):
                pass  # a damaged value only costs a day of relearning
        on = [r[0] for r in self.store.db.execute("SELECT hz FROM channels WHERE always_on = 1")]
        self.tracker.seed_always_on(on, self.clock.now())

    def _save_marks(self) -> None:
        value = json.dumps(self.spurs.marked(), sort_keys=True)
        with self.store.db:
            self.store.set_meta(SPUR_MARKS_KEY, value)

    def _housekeeping(self) -> None:
        self._roll_hour()
        now = self.clock.now()
        if now - self._last_prune >= PRUNE_EVERY_S:
            self.store.prune(now)
            self._last_prune = now
