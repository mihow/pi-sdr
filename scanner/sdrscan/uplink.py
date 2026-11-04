"""Push the hourly digest off the rig, within a monthly byte budget.

The rig sits behind an LTE link with a small data cap. Once an hour it pushes one gzipped
digest (a few KB) to an off-site static host, and viewers read it there, so a dashboard
view costs the rig nothing. Every byte the rig sends or receives for this, plus an
estimate of the TLS handshake, goes on a monthly ``Ledger`` (spec section 10):

- below 80% of the cap: one push per ``interval_s`` (1 h);
- from 80%: one push per ``slow_interval_s`` (3 h);
- at the cap: only a health beat of at most 1 KB per day. The live server answers 429.

Digests wait in an outbox while the host is unreachable (at most ``outbox_keep``). On
recovery only the newest digest is sent as ``digest.json.gz``, plus one ``day-YYYYMMDD``
file per missed day (the last digest of that day). Targets use stdlib ``urllib`` only.
Credentials come from the env file named by ``uplink.secrets_env`` and never appear in
logs or argv; the upload URL is logged by scheme and host only.
"""

from __future__ import annotations

import dataclasses
import datetime
import gzip
import hashlib
import json
import logging
import os
import typing
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .clock import Clock
from .config import UplinkConfig
from .store import Store
from .summarize import encode_digest

log = logging.getLogger("sdrscan.uplink")
DAY = 86400
BEAT_MAX_BYTES = 1024
REQUEST_LINE_EST = 16  # method, HTTP version, separators


@dataclasses.dataclass(frozen=True)
class Response:
    status: int
    bytes_out: int
    bytes_in: int
    etag: str | None = None


@dataclasses.dataclass
class PushResult:
    sent: list[str]
    status: str  # sent | not_due | failed | capped
    bytes: int = 0


class Target(typing.Protocol):
    name: str

    def put(
        self, name: str, body: bytes, content_type: str, encoding: str | None = None
    ) -> Response: ...


def load_secrets(path: Path | None) -> dict[str, str]:
    """``KEY=VALUE`` lines (shell env-file style). Quotes are stripped; ``#`` is a comment."""
    out: dict[str, str] = {}
    if path is None or not Path(path).exists():
        return out
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


class FileTarget:
    """Writes objects into a directory (tests, or a locally synced folder)."""

    name = "file"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def put(self, name: str, body: bytes, content_type: str, encoding: str | None = None):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f".{name}.tmp"
        tmp.write_bytes(body)
        os.replace(tmp, self.root / name)
        return Response(200, len(body), 0)

    def __repr__(self) -> str:
        return "FileTarget()"


class _HttpTarget:
    method = "PUT"
    name = "http"

    def __init__(self, url_template: str, auth: str | None = None, timeout_s: float = 30.0):
        self._url = url_template
        self._auth = auth
        self.timeout_s = timeout_s
        parts = urllib.parse.urlsplit(url_template)
        self.where = f"{parts.scheme}://{parts.hostname}"  # safe to log: no path or query

    @classmethod
    def from_secrets(cls, secrets: dict[str, str], **kw) -> _HttpTarget:
        url = secrets.get("SDRSCAN_UPLINK_URL")
        if not url:
            raise ValueError("SDRSCAN_UPLINK_URL is missing from the uplink secrets file")
        return cls(url, secrets.get("SDRSCAN_UPLINK_AUTH"), **kw)

    def _request(self, name: str, body: bytes, content_type: str, encoding: str | None):
        if "{name}" in self._url:
            url = self._url.replace("{name}", urllib.parse.quote(name))
        else:
            url = self._url
        headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        if encoding:
            headers["Content-Encoding"] = encoding
        if self.method == "POST":
            headers["X-Sdrscan-Name"] = name
        if self._auth:
            headers["Authorization"] = self._auth
        return urllib.request.Request(url, data=body, headers=headers, method=self.method)

    def put(self, name: str, body: bytes, content_type: str, encoding: str | None = None):
        req = self._request(name, body, content_type, encoding)
        parts = urllib.parse.urlsplit(req.full_url)
        out = len(body) + REQUEST_LINE_EST + len(parts.path) + len(parts.query) + len(
            parts.netloc
        ) + sum(len(k) + len(v) + 4 for k, v in req.header_items()) + 40  # Host, Connection
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = resp.read()
                hdr = sum(len(k) + len(v) + 4 for k, v in resp.headers.items()) + 17
                return Response(resp.status, out, hdr + len(data), resp.headers.get("ETag"))
        except urllib.error.HTTPError as exc:
            hdr = sum(len(k) + len(v) + 4 for k, v in exc.headers.items()) + 17
            return Response(exc.code, out, hdr)
        except (urllib.error.URLError, OSError, TimeoutError):
            return Response(0, 0, 0)  # nothing reached the host; the ledger is unchanged

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.where})"


