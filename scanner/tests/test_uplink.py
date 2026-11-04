from __future__ import annotations

import gzip
import http.server
import json
import logging
import threading

import pytest

from sdrscan.config import UplinkConfig
from sdrscan.store import Store
from sdrscan.uplink import DigestPublisher, FileTarget, HttpPutTarget, Ledger, load_secrets

from .conftest import T0

H = 3600
SECRET = "sig-Zq81-very-secret-signature"


class _Server:
    """A local PUT sink. ``fail`` makes it answer 500."""

    def __init__(self) -> None:
        self.fail = False
        self.got: list[tuple[str, bytes, dict]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if outer.fail:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                outer.got.append((self.path, body, dict(self.headers)))
                self.send_response(200)
                self.send_header("ETag", '"abc"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.close()


@pytest.fixture
def store(tmp_path, vclock):
    s = Store.open(tmp_path / "scan.sqlite", vclock)
    s.migrate()
    yield s
    s.close()


def _digest(t: float, extra: int = 0) -> dict:
    return {"v": 1, "generated_at": int(t), "health": {"mode": "normal"}, "pad": "x" * extra}


def _publisher(server, store, vclock, tmp_path, **cfg):
    env = tmp_path / "uplink.env"
    env.write_text(
        f"SDRSCAN_UPLINK_URL=http://127.0.0.1:{server.port}/p/{{name}}?X-Signature={SECRET}\n"
    )
    ucfg = UplinkConfig(target="http_put", secrets_env=env, **cfg)
    target = HttpPutTarget.from_secrets(load_secrets(env))
    ledger = Ledger(store, ucfg, vclock)
    return DigestPublisher(target, ledger, tmp_path / "outbox", vclock, ucfg), ledger


def test_hourly_publish_sends_one_gzip_object_and_charges_the_ledger(
    server, store, vclock, tmp_path
):
    pub, ledger = _publisher(server, store, vclock, tmp_path)
    res = pub.publish(_digest(vclock.now()), vclock.now())
    assert res.sent == ["digest.json.gz"]
    path, body, headers = server.got[0]
    assert path.startswith("/p/digest.json.gz")
    assert json.loads(gzip.decompress(body))["v"] == 1
    assert headers["Content-Encoding"] == "gzip"
    charged = ledger.month_bytes()
    assert charged >= len(body) + 6_000 + 100  # body + headers + TLS estimate
    assert charged < len(body) + 6_000 + 1_500
    vclock.advance(600)
    assert pub.publish(_digest(vclock.now()), vclock.now()).sent == []  # not due yet
    vclock.advance(3000)
    assert pub.publish(_digest(vclock.now()), vclock.now()).sent == ["digest.json.gz"]


def test_cadence_slows_above_80_percent_and_stops_at_the_cap(server, store, vclock, tmp_path):
    pub, ledger = _publisher(server, store, vclock, tmp_path, cap_bytes=100_000)
    ledger.charge("test", 81_000, 0, 200)
    assert ledger.state() == "slow"
    sent = []
    for _ in range(6):
        sent.append(pub.publish(_digest(vclock.now()), vclock.now()).sent)
        vclock.advance(H)
    assert sum(1 for s in sent if s) == 2  # hours 0 and 3
    ledger.charge("test", 30_000, 0, 200)
    assert ledger.state() == "capped"
    server.got.clear()
    names = []
    for _ in range(30):
        names += pub.publish(_digest(vclock.now(), extra=3000), vclock.now()).sent
        vclock.advance(H)
    assert names.count("beat.json") <= 2 and set(names) == {"beat.json"}
    assert all(len(b) <= 1024 for _, b, _ in server.got)


def test_outage_queues_and_coalesces_on_recovery(server, store, vclock, tmp_path):
    pub, ledger = _publisher(server, store, vclock, tmp_path)
    vclock.set(T0 + 22 * H)  # outage starts at 22:00 on day 0
    server.fail = True
    for _ in range(30):
        pub.publish(_digest(vclock.now()), vclock.now())
        vclock.advance(H)
    assert len(list((tmp_path / "outbox").glob("digest-*.json.gz"))) <= 48
    server.fail = False
    res = pub.publish(_digest(vclock.now()), vclock.now())
    # Missed days 0 and 1 (complete) get one day file each; today gets the newest digest.
    assert res.sent == ["day-20260601.json.gz", "day-20260602.json.gz", "digest.json.gz"]
    assert [p.split("?")[0] for p, _, _ in server.got] == [
        "/p/day-20260601.json.gz",
        "/p/day-20260602.json.gz",
        "/p/digest.json.gz",
    ]
    assert not list((tmp_path / "outbox").glob("digest-*.json.gz"))[1:]


def test_outbox_keeps_at_most_48(server, store, vclock, tmp_path):
    pub, _ = _publisher(server, store, vclock, tmp_path)
    server.fail = True
    for _ in range(60):
        pub.publish(_digest(vclock.now()), vclock.now())
        vclock.advance(H)
    assert len(list((tmp_path / "outbox").glob("digest-*.json.gz"))) == 48


def test_credentials_never_reach_logs(server, store, vclock, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    pub, _ = _publisher(server, store, vclock, tmp_path)
    pub.publish(_digest(vclock.now()), vclock.now())
    server.fail = True
    vclock.advance(H)
    pub.publish(_digest(vclock.now()), vclock.now())
    assert caplog.records, "the publisher logs its pushes"
    assert SECRET not in caplog.text
    assert SECRET not in repr(pub.target)


def test_file_target_and_ledger_month_rollover(store, vclock, tmp_path):
    ucfg = UplinkConfig(target="file", path=tmp_path / "site")
    ledger = Ledger(store, ucfg, vclock)
    pub = DigestPublisher(FileTarget(tmp_path / "site"), ledger, tmp_path / "outbox", vclock, ucfg)
    pub.publish(_digest(vclock.now()), vclock.now())
    assert (tmp_path / "site" / "digest.json.gz").exists()
    assert ledger.month_bytes() > 0
    vclock.set(T0 + 31 * 86400)
    assert ledger.month_bytes() == 0


def test_page_upload_only_when_its_hash_changes(store, vclock, tmp_path):
    ucfg = UplinkConfig(target="file", path=tmp_path / "site")
    ledger = Ledger(store, ucfg, vclock)
    pub = DigestPublisher(FileTarget(tmp_path / "site"), ledger, tmp_path / "outbox", vclock, ucfg)
    assert pub.publish_files({"index.html": b"<p>a</p>"}) == ["index.html"]
    assert pub.publish_files({"index.html": b"<p>a</p>"}) == []
    assert pub.publish_files({"index.html": b"<p>b</p>"}) == ["index.html"]


def test_secrets_file_parsing(tmp_path):
    p = tmp_path / "e"
    p.write_text("# comment\nSDRSCAN_UPLINK_URL='http://h/{name}'\nSDRSCAN_UPLINK_AUTH=Basic x\n")
    s = load_secrets(p)
    assert s["SDRSCAN_UPLINK_URL"] == "http://h/{name}" and s["SDRSCAN_UPLINK_AUTH"] == "Basic x"
