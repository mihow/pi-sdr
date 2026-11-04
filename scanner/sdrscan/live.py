"""The rig's own small web server, for viewers on the tailnet who want detail or a clip.

The off-site digest is the normal way to look at the rig. This server is the optional
detail path, and every byte it sends over the tailnet crosses the LTE link, so each
response is gzipped, carries an ETag (a matching ``If-None-Match`` costs a 304 and no
body) and is charged to the uplink ``Ledger``. At the monthly cap it answers 429.
Loopback requests (the install script's ``/healthz`` poll) are not charged.

Routes: ``/`` (the page in live mode), ``/api/digest``, ``/api/now``,
``/api/channel/<hz>?w=day|week``, ``/clip/<id>.opus`` (with ``Range``), ``/healthz``.
It binds only to the configured address; two requests are served at a time.
"""

from __future__ import annotations

import gzip
import hashlib
import http.server
import ipaddress
import json
import logging
import re
import threading
import typing
import urllib.parse

from .clips import ClipStore
from .dashboard import render_page
from .rollup import DAY, H, hour_of
from .store import Store
from .uplink import Ledger

log = logging.getLogger("sdrscan.live")
DIGEST_TTL_S = 60.0
_CLIP = re.compile(r"^/clip/(\d+)\.(opus|wav)$")
_CHAN = re.compile(r"^/api/channel/(\d+)$")


