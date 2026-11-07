"""Command line for the scanner and its file-to-file stages.

``run``, ``simulate``, ``bench`` and ``health`` drive the service. ``capture``, ``detect``,
``demod``, ``gate``, ``stt``, ``digest`` and ``dashboard`` each read one file and write one,
so a stage can be tested by hand from a recording (see the README). ``corpus cut`` slices a
short clip out of a large capture.
"""

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
from .config import STT_NAMES, Config, load_config

DEFAULT_CONFIG = Path("/etc/sdrscan/config.yaml")
LIVE_BIND_RETRY_S = 15.0  # how often the live page retries a tailnet address not up yet
SIM_EPOCH = 1_780_272_000.0  # 2026-06-01 00:00 UTC, a Monday
DONGLE_LOST_EXIT = 71  # the radio stayed missing; restart the container to rescan USB


def _config(path: str | None) -> Config:
    if path:
        return load_config(Path(path))
    return load_config(DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None)


# -- run ---------------------------------------------------------------------------------
def bind_with_retry(factory, stop, retry_s: float = LIVE_BIND_RETRY_S):
    """Build and start a server, retrying while its address cannot be bound yet.

    Returns the started server, or None when ``stop`` is set first.
    """
    log = logging.getLogger("sdrscan")
    warned = False
    while not stop.is_set():
        try:
            server = factory()
        except OSError as exc:
            if not warned:
                log.warning("live page not bound yet (%s); retrying every %.0f s", exc, retry_s)
                warned = True
            stop.wait(retry_s)
            continue
        server.start()
        if warned:
            log.info("live page bound")
        return server
    return None


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
    live_holder: list = []
    if cfg.live.enabled and cfg.live.bind_address:

        def make_live():
            return LiveServer(
                svc.store,
                svc.clips,
                svc.ledger,
                cfg.live.bind_address,
                cfg.live.port,
                # The live page is reachable only over the tailnet, so it shows transcripts
                # and call signs whatever the uplink setting.
                digest_fn=lambda: svc.make_digest(include_transcripts=True),
                health_fn=_health_fn(svc),
            )

        # Bound in the background: the tailnet address may not exist yet at boot, and the
        # page must never stop the scanner.
        threading.Thread(
            target=lambda: live_holder.append(bind_with_retry(make_live, stop)),
            name="sdrscan-live-bind",
            daemon=True,
        ).start()
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
        for live in live_holder:
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

    cfg = _config(args.config)
    plan = BandPlan.load(cfg.bandplan_path, cfg.labels_path)
    if args.events:
        import tempfile

        from .clock import VirtualClock
        from .stages import DETECTIONS_SCHEMA, read_jsonl, replay_detections

        records = read_jsonl(Path(args.events), DETECTIONS_SCHEMA)
        start = args.start if args.start is not None else SIM_EPOCH
        clock = VirtualClock(start)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(Path(tmp) / "replay.sqlite", clock)
            store.migrate()
            replay_detections(records, cfg=cfg, plan=plan, store=store, clock=clock)
            now = args.now if args.now is not None else clock.now()
            sizes = _write_digest(store, None, now, plan, cfg, args)
            store.close()
    else:
        store = Store.open(Path(args.db), RealClock())
        now = (
            args.now
            if args.now is not None
            else (
                store.db.execute("SELECT MAX(t_end) FROM transmissions").fetchone()[0]
                or RealClock().now()
            )
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
        sizes = _write_digest(store, health, float(now), plan, cfg, args)
    print(json.dumps(sizes))
    return 0


def _write_digest(store, health, now: float, plan, cfg: Config, args) -> dict:
    from .summarize import build_digest, encode_digest

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
    gz = gzip.compress(raw, 9, mtime=0)
    (out / "digest.json").write_bytes(raw)
    (out / "digest.json.gz").write_bytes(gz)
    return {"raw_bytes": len(raw), "gz_bytes": len(gz)}


def cmd_dashboard(args) -> int:
    from .dashboard import build_dashboard

    digest = Path(args.digest)
    path = build_dashboard(digest.name, args.mode, Path(args.out))
    print(path)
    return 0


# -- file-to-file stages -----------------------------------------------------------------
_SUFFIX = {"k": 1e3, "m": 1e6, "g": 1e9}


def _hz(text: str) -> int:
    """``443150000``, ``443.15M``, ``443.15MHz`` or ``162.475e6`` as integer Hz."""
    t = text.strip().lower().removesuffix("hz")
    mult = _SUFFIX.get(t[-1:], 1.0)
    if mult != 1.0:
        t = t[:-1]
    try:
        return int(round(float(t) * mult))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a frequency: {text!r}") from None


def _plan(args):
    from .bandplan import BandPlan

    cfg = _config(args.config)
    return cfg, BandPlan.load(cfg.bandplan_path, cfg.labels_path)


def _open_slice(args):
    from .iq.file import open_iq

    f = open_iq(Path(args.iq))
    start, stop = f.sample_range(args.offset, args.seconds)
    return f, start, stop


def cmd_capture(args) -> int:
    import datetime

    from .iq.file import IqWriter
    from .iq.rtl import RtlSource
    from .stages import capture

    writer = IqWriter(
        Path(args.out), rate=args.rate, center_hz=args.freq, gain_db=args.gain, fmt=args.format
    )
    started = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    source = RtlSource(RealClock(), device_index=args.device, rate=args.rate, threaded=False)
    try:
        health = capture(
            source, writer, center_hz=args.freq, gain_db=args.gain, seconds=args.seconds
        )
    finally:
        writer.close(extra={"device": "RTL-SDR", "recorded_at": started})
    print(json.dumps({"out": str(writer.path), "n_samples": writer.n_samples, **health}))
    return 0


def cmd_detect(args) -> int:
    from .stages import detect, summarize_detections, write_jsonl

    cfg, plan = _plan(args)
    f, start, stop = _open_slice(args)
    records = list(detect(f, plan, cfg.detect, start=start, stop=stop, hop_s=args.hop_s))
    write_jsonl(Path(args.out), records)
    print(json.dumps(summarize_detections(records), sort_keys=True))
    return 0


def cmd_demod(args) -> int:
    from .stages import demod, demod_channel
    from .voice.stt import write_wav

    _, plan = _plan(args)
    f, start, stop = _open_slice(args)
    channel = demod_channel(plan, args.freq, args.mode)
    audio = demod(f, channel, start=start, stop=stop)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_wav(out, audio)
    print(
        json.dumps(
            {
                "out": str(out),
                "hz": channel.hz,
                "mode": channel.mode,
                "seconds": round(len(audio) / 16_000, 3),
            }
        )
    )
    return 0


def cmd_gate(args) -> int:
    from .stages import gate, write_jsonl
    from .voice.stt import read_wav

    pcm, rate = read_wav(Path(args.wav))
    records = gate(pcm, rate=rate)
    write_jsonl(Path(args.out), records)
    print(json.dumps(records[-1], sort_keys=True))
    return 0


def cmd_stt(args) -> int:
    import dataclasses

    from .stages import transcribe
    from .voice.stt import MODEL_FIELD, make_stt, read_wav

    cfg = _config(args.config)
    stt_cfg = cfg.stt
    if args.engine:
        stt_cfg = dataclasses.replace(stt_cfg, engine=args.engine)
    elif args.binary and stt_cfg.engine in ("none", "auto"):
        stt_cfg = dataclasses.replace(stt_cfg, engine="whisper_cpp")
    if args.binary:
        stt_cfg = dataclasses.replace(stt_cfg, whisper_binary=Path(args.binary))
    if args.model and stt_cfg.engine in MODEL_FIELD:
        stt_cfg = dataclasses.replace(stt_cfg, **{MODEL_FIELD[stt_cfg.engine]: Path(args.model)})
    if args.threads:
        stt_cfg = dataclasses.replace(stt_cfg, threads=args.threads)
    pcm, rate = read_wav(Path(args.wav))
    engine = make_stt(stt_cfg, "normal", choice_path=_choice_path(cfg))
    rec = transcribe(pcm, engine, rate=rate)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, sort_keys=True) + "\n")
    print(json.dumps(rec, sort_keys=True))
    return 0


