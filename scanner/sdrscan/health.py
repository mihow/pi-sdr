"""Health monitor and thermal governor.

Nobody can touch the rig, so its health is part of the dashboard: temperature and governor
mode, dongle presence, how fresh the IQ stream is, disk and clip quota, database size,
clock state, memory and the uplink ledger. All system paths sit under ``sys_root`` so tests
can fake them, and a missing tool (``vcgencmd``) is not an error.

Governor modes (spec 5.13): ``normal``, ``hot`` (at least 70 °C), ``very_hot`` (at least
78 °C or a throttle flag) and ``critical`` (at least 82 °C). A mode is left only when the
temperature falls ``hysteresis_c`` below its threshold. ``no_dongle`` and ``disk_low`` are
flags next to the mode, because they change what is recorded, not how the tuner is paced.

The systemd watchdog is pinged only while IQ blocks keep arriving, so a hung USB read
restarts the service.
"""

from __future__ import annotations

import dataclasses
import os
import socket
import subprocess
import typing
from pathlib import Path

from . import __version__
from .clock import Clock
from .config import Config

MODES = ("normal", "hot", "very_hot", "critical")
RTL_USB_IDS = {("0bda", "2838"), ("0bda", "2832")}
WATCHDOG_MAX_IQ_AGE_S = 10.0
MIN_VALID_EPOCH = 1_767_225_600  # 2026-01-01: a Pi without an RTC boots earlier than this
MB = 1024 * 1024


@dataclasses.dataclass
class Health:
    ts: float
    up_s: float
    temp_c: float | None
    throttled: int | None
    mode: str
    flags: list[str]
    dongle_ok: bool
    iq_age_s: float | None
    disk_free_pct: float | None
    disk_free_mb: float | None
    quota_used_mb: float
    quota_mb: float
    db_mb: float
    clock_ok: bool
    rss_mb: float | None
    errors: int = 0
    drops: int = 0
    clip_frac: float = 0.0

    def to_row(self) -> dict:
        return {
            "ts": int(self.ts), "temp_c": self.temp_c, "throttled": self.throttled,
            "disk_free_mb": self.disk_free_mb, "quota_used_mb": round(self.quota_used_mb, 1),
            "dongle_ok": int(self.dongle_ok), "drops": self.drops,
            "clip_frac": round(self.clip_frac, 4), "mode": self.mode,
            "clock_ok": int(self.clock_ok),
        }  # fmt: skip

    def to_digest(self) -> dict:
        def r1(x):
            return None if x is None else round(float(x), 1)

        return {
            "up_s": int(self.up_s), "temp_c": r1(self.temp_c), "mode": self.mode,
            "flags": sorted(self.flags), "dongle": self.dongle_ok,
            "iq_age_s": r1(self.iq_age_s), "disk_free_pct": r1(self.disk_free_pct),
            "quota_used_mb": r1(self.quota_used_mb), "quota_mb": r1(self.quota_mb),
            "db_mb": r1(self.db_mb), "clock_ok": self.clock_ok, "rss_mb": r1(self.rss_mb),
            "errors": int(self.errors), "clip_pct": r1(self.clip_frac * 100),
            "version": __version__,
        }  # fmt: skip


