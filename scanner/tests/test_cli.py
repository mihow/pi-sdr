from __future__ import annotations

import gzip
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sdrscan.cli import main

SCENES = Path(__file__).parent / "scenes"
ROOT = Path(__file__).resolve().parents[1]
SLOW = 10 if os.environ.get("SDRSCAN_SLOW_HOST") else 1  # qemu runs far slower


def test_simulate_writes_db_digest_and_page(tmp_path, capsys):
    out = tmp_path / "sim"
    t0 = time.monotonic()
    assert (
        main(
            [
                "simulate",
                "--scene",
                str(SCENES / "week_basic.yaml"),
                "--hours",
                "1",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    if not os.environ.get("SDRSCAN_SLOW_HOST"):  # set in the qemu test image
        assert time.monotonic() - t0 < 30
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (out / "scan.sqlite").exists() and (out / "index.html").exists()
    d = json.loads(gzip.decompress((out / "digest.json.gz").read_bytes()))
    assert d["v"] == 1 and d["windows"]["hour"]["top"]
    assert report["digest_gz_bytes"] <= 4096 and report["page_gz_bytes"] <= 10_000
    db = sqlite3.connect(out / "scan.sqlite")
    assert db.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] > 0


def test_digest_and_dashboard_commands(tmp_path, capsys):
    out = tmp_path / "sim"
    main(
        [
            "simulate",
            "--scene",
            str(SCENES / "week_basic.yaml"),
            "--hours",
            "0.3",
            "--out",
            str(out),
        ]
    )
    assert main(["digest", "--db", str(out / "scan.sqlite"), "--out", str(tmp_path / "d")]) == 0
    assert json.loads((tmp_path / "d" / "digest.json").read_text())["v"] == 1
    assert (
        main(
            [
                "dashboard",
                "--digest",
                str(tmp_path / "d" / "digest.json.gz"),
                "--out",
                str(tmp_path / "w"),
            ]
        )
        == 0
    )
    assert (tmp_path / "w" / "index.html").exists()


def test_bench_json(capsys):
    assert main(["bench", "--json", "--seconds", "0.05"]) == 0
    res = json.loads(capsys.readouterr().out)
    for key in ("hop_analyze_ms_per_100ms", "demod_ms_per_channel_per_100ms", "vad_ms_per_frame"):
        assert res[key] > 0


def test_run_synthetic_exits_cleanly_on_sigterm(tmp_path, sys_root):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"data_root: {tmp_path / 'data'}\nstage_root: {tmp_path / 'stage'}\n")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sdrscan",
            "run",
            "--config",
            str(cfg),
            "--source",
            "synthetic",
            "--scene",
            str(SCENES / "week_basic.yaml"),
            "--spectrum",
            "psd",
            "--sys-root",
            str(sys_root),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    db = tmp_path / "data" / "db" / "scan.sqlite"
    deadline = time.monotonic() + 30 * SLOW
    while not db.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(2.0 * SLOW)
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30 * SLOW)
    assert proc.returncode == 0, out.decode()[-2000:]
    rows = sqlite3.connect(db).execute("SELECT COUNT(*) FROM health").fetchone()[0]
    assert rows >= 1, "the store was flushed on the way out"


def test_health_command(tmp_path, capsys):
    assert main(["health", "--db", str(tmp_path / "missing.sqlite")]) == 1


@pytest.mark.parametrize("args", [["run", "--source", "synthetic"], ["run", "--source", "file"]])
def test_run_argument_errors(args):
    with pytest.raises(SystemExit):
        main(args)


def test_live_page_bind_is_retried_until_the_address_exists():
    # At boot the tailnet address appears only once Tailscale is up; binding it earlier
    # raised EADDRNOTAVAIL and crashed the whole scanner. The page now retries instead.
    import threading

    from sdrscan.cli import bind_with_retry

    attempts = []

    class Server:
        started = False

        def start(self):
            self.started = True

    def factory():
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError(99, "Cannot assign requested address")
        return Server()

    stop = threading.Event()
    server = bind_with_retry(factory, stop, retry_s=0.0)
    assert server is not None and server.started
    assert len(attempts) == 3


def test_live_page_bind_gives_up_when_stopping():
    import threading

    from sdrscan.cli import bind_with_retry

    def factory():
        raise OSError(99, "Cannot assign requested address")

    stop = threading.Event()
    stop.set()
    assert bind_with_retry(factory, stop, retry_s=0.0) is None
