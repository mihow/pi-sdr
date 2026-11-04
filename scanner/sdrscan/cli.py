"""Command line: ``sdrscan run | simulate | digest | dashboard | bench | health``."""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import signal
import sys
import threading
from pathlib import Path

from .clock import RealClock
from .config import Config, load_config

DEFAULT_CONFIG = Path("/etc/sdrscan/config.yaml")
SIM_EPOCH = 1_780_272_000.0  # 2026-06-01 00:00 UTC, a Monday
DONGLE_LOST_EXIT = 71  # the radio stayed missing; restart the container to rescan USB


def _config(path: str | None) -> Config:
    if path:
        return load_config(Path(path))
    return load_config(DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None)


# -- run ---------------------------------------------------------------------------------
def cmd_run(args) -> int:
    from .live import LiveServer
    from .service import Service

    cfg = _config(args.config)
    clock = RealClock()
    if args.source == "rtl":
        from .iq.rtl import RtlSource

        source = RtlSource(clock=clock)
        spectrum = "iq"
    elif args.source == "file":
        from .iq.file import FileSource

        source = FileSource(Path(args.iq), loop=True)
        spectrum = "iq"
    else:
        from .iq.scene import Scene
        from .iq.synthetic import SyntheticSource

        source = SyntheticSource(Scene.load(Path(args.scene), clock), clock)
        spectrum = args.spectrum
    svc = Service(cfg, clock, source, spectrum=spectrum, sys_root=Path(args.sys_root))
    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    live = None
    if cfg.live.enabled and cfg.live.bind_address:
        live = LiveServer(
            svc.store,
            svc.clips,
            svc.ledger,
            cfg.live.bind_address,
            cfg.live.port,
            digest_fn=svc.make_digest,
            health_fn=_health_fn(svc),
        )
        live.start()
    svc.start_watchdog(stop)
    logging.getLogger("sdrscan").info("scanning; source %s", args.source)
    from .runner import DongleLost

    code = 0
    try:
        svc.run(stop=stop)
    except DongleLost as exc:
        # A clean exit: systemd restarts the container, which rescans the USB bus.
        logging.getLogger("sdrscan").error("%s; exiting for a restart", exc)
        code = DONGLE_LOST_EXIT
    finally:
        if live is not None:
            live.shutdown()
        svc.shutdown()
    return code


def _health_fn(svc):
    def fn() -> dict:
        h = svc.health.last
        return {"ok": True, "health": h.to_digest() if h else None}

    return fn


# -- simulate ----------------------------------------------------------------------------
def cmd_simulate(args) -> int:
    from .clips import WavEncoder
    from .clock import VirtualClock
    from .config import DutyConfig, UplinkConfig
    from .iq.scene import Scene
    from .iq.synthetic import SyntheticSource
    from .service import Service
    from .voice.stt import FakeStt

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    clock = VirtualClock(SIM_EPOCH)
    scene = Scene.load(Path(args.scene), clock)
    base = _config(args.config) if args.config else Config()
    cfg = Config(
        **{
            **{f: getattr(base, f) for f in base.__dataclass_fields__},
            "data_root": out,
            "stage_root": out / ".stage",
            "duty": DutyConfig(**{**base.duty.__dict__, "dwell_block_s": 0.5})
            if args.spectrum == "psd"
            else base.duty,
            "uplink": UplinkConfig(
                **{
                    **base.uplink.__dict__,
                    "target": "file",
                    "path": out,
                    "include_transcripts": True,
                }
            ),
        }
    )
    wall = RealClock()
    t_start = wall.monotonic()
    sys_root = Path(args.sys_root) if args.sys_root else _fake_sys(out / ".sys")
    svc = Service(
        cfg,
        clock,
        SyntheticSource(scene, clock),
        db_path=out / "scan.sqlite",
        spectrum=args.spectrum,
        encoder=WavEncoder(),
        inline_stt=True,
        stt=FakeStt(),
        sys_root=sys_root,
    )
    hours = args.hours if args.hours is not None else args.days * 24
    svc.run(until=clock.now() + hours * 3600 + 60)
    svc.publish_digest()
    svc.shutdown()
    page = out / "index.html"  # written by the publisher's file target, next to the digest
    gz = (out / "digest.json.gz").read_bytes()
    report = {
        "hours": hours,
        "seconds": round(wall.monotonic() - t_start, 1),
        "digests": svc.digests,
        "digest_gz_bytes": len(gz),
        "page_gz_bytes": len(gzip.compress(page.read_bytes(), 9, mtime=0)),
        "out": str(out),
    }
    print(json.dumps(report, sort_keys=True))
    return 0


def _fake_sys(root: Path) -> Path:
    """A sysfs stand-in with a cool SoC and a present dongle, for simulations."""
    (root / "class/thermal/thermal_zone0").mkdir(parents=True, exist_ok=True)
    (root / "class/thermal/thermal_zone0/temp").write_text("48000\n")
    usb = root / "bus/usb/devices/1-1"
    usb.mkdir(parents=True, exist_ok=True)
    (usb / "idVendor").write_text("0bda\n")
    (usb / "idProduct").write_text("2838\n")
    return root