class HealthMonitor:
    def __init__(
        self,
        cfg: Config,
        clock: Clock,
        sys_root: Path = Path("/sys"),
        *,
        proc_root: Path = Path("/proc"),
        iq_age: typing.Callable[[], float | None] | None = None,
        quota_used: typing.Callable[[], int] | None = None,
        status: typing.Callable[[], dict] | None = None,
        run: typing.Callable[..., subprocess.CompletedProcess] = subprocess.run,
        statvfs: typing.Callable[[str], typing.Any] = os.statvfs,
        notify: typing.Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.sys_root = Path(sys_root)
        self.proc_root = Path(proc_root)
        self._iq_age = iq_age or (lambda: None)
        self._quota_used = quota_used or (lambda: 0)
        self._status = status or (lambda: {})
        self._run = run
        self._statvfs = statvfs
        self._notify = notify or _sd_notify
        self._start = clock.monotonic()
        self._level = 0
        self.last: Health | None = None

    # -- sampling ----------------------------------------------------------------------
    def sample(self) -> Health:
        temp = self._temp()
        throttled = self._throttled()
        self._govern(temp, throttled)
        dongle = self._dongle()
        free_pct, free_mb = self._disk()
        quota = self.cfg.quota
        flags = []
        if not dongle:
            flags.append("no_dongle")
        if free_pct is not None and free_pct < quota.free_evict_frac * 100:
            flags.append("disk_low")
        now = self.clock.now()
        clock_ok = now >= MIN_VALID_EPOCH
        if not clock_ok:
            flags.append("clock_unsynced")
        st = self._status()
        if st.get("mode") == "no_dongle" and "no_dongle" not in flags:
            flags.append("dongle_busy")
        db = self.cfg.data_root / "db" / "scan.sqlite"
        db_mb = sum(
            p.stat().st_size for p in (db, db.with_name(db.name + "-wal")) if p.exists()
        ) / MB
        h = Health(
            ts=now,
            up_s=self.clock.monotonic() - self._start,
            temp_c=temp,
            throttled=throttled,
            mode=self.mode(),
            flags=flags,
            dongle_ok=dongle,
            iq_age_s=self._iq_age(),
            disk_free_pct=free_pct,
            disk_free_mb=free_mb,
            quota_used_mb=self._quota_used() / MB,
            quota_mb=quota.clip_quota_bytes / MB,
            db_mb=db_mb,
            clock_ok=clock_ok,
            rss_mb=self._rss_mb(),
            errors=int(st.get("errors", 0)),
            clip_frac=float(st.get("last_clip_frac", 0.0)),
        )
        self.last = h
        return h

    def mode(self) -> str:
        return MODES[self._level]

    def notify_watchdog(self) -> bool:
        age = self._iq_age()
        if age is None or age > WATCHDOG_MAX_IQ_AGE_S:
            return False
        self._notify("WATCHDOG=1")
        return True

    # -- internals ---------------------------------------------------------------------
    def _govern(self, temp: float | None, throttled: int | None) -> None:
        t = self.cfg.thermal
        limits = (float("-inf"), t.hot_c, t.very_hot_c, t.critical_c)
        if temp is None:
            target = 0
        else:
            target = max(i for i, lim in enumerate(limits) if temp >= lim)
        if throttled:  # any current or past under-voltage or throttle bit
            target = max(target, 2)
        if target >= self._level:
            self._level = target
            return
        while self._level > target and (
            temp is None or temp < limits[self._level] - t.hysteresis_c
        ):
            self._level -= 1
        if throttled:
            self._level = max(self._level, 2)

    def _temp(self) -> float | None:
        p = self.sys_root / "class/thermal/thermal_zone0/temp"
        try:
            return int(p.read_text().strip()) / 1000.0
        except (OSError, ValueError):
            return None

    def _throttled(self) -> int | None:
        try:
            proc = self._run(["vcgencmd", "get_throttled"], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        out = proc.stdout.decode() if isinstance(proc.stdout, bytes) else str(proc.stdout)
        if proc.returncode != 0 or "=" not in out:
            return None
        try:
            return int(out.strip().split("=", 1)[1], 16)
        except ValueError:
            return None

    def _dongle(self) -> bool:
        root = self.sys_root / "bus/usb/devices"
        if not root.is_dir():
            return False
        for d in root.iterdir():
            try:
                vid = (d / "idVendor").read_text().strip()
                pid = (d / "idProduct").read_text().strip()
            except OSError:
                continue
            if (vid, pid) in RTL_USB_IDS:
                return True
        return False

    def _disk(self) -> tuple[float | None, float | None]:
        try:
            self.cfg.data_root.mkdir(parents=True, exist_ok=True)
            st = self._statvfs(str(self.cfg.data_root))
        except OSError:
            return None, None
        if not st.f_blocks:
            return None, None
        return (
            round(st.f_bavail / st.f_blocks * 100, 1),
            round(st.f_bavail * st.f_frsize / MB, 1),
        )

    def _rss_mb(self) -> float | None:
        try:
            for line in (self.proc_root / "self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
        except (OSError, ValueError, IndexError):
            return None
        return None


def _sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.send(msg.encode())
    except OSError:
        pass  # the watchdog then restarts the service, which is the intended outcome
