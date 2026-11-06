"""The whole rig in one object: scan loop, health, rollups, speech-to-text and the uplink.

``Service`` owns one ``Runner`` and adds the slower duties around it, all on the injected
clock, so ``sdrscan run`` (real time) and ``sdrscan simulate`` and the end-to-end test
(virtual time) share one code path:

- every ``thermal.sample_interval_s``: sample health, store it, ping the watchdog;
- after each kept voice clip: transcribe it when an engine is configured and the governor
  allows it (inline in simulations, on a worker thread on the rig), and store the call
  signs found in the text;
- a progress watchdog (``start_watchdog``, real time only): when the scan loop makes no
  progress for 120 s, for example inside a hung USB read, the process exits so systemd
  restarts the container;
- at each hour boundary: rebuild the finished hour, fold it into the hour-of-week
  profiles, fold a finished day into the day rollup, then build the digest and hand it to
  the publisher, and write it next to the dashboard page under ``www/``.
"""

from __future__ import annotations

import gzip
import logging
import os
import queue
import threading
import typing
from pathlib import Path

import numpy as np

from . import rollup
from .bandplan import BandPlan
from .clips import ClipStore, Encoder
from .clock import Clock, RealClock, kernel_clock_synced
from .config import Config
from .dashboard import build_dashboard
from .health import HealthMonitor
from .iq.base import IqSource
from .llm import ProseEngine, make_prose
from .lowprio import call_low, lower_this_thread
from .runner import Runner
from .store import Store
from .summarize import build_digest, encode_digest
from .tracker import Transmission
from .uplink import DigestPublisher, Ledger, make_target
from .voice.stt import NullStt, SttEngine, make_stt, select_stt

log = logging.getLogger("sdrscan.service")
STT_MAX_S = 30.0
STT_HOT_WAIT_S = 10.0  # how often a held transcription re-reads the temperature
HOUR_GRACE_S = 5.0  # let transmissions that end on the boundary close first
WATCHDOG_S = 120.0
WATCHDOG_EXIT = 70
CATCH_UP_MAX_S = 7 * 86400  # unfinished hours older than this are left as they are