def _choice_path(cfg) -> Path:
    return cfg.stt.choice_path or cfg.data_root / "stt-choice.json"


def cmd_stt_bench(args) -> int:
    import dataclasses

    from .voice import stt_bench as sb

    cfg = _config(args.config)
    stt_cfg = cfg.stt
    if args.threads:
        stt_cfg = dataclasses.replace(stt_cfg, threads=args.threads)
    lim = sb.Limits.from_cfg(stt_cfg)
    for key in ("max_rtf", "max_rss_mb", "max_model_mb", "max_noise_shown_frac"):
        if getattr(args, key) is not None:
            lim = dataclasses.replace(lim, **{key: getattr(args, key)})
    cands = sb.parse_candidates(args.engines, stt_cfg)
    choice, details = sb.run_bench(
        Path(args.reference), cands, stt_cfg, lim, log=lambda m: print(m, file=sys.stderr)
    )
    out = Path(args.out) if args.out else _choice_path(cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(choice, indent=1, sort_keys=True) + "\n")
    tmp.replace(out)
    if args.details:
        Path(args.details).write_text(json.dumps(details, indent=1) + "\n")
    print(sb.format_table(choice))
    print(f"wrote {out}")
    return 0


def cmd_corpus_cut(args) -> int:
    from .corpus import cut_clip

    res = cut_clip(
        Path(args.src),
        Path(args.out),
        offset_s=args.offset,
        seconds=args.seconds,
        scale=args.scale,
    )
    print(json.dumps({"file": Path(args.src).name, **res}, sort_keys=True))
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