class HttpPutTarget(_HttpTarget):
    """PUT to an S3-compatible presigned URL template or WebDAV (``{name}`` is replaced)."""

    method = "PUT"
    name = "http_put"


class HttpPostTarget(_HttpTarget):
    """POST to a collector on a tailnet peer; the object name goes in ``X-Sdrscan-Name``."""

    method = "POST"
    name = "http_post"


def make_target(cfg: UplinkConfig) -> Target:
    if cfg.target == "file":
        if cfg.path is None:
            raise ValueError("uplink.path is required for the file target")
        return FileTarget(cfg.path)
    secrets = load_secrets(cfg.secrets_env)
    cls = HttpPutTarget if cfg.target == "http_put" else HttpPostTarget
    return cls.from_secrets(secrets)


class Ledger:
    """Monthly uplink bytes, persisted in ``uplink_log``. Shared by the publisher and the
    live server, so every byte that crosses the LTE link is counted once."""

    def __init__(self, store: Store, cfg: UplinkConfig, clock: Clock) -> None:
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self._month: str | None = None
        self._bytes = 0

    @staticmethod
    def _month_of(t: float) -> tuple[str, int]:
        d = datetime.datetime.fromtimestamp(t, datetime.UTC)
        start = datetime.datetime(d.year, d.month, 1, tzinfo=datetime.UTC)
        return f"{d:%Y-%m}", int(start.timestamp())

    def _sync(self) -> None:
        key, start = self._month_of(self.clock.now())
        if key != self._month:
            self.store.flush()
            row = self.store.db.execute(
                "SELECT COALESCE(SUM(bytes_out + bytes_in), 0) FROM uplink_log WHERE ts >= ?",
                (start,),
            ).fetchone()
            self._month, self._bytes = key, int(row[0])

    def charge(self, target: str, bytes_out: int, bytes_in: int, status: int) -> None:
        self._sync()
        self._bytes += int(bytes_out) + int(bytes_in)
        self.store.add_uplink(self.clock.now(), target, int(bytes_out), int(bytes_in), status)

    def month_bytes(self) -> int:
        self._sync()
        return self._bytes

    def state(self) -> str:
        used = self.month_bytes()
        if used >= self.cfg.cap_bytes:
            return "capped"
        if used >= self.cfg.slow_frac * self.cfg.cap_bytes:
            return "slow"
        return "ok"

    def to_dict(self) -> dict:
        return {"month_bytes": self.month_bytes(), "cap_bytes": self.cfg.cap_bytes,
                "state": self.state()}  # fmt: skip