class Service:
    def __init__(
        self,
        cfg: Config,
        clock: Clock,
        source: IqSource,
        *,
        plan: BandPlan | None = None,
        db_path: Path | None = None,
        spectrum: str = "iq",
        encoder: Encoder | None = None,
        statvfs: typing.Callable | None = None,
        sys_root: Path = Path("/sys"),
        publisher: DigestPublisher | None = None,
        stt: SttEngine | None = None,
        prose: ProseEngine | None = None,
        inline_stt: bool = False,
        runner_kw: dict | None = None,
        clock_synced: typing.Callable[[], bool | None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        if clock_synced is None:
            clock_synced = kernel_clock_synced if isinstance(clock, RealClock) else lambda: None
        self._clock_synced = clock_synced
        self._unsynced_ref: float | None = None  # wall minus monotonic while unsynced
        self.plan = plan or BandPlan.load(cfg.bandplan_path, cfg.labels_path)
        root = Path(cfg.data_root)
        self.www = root / "www"
        self.store = Store.open(db_path or root / "db" / "scan.sqlite", clock)
        self.store.migrate()
        self.store.set_meta("site_id", cfg.site_id)
        fs_kw = {"statvfs": statvfs} if statvfs is not None else {}
        self.clips = ClipStore(
            self.store,
            cfg.quota,
            data_root=root,
            stage_root=cfg.stage_root,
            clock=clock,
            encoder=encoder,
            **fs_kw,
        )
        self.runner = Runner(
            cfg=cfg,
            plan=self.plan,
            source=source,
            clock=clock,
            store=self.store,
            clips=self.clips,
            on_tx=self._on_tx,
            spectrum=spectrum,
            **(runner_kw or {}),
        )
        self.health = HealthMonitor(
            cfg,
            clock,
            sys_root,
            iq_age=self._iq_age,
            quota_used=self.clips.used_bytes,
            status=lambda: self.runner.status,
            extra_flags=self._extra_flags,
            clock_synced=clock_synced,
            **fs_kw,
        )
        self.runner.mode_fn = self.health.mode
        # The speech-to-text engine is chosen once at start (stt.engine: auto reads the
        # stt-bench choice file) and built lazily on the STT worker thread, at nice 19.
        self.stt_selection = select_stt(cfg.stt, root / "stt-choice.json")
        self._stt_engine: SttEngine | None = None
        self._stt_flags: list[str] = list(self.stt_selection.flags)
        if cfg.stt.engine != "none":
            log.info(
                "speech-to-text: %s (%s)%s",
                self.stt_selection.engine,
                self.stt_selection.source,
                f"; {self.stt_selection.detail}" if self.stt_selection.detail else "",
            )
        self.runner.clock_ok_fn = lambda: self._clock_synced() is not False
        self.ledger = Ledger(self.store, cfg.uplink, clock)
        # A missing or bad uplink configuration (the state of a fresh install) turns pushes
        # off; it must never stop the scan or the live page on a rig nobody can reach.
        self.uplink_error: str | None = None
        if publisher is None and (cfg.uplink.target != "file" or cfg.uplink.path is not None):
            try:
                target = make_target(cfg.uplink)
            except (ValueError, OSError) as exc:
                self.uplink_error = str(exc)
                log.error("uplink disabled: %s; scanning and the live page continue", exc)
            else:
                publisher = DigestPublisher(target, self.ledger, root / "outbox", clock, cfg.uplink)
        self.publisher = publisher
        self._stt_fixed = stt
        self.prose = prose or make_prose(cfg.prose)
        self.inline_stt = inline_stt
        self._stt_q: queue.Queue = queue.Queue(maxsize=cfg.stt.queue_max)
        self._stt_thread: threading.Thread | None = None
        self._next_health = clock.now()
        self._hour = rollup.hour_of(clock.now())
        self._catch_up()
        self.last_digest: dict | None = None
        self._progress = clock.monotonic()
        self.digests = 0
        self.www.mkdir(parents=True, exist_ok=True)
        build_dashboard("digest.json.gz", "static", self.www)
        if self.publisher is not None:
            self.publisher.publish_files({"index.html": (self.www / "index.html").read_bytes()})

    # -- loop --------------------------------------------------------------------------
    def run(self, until: float | None = None, stop: threading.Event | None = None) -> None:
        while (until is None or self.clock.now() < until) and not (stop and stop.is_set()):
            self.runner.step()
            self._progress = self.clock.monotonic()
            self.tick()

    def tick(self) -> None:
        self._check_clock_sync()
        now = self.clock.now()
        if now >= self._next_health:
            h = self.health.sample()
            self.store.add_health(h.to_row())
            self.health.notify_watchdog()
            self._next_health = now + self.cfg.thermal.sample_interval_s
        if self.inline_stt:
            self._drain_stt()
        if rollup.hour_of(now - HOUR_GRACE_S) > self._hour:
            self._finish_hour(self._hour)
            self._hour = rollup.hour_of(now - HOUR_GRACE_S)

    def _check_clock_sync(self) -> None:
        """When NTP first syncs, shift rows stamped by the unsynced clock by the step.

        The step is the change in wall time minus monotonic time since the clock was
        first seen unsynced. Rows from an earlier boot keep ``clock_ok = 0``.
        """
        offset = self.clock.now() - self.clock.monotonic()
        if self._clock_synced() is False:
            if self._unsynced_ref is None:
                self._unsynced_ref = offset
            return
        if self._unsynced_ref is None:
            return
        step = offset - self._unsynced_ref
        self._unsynced_ref = None
        log.info("clock synchronized; shifting unsynced rows by %.0f s", step)
        rollup.apply_clock_correction(self.store, step if abs(step) >= 1 else 0.0)

    def watchdog_check(self, exit_fn: typing.Callable[[int], None] = os._exit) -> bool:
        """Exit the process when the scan loop has been stuck for ``WATCHDOG_S``.

        A deliberate backoff while the radio is missing counts as progress; the runner
        gives up on its own after ``MAX_OPEN_FAILURES`` (see ``runner.DongleLost``).
        """
        if self.runner.backing_off:
            self._progress = self.clock.monotonic()
            return True
        stuck = self.clock.monotonic() - self._progress
        if stuck > WATCHDOG_S:
            log.error("scan loop made no progress for %.0f s; exiting for a restart", stuck)
            logging.shutdown()
            exit_fn(WATCHDOG_EXIT)
            return False
        return True

    def start_watchdog(self, stop: threading.Event) -> threading.Thread:
        def loop() -> None:
            while not stop.wait(10.0):
                self.watchdog_check()

        t = threading.Thread(target=loop, name="sdrscan-watchdog", daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        self.runner.shutdown()
        if self.inline_stt:
            self._drain_stt()
        self.store.flush()
        self.store.close()

    # -- hourly work -------------------------------------------------------------------
    def _finish_hour(self, hour_ts: int) -> None:
        self.store.flush()
        rollup.recompute_hour(self.store, hour_ts)
        rollup.update_profiles(self.store, hour_ts)
        self._set_hours_through(hour_ts + rollup.H)
        self._roll_days(rollup.day_of(hour_ts + rollup.H))
        self.publish_digest()

    def _catch_up(self) -> None:
        """Finish what downtime left open: hours the service never closed, and every
        whole day since ``rolled_through`` (a rig that is down at midnight would otherwise
        lose that day from the week and all-time views)."""
        self.store.flush()
        cur = self._hour
        done = self.store.meta("hours_through")
        if done is not None:
            for h in range(max(int(done), cur - CATCH_UP_MAX_S), cur, rollup.H):
                rollup.recompute_hour(self.store, h)
        self._set_hours_through(cur)
        self._roll_days(rollup.day_of(cur))

    def _set_hours_through(self, hour_ts: int) -> None:
        with self.store.db:
            self.store.set_meta("hours_through", str(int(hour_ts)))

    def _roll_days(self, today: int) -> None:
        """Roll up every finished day before ``today`` that has not been rolled up yet."""
        rolled = self.store.meta("rolled_through")
        if rolled is None:
            first = self.store.db.execute("SELECT MIN(hour_ts) FROM rollup_hour").fetchone()[0]
            if first is None:
                return
            rolled = rollup.day_of(first)
        for day in range(int(rolled), today, rollup.DAY):
            rollup.rollup_day(self.store, day)

    def make_digest(self, include_transcripts: bool | None = None) -> dict:
        """The digest. Transcripts and call signs go in only with
        ``uplink.include_transcripts``, because the off-site copy is publicly readable;
        the live page on the rig passes ``include_transcripts=True``."""
        if include_transcripts is None:
            include_transcripts = self.cfg.uplink.include_transcripts
        now = self.clock.now()
        h = self.health.last or self.health.sample()
        digest = build_digest(
            self.store,
            h,
            now,
            plan=self.plan,
            site_id=self.cfg.site_id,
            include_transcripts=include_transcripts,
            now_state=self.runner.now_state(),
            uplink=self._uplink_state(),
        )
        if now - rollup.day_of(now) < rollup.H:  # once a day, just after midnight UTC
            digest["prose"] = call_low(self.prose.rewrite, digest, mode=self.health.mode())
        return digest

    def _uplink_state(self) -> dict:
        state = self.ledger.to_dict()
        if self.uplink_error:
            state["state"] = "unconfigured"
        return state

    def publish_digest(self) -> dict:
        digest = self.make_digest()
        body = gzip.compress(encode_digest(digest), 9, mtime=0)
        tmp = self.www / ".digest.json.gz.tmp"
        tmp.write_bytes(body)
        tmp.replace(self.www / "digest.json.gz")
        if self.publisher is not None:
            res = self.publisher.publish(digest, self.clock.now())
            log.info("digest %d bytes gzipped, push %s", len(body), res.status)
        self.last_digest = digest
        self.digests += 1
        return digest

    def _extra_flags(self) -> list[str]:
        return (["uplink_unconfigured"] if self.uplink_error else []) + self._stt_flags

    # -- speech-to-text ----------------------------------------------------------------
    def _stt(self) -> SttEngine:
        mode = self.health.mode()
        if mode != "normal":
            return NullStt()  # every other mode is hot; a loaded model stays loaded
        if self._stt_fixed is not None:
            return self._stt_fixed
        if self._stt_engine is None:
            try:
                self._stt_engine = make_stt(
                    self.cfg.stt,
                    mode,
                    work_dir=self.cfg.stage_root,
                    selection=self.stt_selection,
                )
            except Exception as exc:  # noqa: BLE001 - a broken model must not stop the scan
                log.error("speech-to-text engine %s failed to load: %s",
                          self.stt_selection.engine, exc)  # fmt: skip
                self._stt_engine = NullStt()
                self._stt_flags.append("stt_unavailable")
        return self._stt_engine

    def _on_tx(self, tx_id: int, tx: Transmission, pcm: np.ndarray | None) -> None:
        if pcm is None or tx.clip_id is None:
            return
        if self.stt_selection.engine == "none" and self._stt_fixed is None:
            return
        pcm16 = (np.clip(pcm[: int(STT_MAX_S * 16_000)], -1, 1) * 32767).astype(np.int16)
        try:
            self._stt_q.put_nowait((tx.clip_id, tx_id, pcm16, tx.hz, tx.band))
        except queue.Full:
            log.info("speech-to-text queue full; clip %s keeps no transcript", tx.clip_id)
            return
        if not self.inline_stt and self._stt_thread is None:
            self._stt_thread = threading.Thread(target=self._stt_worker, daemon=True)
            self._stt_thread.start()

    def _transcribe(self, item) -> None:
        clip_id, tx_id, pcm16, hz, band = item
        tr = self._stt().transcribe(pcm16.astype(np.float32) / 32767)
        if tr is None:
            return
        now = self.clock.now()
        tid = self.store.add_transcript(
            clip_id, tx_id, tr.engine, tr.text, tr.confidence, tr.rtf, now
        )
        self.store.add_calls_from_transcript(
            tr.text, tr.confidence, hz=hz, band=band, t=now, tx_id=tx_id, transcript_id=tid
        )
        self.clips.set_tier(clip_id, {"class": "voice", "confidence": tr.confidence})

    def _stt_too_hot(self) -> bool:
        """A fresh SoC reading at or above thermal.hot_c holds transcription.

        Read here, not from the 30 s health sample: whisper runs per clip, so the heat comes
        in short spikes that the periodic sample misses. Recording carries on regardless.
        """
        temp = self.health.read_temp()
        return temp is not None and temp >= self.cfg.thermal.hot_c

    def _drain_stt(self) -> None:
        while True:
            if self._stt_too_hot():
                return
            try:
                item = self._stt_q.get_nowait()
            except queue.Empty:
                return
            self._transcribe(item)

    def _stt_worker(self) -> None:
        lower_this_thread()  # whisper-cli inherits nice 19 from this thread
        while True:
            item = self._stt_q.get()
            while self._stt_too_hot():
                self.clock.sleep(STT_HOT_WAIT_S)
            try:
                self._transcribe(item)
            except Exception:  # noqa: BLE001 - a failed transcript must not stop the worker
                log.exception("speech-to-text failed for clip %s", item[0])

    def _iq_age(self) -> float | None:
        try:
            return self.runner.source.health().get("last_block_age_s")
        except Exception:  # noqa: BLE001 - a closed or failing source reports no age
            return None