def cmd_recall_calls(args) -> int:
    """Re-scan stored transcripts for call signs the extractor did not find at the time."""
    from .bandplan import BandPlan
    from .store import Store

    cfg = _config(args.config)
    plan = BandPlan.load(cfg.bandplan_path, cfg.labels_path)
    db = Path(args.db) if args.db else cfg.data_root / "db" / "scan.sqlite"
    store = Store.open(db, RealClock())
    added = store.recall_calls(lambda hz: plan.band_for(hz).id)
    store.close()
    print(json.dumps({"added": added}))
    return 0


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

    c = sub.add_parser("capture", help="record IQ from the RTL-SDR to a file")
    c.add_argument("--freq", type=_hz, required=True, help="center, e.g. 443.15M")
    c.add_argument("--rate", type=int, default=2_400_000)
    c.add_argument("--seconds", type=float, required=True)
    c.add_argument("--gain", type=float, required=True, help="manual gain in dB")
    c.add_argument("--format", choices=("cu8", "cf32"), default="cu8")
    c.add_argument("--device", type=int, default=0)
    c.add_argument("--out", required=True)
    c.set_defaults(fn=cmd_capture)

    def iq_args(sp) -> None:
        sp.add_argument("--iq", required=True, help=".cu8/.raw/.cf32 with a sidecar, or SigMF")
        sp.add_argument("--offset", type=float, default=0.0, help="seconds into the file")
        sp.add_argument("--seconds", type=float, help="default: to the end")
        sp.add_argument("--config")

    t = sub.add_parser("detect", help="find signals in an IQ file (detections JSONL)")
    iq_args(t)
    t.add_argument("--hop-s", type=float, default=0.1)
    t.add_argument("--out", required=True)
    t.set_defaults(fn=cmd_detect)

    m = sub.add_parser("demod", help="demodulate one channel of an IQ file to a WAV")
    iq_args(m)
    m.add_argument("--freq", type=_hz, required=True)
    m.add_argument("--mode", choices=("nfm", "am"), help="default: the band plan's mode")
    m.add_argument("--out", required=True)
    m.set_defaults(fn=cmd_demod)

    g = sub.add_parser("gate", help="classify a WAV as voice, data or noise (segments JSONL)")
    g.add_argument("--wav", required=True)
    g.add_argument("--out", required=True)
    g.set_defaults(fn=cmd_gate)

    x = sub.add_parser("stt", help="transcribe a WAV (transcript JSON)")
    x.add_argument("--wav", required=True)
    x.add_argument("--out", required=True)
    x.add_argument("--config")
    x.add_argument("--engine", choices=STT_NAMES)
    x.add_argument("--binary", help="whisper-cli")
    x.add_argument("--model", help="the engine's model file or directory")
    x.add_argument("--threads", type=int)
    x.set_defaults(fn=cmd_stt)

    sb = sub.add_parser(
        "stt-bench",
        help="measure the speech-to-text engines on this host and choose one",
        description="Runs each engine over a private reference set and writes the choice "
        "that stt.engine: auto reads. Rule: lowest WER among engines within max_rtf, "
        "max_rss_mb, max_model_mb and max_noise_shown_frac; ties go to the smaller RSS.",
    )
    sb.add_argument("--reference", required=True, help="reference.json of the reference set")
    sb.add_argument("--config")
    sb.add_argument(
        "--engines",
        help="comma-separated name[:key=value...], e.g. "
        "whisper_cpp:audio_ctx=768,faster_whisper:model=/m/small.en (default: all)",
    )
    sb.add_argument("--threads", type=int, help="default: stt.threads")
    sb.add_argument("--out", help="default: stt.choice_path or <data_root>/stt-choice.json")
    sb.add_argument("--details", help="also write each engine's per-segment text (private)")
    for key in ("max_rtf", "max_rss_mb", "max_model_mb", "max_noise_shown_frac"):
        sb.add_argument(f"--{key.replace('_', '-')}", dest=key, type=float)
    sb.set_defaults(fn=cmd_stt_bench)

    d = sub.add_parser("digest", help="build a digest from a database or a detections file")
    src = d.add_mutually_exclusive_group(required=True)
    src.add_argument("--db")
    src.add_argument("--events", help="detections JSONL from 'sdrscan detect'")
    d.add_argument("--out", required=True)
    d.add_argument("--config")
    d.add_argument("--now", type=float)
    d.add_argument("--start", type=float, help="epoch of the detections' t_s 0 (--events)")
    d.add_argument("--transcripts", action="store_true")
    d.set_defaults(fn=cmd_digest)

    k = sub.add_parser("corpus", help="tools for a private IQ corpus")
    ksub = k.add_subparsers(dest="corpus_cmd", required=True)
    kc = ksub.add_parser("cut", help="slice a short clip out of a large capture")
    kc.add_argument("--src", required=True)
    kc.add_argument("--offset", type=float, default=0.0)
    kc.add_argument("--seconds", type=float, default=2.0)
    kc.add_argument("--scale", type=float, help="sample gain; default auto for cf32 to cu8")
    kc.add_argument("--out", required=True, help=".cu8 or .cf32")
    kc.set_defaults(fn=cmd_corpus_cut)

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

    rc = sub.add_parser("recall-calls", help="find call signs in transcripts stored earlier")
    rc.add_argument("--config")
    rc.add_argument("--db")
    rc.set_defaults(fn=cmd_recall_calls)

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