class DigestPublisher:
    def __init__(
        self, target: Target, ledger: Ledger, outbox: Path, clock: Clock, cfg: UplinkConfig
    ) -> None:
        self.target = target
        self.ledger = ledger
        self.outbox = Path(outbox)
        self.clock = clock
        self.cfg = cfg
        self.outbox.mkdir(parents=True, exist_ok=True)
        self._last_ok: float | None = None
        self._last_beat: float | None = None

    def publish(self, digest: dict, now: float) -> PushResult:
        self._queue(digest, now)
        state = self.ledger.state()
        if state == "capped":
            return self._beat(digest, now)
        interval = self.cfg.slow_interval_s if state == "slow" else self.cfg.interval_s
        if self._last_ok is not None and now - self._last_ok < interval - 1:
            return PushResult([], "not_due")
        queued = sorted(self.outbox.glob("digest-*.json.gz"))
        newest = queued[-1]
        today = _day(_ts(newest))
        last_per_day: dict[int, Path] = {}
        for p in queued[:-1]:
            if _day(_ts(p)) < today:
                last_per_day[_day(_ts(p))] = p
        sends = [(f"day-{_ymd(d)}.json.gz", p) for d, p in sorted(last_per_day.items())]
        sends.append(("digest.json.gz", newest))
        sent: list[str] = []
        total = 0
        for name, path in sends:
            body = path.read_bytes()
            resp = self._send(name, body, "application/json", "gzip")
            total += resp.bytes_out + resp.bytes_in
            if not 200 <= resp.status < 300:
                log.warning("digest push to %r failed with status %s", self.target, resp.status)
                return PushResult(sent, "failed", total)
            sent.append(name)
        for p in queued:
            p.unlink(missing_ok=True)
        self._last_ok = now
        log.info("pushed %s to %r (%d bytes with overhead)", ",".join(sent), self.target, total)
        return PushResult(sent, "sent", total)

    def publish_files(self, files: dict[str, bytes]) -> list[str]:
        """Upload static files (the dashboard page) only when their content hash changes."""
        sent = []
        for name, body in sorted(files.items()):
            digest = hashlib.sha256(body).hexdigest()
            key = f"page_sha:{name}"
            if self.ledger.store.meta(key) == digest:
                continue
            if self.ledger.state() == "capped":
                break
            ctype = "text/html; charset=utf-8" if name.endswith(".html") else "application/json"
            # Over the network the page travels gzipped and the host serves it with
            # Content-Encoding: gzip; a local folder keeps the plain file.
            if self.target.name == "file":
                resp = self._send(name, body, ctype, None)
            else:
                resp = self._send(name, gzip.compress(body, 9, mtime=0), ctype, "gzip")
            if 200 <= resp.status < 300:
                self.ledger.store.set_meta(key, digest)
                self.ledger.store.db.commit()
                sent.append(name)
        return sent

    # -- internals ---------------------------------------------------------------------
    def _send(self, name: str, body: bytes, ctype: str, encoding: str | None) -> Response:
        resp = self.target.put(name, body, ctype, encoding)
        if resp.status:
            # Connection setup (TLS handshake, TCP) per request; a local file costs none.
            network = self.target.name != "file" and resp.bytes_out
            tls = self.cfg.tls_overhead_bytes if network else 0
            self.ledger.charge(self.target.name, resp.bytes_out + tls, resp.bytes_in, resp.status)
        return resp

    def _queue(self, digest: dict, now: float) -> None:
        path = self.outbox / f"digest-{int(now):012d}.json.gz"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(encode_digest(digest), 9, mtime=0))
        os.replace(tmp, path)
        queued = sorted(self.outbox.glob("digest-*.json.gz"))
        for p in queued[: max(0, len(queued) - self.cfg.outbox_keep)]:
            p.unlink(missing_ok=True)

    def _beat(self, digest: dict, now: float) -> PushResult:
        if self._last_beat is not None and now - self._last_beat < DAY - 1:
            return PushResult([], "capped")
        beat = {"v": 1, "generated_at": int(now), "beat": True, "uplink": "capped",
                "health": digest.get("health", {})}  # fmt: skip
        body = json.dumps(beat, sort_keys=True, separators=(",", ":")).encode()
        if len(body) > BEAT_MAX_BYTES:
            beat["health"] = {k: beat["health"].get(k) for k in ("mode", "dongle", "temp_c")}
            body = json.dumps(beat, sort_keys=True, separators=(",", ":")).encode()
        resp = self._send("beat.json", body, "application/json", None)
        if 200 <= resp.status < 300:
            self._last_beat = now
            return PushResult(["beat.json"], "capped", resp.bytes_out + resp.bytes_in)
        return PushResult([], "failed")


def _ts(p: Path) -> int:
    return int(p.name.split("-", 1)[1].split(".", 1)[0])


def _day(t: float) -> int:
    return int(t // DAY * DAY)


def _ymd(day_ts: int) -> str:
    return f"{datetime.datetime.fromtimestamp(day_ts, datetime.UTC):%Y%m%d}"
