"""The whole rig in one object: scan loop, health, rollups, speech-to-text and the uplink.

``Service`` owns one ``Runner`` and adds the slower duties around it, all on the injected
clock, so ``sdrscan run`` (real time) and ``sdrscan simulate`` and the end-to-end test
(virtual time) share one code path:

- every ``thermal.sample_interval_s``: sample health, store it, ping the watchdog;
- after each kept voice clip: transcribe it when an engine is configured and the governor
  allows it (inline in simulations, on a worker thread on the rig);
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
from .clock import Clock
from .config import Config
from .dashboard import build_dashboard
from .health import HealthMonitor
from .iq.base import IqSource
from .llm import ProseEngine, make_prose
from .runner import Runner
from .store import Store
from .summarize import build_digest, encode_digest
from .tracker import Transmission
from .uplink import DigestPublisher, Ledger, make_target
from .voice.stt import SttEngine, make_stt

log = logging.getLogger("sdrscan.service")
STT_MAX_S = 30.0
HOUR_GRACE_S = 5.0  # let transmissions that end on the boundary close first
WATCHDOG_S = 120.0
WATCHDOG_EXIT = 70


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
    ) -> None:
        self.cfg = cfg
        self.clock = clock
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
            extra_flags=lambda: ["uplink_unconfigured"] if self.uplink_error else [],
            **fs_kw,
        )
        self.runner.mode_fn = self.health.mode
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
                publisher = DigestPublisher(
                    target, self.ledger, root / "outbox", clock, cfg.uplink
                )
        self.publisher = publisher
        self._stt_fixed = stt
        self.prose = prose or make_prose(cfg.prose)
        self.inline_stt = inline_stt
        self._stt_q: queue.Queue = queue.Queue(maxsize=cfg.stt.queue_max)
        self._stt_thread: threading.Thread | None = None
        self._next_health = clock.now()
        self._hour = rollup.hour_of(clock.now())
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

    def watchdog_check(self, exit_fn: typing.Callable[[int], None] = os._exit) -> bool:
        """Exit the process when the scan loop has been stuck for ``WATCHDOG_S``."""
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
        if rollup.day_of(hour_ts + rollup.H) > rollup.day_of(hour_ts):
            rollup.rollup_day(self.store, rollup.day_of(hour_ts))
        self.publish_digest()

    def make_digest(self) -> dict:
        now = self.clock.now()
        h = self.health.last or self.health.sample()
        digest = build_digest(
            self.store,
            h,
            now,
            plan=self.plan,
            site_id=self.cfg.site_id,
            include_transcripts=self.cfg.uplink.include_transcripts,
            now_state=self.runner.now_state(),
            uplink=self._uplink_state(),
        )
        if now - rollup.day_of(now) < rollup.H:  # once a day, just after midnight UTC
            digest["prose"] = self.prose.rewrite(digest, mode=self.health.mode())
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

    # -- speech-to-text ----------------------------------------------------------------
    def _stt(self) -> SttEngine:
        mode = self.health.mode()
        if self._stt_fixed is not None and mode == "normal":
            return self._stt_fixed
        return make_stt(self.cfg.stt, mode, work_dir=self.cfg.stage_root)

    def _on_tx(self, tx_id: int, tx: Transmission, pcm: np.ndarray | None) -> None:
        if pcm is None or tx.clip_id is None:
            return
        if self.cfg.stt.engine == "none" and self._stt_fixed is None:
            return
        pcm16 = (np.clip(pcm[: int(STT_MAX_S * 16_000)], -1, 1) * 32767).astype(np.int16)
        try:
            self._stt_q.put_nowait((tx.clip_id, tx_id, pcm16, tx.t_end))
        except queue.Full:
            log.info("speech-to-text queue full; clip %s keeps no transcript", tx.clip_id)
            return
        if not self.inline_stt and self._stt_thread is None:
            self._stt_thread = threading.Thread(target=self._stt_worker, daemon=True)
            self._stt_thread.start()

    def _transcribe(self, item) -> None:
        clip_id, tx_id, pcm16, t_end = item
        tr = self._stt().transcribe(pcm16.astype(np.float32) / 32767)
        if tr is None:
            return
        self.store.add_transcript(
            clip_id, tx_id, tr.engine, tr.text, tr.confidence, tr.rtf, self.clock.now()
        )
        self.clips.set_tier(clip_id, {"class": "voice", "confidence": tr.confidence})

    def _drain_stt(self) -> None:
        while True:
            try:
                item = self._stt_q.get_nowait()
            except queue.Empty:
                return
            self._transcribe(item)

    def _stt_worker(self) -> None:
        while True:
            item = self._stt_q.get()
            try:
                self._transcribe(item)
            except Exception:  # noqa: BLE001 - a failed transcript must not stop the worker
                log.exception("speech-to-text failed for clip %s", item[0])

    def _iq_age(self) -> float | None:
        try:
            return self.runner.source.health().get("last_block_age_s")
        except Exception:  # noqa: BLE001 - a closed or failing source reports no age
            return None