def _json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class LiveServer:
    def __init__(
        self,
        store: Store,
        clips: ClipStore | None,
        ledger: Ledger,
        bind: str,
        port: int,
        *,
        digest_fn: typing.Callable[[], dict],
        health_fn: typing.Callable[[], dict] | None = None,
        charge_loopback: bool = False,
        workers: int = 2,
    ) -> None:
        self.store = store
        self.clips = clips
        self.ledger = ledger
        self.digest_fn = digest_fn
        self.health_fn = health_fn or (lambda: {"ok": True})
        self.charge_loopback = charge_loopback
        self.page = render_page("api/digest", "live")
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(workers)
        self._digest: tuple[float, bytes] | None = None
        self.httpd = http.server.ThreadingHTTPServer((bind, port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, name="sdrscan-live", daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- content -----------------------------------------------------------------------
    def digest_bytes(self) -> bytes:
        now = self.store.clock.monotonic()
        with self._lock:
            if self._digest is None or now - self._digest[0] >= DIGEST_TTL_S:
                self._digest = (now, _json(self.digest_fn()))
            return self._digest[1]

    def channel(self, hz: int, window: str) -> dict:
        now = self.store.clock.now()
        span = 7 * DAY if window == "week" else DAY
        with self.store.lock:
            self.store.flush()
            first = hour_of(now) - span + H
            rows = self.store.db.execute(
                "SELECT hour_ts, active_s, tx_count FROM rollup_hour WHERE hz = ?"
                " AND hour_ts >= ? ORDER BY hour_ts",
                (hz, first),
            ).fetchall()
            txs = self.store.db.execute(
                "SELECT t_start, dur_ms, class, peak_snr_db, clip_id FROM transmissions"
                " WHERE hz = ? ORDER BY t_start DESC LIMIT 10",
                (hz,),
            ).fetchall()
        cells = [0] * (span // H)
        for r in rows:
            cells[(r["hour_ts"] - first) // H] = int(round(r["active_s"]))
        return {
            "hz": hz,
            "w": window,
            "first_hour": first,
            "active_s": cells,
            "recent": [
                {
                    "t": r["t_start"],
                    "dur_s": round(r["dur_ms"] / 1000, 1),
                    "cls": r["class"],
                    "snr": r["peak_snr_db"],
                    "clip": r["clip_id"],
                }
                for r in txs
            ],
        }

    def now_section(self) -> dict:
        d = json.loads(self.digest_bytes())
        return {
            "generated_at": d.get("generated_at"),
            "now": d.get("now"),
            "health": d.get("health"),
        }

    # -- HTTP --------------------------------------------------------------------------
    def _handler(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "sdrscan"
            sys_version = ""

            def log_message(self, fmt, *args):
                log.debug("live %s", fmt % args)

            def do_GET(self):  # noqa: N802
                with server._slots:
                    server._get(self)

            def do_HEAD(self):  # noqa: N802
                self.do_GET()

        return Handler

    def _charged(self, h) -> bool:
        if self.charge_loopback:
            return True
        try:
            return not ipaddress.ip_address(h.client_address[0]).is_loopback
        except ValueError:
            return True

    def _get(self, h) -> None:
        url = urllib.parse.urlsplit(h.path)
        path = url.path
        charged = self._charged(h)
        if charged and self.ledger.state() == "capped" and path != "/healthz":
            return self._send(
                h,
                429,
                b'{"error":"monthly data cap reached"}',
                "application/json",
                charged=charged,
                cache=False,
            )
        try:
            if path in ("/", "/index.html"):
                return self._send(h, 200, self.page, "text/html; charset=utf-8", charged=charged)
            if path in ("/api/digest", "/digest.json"):
                return self._send(h, 200, self.digest_bytes(), "application/json", charged=charged)
            if path == "/api/now":
                return self._send(
                    h, 200, _json(self.now_section()), "application/json", charged=charged
                )
            if path == "/healthz":
                return self._send(
                    h,
                    200,
                    _json(self.health_fn()),
                    "application/json",
                    charged=charged,
                    cache=False,
                )
            m = _CHAN.match(path)
            if m:
                w = urllib.parse.parse_qs(url.query).get("w", ["day"])[0]
                body = _json(self.channel(int(m.group(1)), "week" if w == "week" else "day"))
                return self._send(h, 200, body, "application/json", charged=charged)
            m = _CLIP.match(path)
            if m and self.clips is not None:
                return self._clip(h, int(m.group(1)), charged)
        except Exception:  # noqa: BLE001 - one bad request must not stop the server
            log.exception("live request failed")
            return self._send(
                h, 500, b'{"error":"internal"}', "application/json", charged=charged, cache=False
            )
        return self._send(
            h, 404, b'{"error":"not found"}', "application/json", charged=charged, cache=False
        )

    def _send(self, h, status, body: bytes, ctype: str, *, charged: bool, cache: bool = True):
        etag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
        if cache and status == 200 and h.headers.get("If-None-Match") == etag:
            status, payload, extra = 304, b"", {}
        else:
            gz = "gzip" in (h.headers.get("Accept-Encoding") or "")
            payload = gzip.compress(body, 6, mtime=0) if gz else body
            extra = {"Content-Type": ctype}
            if gz:
                extra["Content-Encoding"] = "gzip"
        headers = {
            "ETag": etag,
            "Cache-Control": "no-cache" if cache else "no-store",
            "Vary": "Accept-Encoding",
            "Content-Length": str(len(payload)),
            **extra,
        }
        self._write(h, status, headers, payload, charged)

    def _clip(self, h, clip_id: int, charged: bool) -> None:
        path = self.clips.path_for(clip_id)
        if path is None or not path.exists():
            return self._send(
                h,
                404,
                b'{"error":"clip evicted or unknown"}',
                "application/json",
                charged=charged,
                cache=False,
            )
        data = path.read_bytes()
        etag = '"' + hashlib.sha256(data).hexdigest()[:20] + '"'
        ctype = "audio/ogg" if path.suffix == ".opus" else "audio/wav"
        headers = {
            "ETag": etag,
            "Accept-Ranges": "bytes",
            "Content-Type": ctype,
            "Cache-Control": "private, max-age=86400",
        }
        if h.headers.get("If-None-Match") == etag:
            headers["Content-Length"] = "0"
            return self._write(h, 304, headers, b"", charged)
        status, body = 200, data
        m = re.match(r"bytes=(\d*)-(\d*)$", h.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            n = len(data)
            if m.group(1):
                lo = int(m.group(1))
                hi = min(int(m.group(2)), n - 1) if m.group(2) else n - 1
            else:
                lo, hi = max(0, n - int(m.group(2))), n - 1
            if lo > hi or lo >= n:
                headers["Content-Range"] = f"bytes */{n}"
                headers["Content-Length"] = "0"
                return self._write(h, 416, headers, b"", charged)
            status, body = 206, data[lo : hi + 1]
            headers["Content-Range"] = f"bytes {lo}-{hi}/{n}"
        headers["Content-Length"] = str(len(body))
        self._write(h, status, headers, body, charged)

    def _write(self, h, status: int, headers: dict, body: bytes, charged: bool) -> None:
        h.send_response(status)
        for k, v in headers.items():
            h.send_header(k, v)
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)
        if charged:
            out = len(body) + 17 + sum(len(k) + len(str(v)) + 4 for k, v in headers.items()) + 60
            req = len(h.requestline) + sum(len(k) + len(v) + 4 for k, v in h.headers.items())
            self.ledger.charge("live", out, req, status)
