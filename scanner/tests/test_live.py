from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request

import pytest

from sdrscan.clips import ClipStore, WavEncoder
from sdrscan.config import QuotaConfig, UplinkConfig
from sdrscan.live import LiveServer
from sdrscan.store import Store
from sdrscan.uplink import Ledger

from .audio import voice
from .test_clips import FakeFs


@pytest.fixture
def live(tmp_path, vclock):
    store = Store.open(tmp_path / "scan.sqlite", vclock)
    store.migrate()
    clips = ClipStore(store, QuotaConfig(), data_root=tmp_path / "d", stage_root=tmp_path / "s",
                      clock=vclock, encoder=WavEncoder(), statvfs=FakeFs())  # fmt: skip
    w = clips.begin(462_562_500, vclock.now())
    w.write(voice(1.0))
    clip_id = clips.finish(w, {"class": "voice"})
    ledger = Ledger(store, UplinkConfig(cap_bytes=1_000_000), vclock)
    digest = {"v": 1, "generated_at": int(vclock.now()), "now": {"active": []}, "health": {}}
    srv = LiveServer(store, clips, ledger, "127.0.0.1", 0, digest_fn=lambda: digest,
                     charge_loopback=True)  # fmt: skip
    srv.start()
    yield srv, ledger, clip_id, f"http://127.0.0.1:{srv.port}"
    srv.shutdown()
    store.close()


def _get(url, **headers):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", **headers})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_api_now_is_gzipped_with_etag_and_304(live):
    srv, ledger, _, base = live
    status, headers, body = _get(base + "/api/now")
    assert status == 200 and headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(body))["now"] == {"active": []}
    etag = headers["ETag"]
    status, headers, body = _get(base + "/api/now", **{"If-None-Match": etag})
    assert status == 304 and body == b""


def test_every_response_is_charged(live):
    srv, ledger, _, base = live
    before = ledger.month_bytes()
    _, h1, b1 = _get(base + "/")
    mid = ledger.month_bytes()
    assert mid - before >= len(b1) > 0
    print(f"live page first open: {len(b1)} bytes body, {mid - before} bytes charged")
    _, _, b2 = _get(base + "/api/digest")
    assert ledger.month_bytes() - mid >= len(b2)


def test_at_the_cap_it_answers_429(live):
    srv, ledger, _, base = live
    ledger.charge("test", 2_000_000, 0, 200)
    assert _get(base + "/api/now")[0] == 429
    assert _get(base + "/healthz")[0] == 200


def test_clip_supports_range(live):
    srv, ledger, clip_id, base = live
    status, headers, body = _get(base + f"/clip/{clip_id}.opus", Range="bytes=0-99")
    assert status == 206 and len(body) == 100
    assert headers["Content-Range"].startswith("bytes 0-99/")
    full = int(headers["Content-Range"].split("/")[1])
    status, _, body = _get(base + f"/clip/{clip_id}.opus", Range="bytes=-10")
    assert status == 206 and len(body) == 10
    assert _get(base + f"/clip/{clip_id}.opus", Range=f"bytes={full}-")[0] == 416
    assert _get(base + "/clip/999.opus")[0] == 404


def test_channel_detail(live):
    srv, ledger, _, base = live
    status, _, body = _get(base + "/api/channel/462562500?w=week")
    d = json.loads(gzip.decompress(body))
    assert status == 200 and len(d["active_s"]) == 168 and d["w"] == "week"