# -- digest, dashboard -------------------------------------------------------------------
def cmd_digest(args) -> int:
    from .bandplan import BandPlan
    from .store import Store
    from .summarize import build_digest, encode_digest

    cfg = _config(args.config)
    clock = RealClock()
    store = Store.open(Path(args.db), clock)
    now = (
        args.now
        if args.now is not None
        else (store.db.execute("SELECT MAX(t_end) FROM transmissions").fetchone()[0] or clock.now())
    )
    row = store.db.execute("SELECT * FROM health ORDER BY ts DESC LIMIT 1").fetchone()
    health = None
    if row is not None:
        health = {
            "mode": row["mode"],
            "temp_c": row["temp_c"],
            "dongle": bool(row["dongle_ok"]),
            "quota_used_mb": row["quota_used_mb"],
            "clock_ok": bool(row["clock_ok"]),
        }
    plan = BandPlan.load(cfg.bandplan_path, cfg.labels_path)
    d = build_digest(
        store,
        health,
        float(now),
        plan=plan,
        site_id=cfg.site_id,
        include_transcripts=args.transcripts,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    raw = encode_digest(d)
    (out / "digest.json").write_bytes(raw)
    (out / "digest.json.gz").write_bytes(gzip.compress(raw, 9, mtime=0))
    print(json.dumps({"raw_bytes": len(raw), "gz_bytes": len(gzip.compress(raw, 9, mtime=0))}))
    return 0


def cmd_dashboard(args) -> int:
    from .dashboard import build_dashboard

    digest = Path(args.digest)
    path = build_dashboard(digest.name, args.mode, Path(args.out))
    print(path)
    return 0


# -- bench, health -----------------------------------------------------------------------
def cmd_bench(args) -> int:
    from .bench import run_bench

    res = run_bench(seconds=args.seconds)
    if args.json:
        print(json.dumps(res, sort_keys=True))
    else:
        for k, v in sorted(res.items()):
            print(f"{k:28s} {v}")
    return 0


def cmd_health(args) -> int:
    """Exit 0 when the service wrote a health row recently (the container HEALTHCHECK)."""
    from .store import Store

    cfg = _config(args.config)
    db = Path(args.db) if args.db else cfg.data_root / "db" / "scan.sqlite"
    if not db.exists():
        print(json.dumps({"ok": False, "reason": "no database yet"}))
        return 1
    store = Store.open(db, RealClock())
    row = store.db.execute("SELECT * FROM health ORDER BY ts DESC LIMIT 1").fetchone()
    if row is None:
        print(json.dumps({"ok": False, "reason": "no health sample yet"}))
        return 1
    age = RealClock().now() - row["ts"]
    ok = age <= args.max_age_s and bool(row["dongle_ok"])
    print(
        json.dumps(
            {
                "ok": ok,
                "age_s": round(age, 1),
                "mode": row["mode"],
                "dongle_ok": bool(row["dongle_ok"]),
                "temp_c": row["temp_c"],
            }
        )
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sdrscan", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="scan until stopped")
    r.add_argument("--config")
    r.add_argument("--source", choices=("rtl", "file", "synthetic"), default="rtl")
    r.add_argument("--scene", help="scene YAML for --source synthetic")
    r.add_argument("--iq", help="cf32 file for --source file")
    r.add_argument("--spectrum", choices=("iq", "psd", "hybrid"), default="hybrid")
    r.add_argument("--sys-root", default="/sys")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("simulate", help="run a scene on a virtual clock")
    s.add_argument("--scene", required=True)
    s.add_argument("--days", type=float, default=1.0)
    s.add_argument("--hours", type=float)
    s.add_argument("--out", required=True)
    s.add_argument("--config")
    s.add_argument("--spectrum", choices=("psd", "hybrid"), default="psd")
    s.add_argument("--sys-root", help="default: a fake sysfs with a healthy rig")
    s.set_defaults(fn=cmd_simulate)

    d = sub.add_parser("digest", help="build a digest from a database")
    d.add_argument("--db", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--config")
    d.add_argument("--now", type=float)
    d.add_argument("--transcripts", action="store_true")
    d.set_defaults(fn=cmd_digest)

    b = sub.add_parser("dashboard", help="build the static dashboard page")
    b.add_argument("--digest", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--mode", choices=("static", "live"), default="static")
    b.set_defaults(fn=cmd_dashboard)

    e = sub.add_parser("bench", help="time the hot paths on this machine")
    e.add_argument("--json", action="store_true")
    e.add_argument("--seconds", type=float, default=1.0)
    e.set_defaults(fn=cmd_bench)

    h = sub.add_parser("health", help="exit 0 when the running service is healthy")
    h.add_argument("--config")
    h.add_argument("--db")
    h.add_argument("--max-age-s", type=float, default=120.0)
    h.set_defaults(fn=cmd_health)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if args.cmd == "run" and args.source == "synthetic" and not args.scene:
        p.error("--source synthetic needs --scene")
    if args.cmd == "run" and args.source == "file" and not args.iq:
        p.error("--source file needs --iq")
    return int(args.fn(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
