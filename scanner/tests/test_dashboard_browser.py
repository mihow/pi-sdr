"""The dashboard in real browser engines at phone and tablet widths, both themes.

Skipped without Playwright. Chromium comes from Playwright's cache or, with
``SDRSCAN_CHROME``, from a local Chrome; WebKit (the iOS Safari engine) runs when Playwright
has it installed. Screenshots go to ``scanner/build/screens/`` (gitignored).
"""

from __future__ import annotations

import copy
import functools
import gzip
import http.server
import json
import os
import threading
import time
from pathlib import Path

import pytest

from sdrscan.dashboard import build_dashboard
from sdrscan.summarize import build_digest, encode_digest

from . import weekgen

pytestmark = pytest.mark.browser
GOLDEN = Path(__file__).parent / "golden" / "digest_week.json"
SCREENS = Path(__file__).resolve().parents[1] / "build" / "screens"
WIDTHS = (360, 390, 768)


def _variants(tmp_path, vclock) -> dict[str, dict]:
    now = int(time.time())
    week = json.loads(GOLDEN.read_text())
    shift = now - 600 - week["generated_at"]
    week["generated_at"] += shift
    for r in week["recent"]:
        r["t"] += shift
    for n in week["new"]:
        n["first_ts"] += shift
    for u in week["unusual"]:
        u["hour"] += shift
    for w in week["windows"].values():
        for c in w.get("calls", []):
            c["first_ts"] += shift
            c["last_ts"] += shift
    week["recent"][0]["text"] = (
        "Engine two is clear of the north gate, returning to the station now"
    )
    week["recent"][0]["conf"] = 0.8
    week["labels"]["146940000"] = "W7XYZ repeater"
    week["prose"] = "Land mobile and GMRS were busiest. One new FRS channel appeared today."
    stale = copy.deepcopy(week)
    stale["generated_at"] = now - 3 * 3600
    red = copy.deepcopy(week)
    red["generated_at"] = now - 7 * 3600
    red["health"].update(
        dongle=False,
        mode="critical",
        temp_c=83.4,
        disk_free_pct=4.2,
        flags=["disk_low", "no_dongle"],
        coverage_pct=31.0,
    )
    # Above 80% of the cap the rig pushes every 3 h; 2.5 h old is on time, not late.
    slow = copy.deepcopy(week)
    slow["generated_at"] = now - int(2.5 * 3600)
    slow["health"]["uplink"] = {"month_kb": 25_000, "cap_kb": 30_000, "state": "slow",
                                "interval_s": 10_800}  # fmt: skip
    # At the cap only beat.json moves (see BEATS); the digest is days old.
    capped = copy.deepcopy(slow)
    capped["generated_at"] = now - 3 * 86400
    from sdrscan.store import Store

    store = Store.open(tmp_path / "empty.sqlite", vclock)
    store.migrate()
    empty = build_digest(store, weekgen.fake_health(), vclock.now())
    empty["generated_at"] = now - 60
    return {
        "week": week,
        "stale": stale,
        "red": red,
        "empty": empty,
        "slow": slow,
        "capped": capped,
        "plain": copy.deepcopy(week),
    }


# Variants whose host also holds a beat.json (age in seconds before now). The page asks
# for it only when the digest's uplink state is slow or capped; "slow" has last month's.
BEATS = {"capped": 2 * 3600, "slow": 40 * 86400}
# Variants served the way a WebDAV or static host serves them: the gzip bytes with no
# Content-Encoding header, so the page's own DecompressionStream path runs.
PLAIN = {"plain"}


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves ``digest.json.gz`` with ``Content-Encoding: gzip``, as the object store does."""

    def end_headers(self):
        path = self.path.split("?")[0]
        if path.endswith(".json.gz") and path.split("/")[1] not in PLAIN:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Type", "application/json")
        super().end_headers()

    def guess_type(self, path):
        return "application/json" if str(path).endswith(".json.gz") else super().guess_type(path)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    from sdrscan.clock import VirtualClock

    from .conftest import T0

    root = tmp_path_factory.mktemp("site")
    build_dashboard("digest.json.gz", "static", root)
    for name, d in _variants(root, VirtualClock(T0)).items():
        (root / name).mkdir()
        (root / name / "index.html").write_bytes((root / "index.html").read_bytes())
        (root / name / "digest.json.gz").write_bytes(gzip.compress(encode_digest(d), 9, mtime=0))
        if name in BEATS:
            beat = {"v": 1, "beat": True, "uplink": "capped", "health": {"mode": "normal"},
                    "generated_at": int(time.time()) - BEATS[name]}  # fmt: skip
            (root / name / "beat.json").write_text(json.dumps(beat))
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Handler, directory=str(root))
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _engines():
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    out = []
    chrome = os.environ.get("SDRSCAN_CHROME")
    try:
        out.append(("chromium", pw.chromium.launch(executable_path=chrome or None)))
    except Exception:  # noqa: BLE001
        out.append(("chromium", pw.chromium.launch(executable_path="/usr/bin/google-chrome")))
    try:
        out.append(("webkit", pw.webkit.launch()))
    except Exception:  # noqa: BLE001 - WebKit not installed for Playwright
        pass
    return pw, out


@pytest.fixture(scope="module")
def browsers():
    pw, engines = _engines()
    yield engines
    for _, b in engines:
        b.close()
    pw.stop()


AUDIT = """() => {
  const se = document.scrollingElement;
  const small = [...document.querySelectorAll('button,[role=tab],summary')]
    .filter(e => e.offsetParent !== null)
    .map(e => [e.textContent.trim(), e.getBoundingClientRect()])
    .filter(([, r]) => r.width < 44 || r.height < 44).map(([t]) => t);
  const wide = [...document.querySelectorAll('main *')]
    .filter(e => e.getBoundingClientRect().right > se.clientWidth + 0.5)
    .map(e => e.className || e.tagName);
  return {sw: se.scrollWidth, cw: se.clientWidth, small, wide: wide.slice(0, 5),
          age: document.getElementById('age').textContent,
          health: document.getElementById('health').className};
}"""


@pytest.mark.parametrize("variant", ["week", "empty", "stale", "red", "slow", "capped", "plain"])
def test_dashboard_fits_phones_in_both_themes(site, browsers, variant):
    SCREENS.mkdir(parents=True, exist_ok=True)
    for engine, browser in browsers:
        for width in WIDTHS:
            for scheme in ("light", "dark"):
                ctx = browser.new_context(
                    viewport={"width": width, "height": 800},
                    color_scheme=scheme,
                    device_scale_factor=2,
                )
                page = ctx.new_page()
                errors: list[str] = []
                requests: list[str] = []
                page.on("console", lambda m, out=errors: m.type == "error" and out.append(m.text))
                page.on("pageerror", lambda e, out=errors: out.append(str(e)))
                page.on("request", lambda r, out=requests: out.append(r.url))
                page.goto(f"{site}/{variant}/index.html")
                page.wait_for_function(
                    "!document.getElementById('age').textContent.startsWith('Loading')"
                )
                a = page.evaluate(AUDIT)
                tag = f"{variant}-{engine}-{width}-{scheme}"
                page.screenshot(path=str(SCREENS / f"{tag}.png"), full_page=True)
                ctx.close()
                assert a["sw"] <= a["cw"], (tag, a)
                assert not a["wide"], (tag, a["wide"])  # overflow inside clipped boxes
                assert not a["small"], (tag, a["small"])
                assert not errors, (tag, errors)
                paths = sorted({u.split(site)[1] for u in requests})
                names = ["digest.json.gz", "index.html"]
                if variant in BEATS:
                    names.insert(0, "beat.json")
                assert paths == [f"/{variant}/{n}" for n in names], paths
                assert "Could not load" not in a["age"], (tag, a["age"])
                want = {
                    "week": "health",
                    "empty": "health",
                    "stale": "health stale",
                    "red": "health off",
                    "slow": "health",
                    "capped": "health",
                    "plain": "health",
                }[variant]
                assert a["health"] == want, (tag, a)
                if variant == "capped":
                    assert "Data cap reached" in a["age"], (tag, a["age"])


def test_theme_toggle_and_tabs(site, browsers):
    engine, browser = browsers[0]
    page = browser.new_page(viewport={"width": 360, "height": 800})
    page.goto(f"{site}/week/index.html")
    page.wait_for_selector("#busy li")
    page.click("#theme")
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    page.click("#theme")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    page.reload()
    page.wait_for_selector("#busy li")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    for tab, text in (("week", "this week"), ("all", "of all time"), ("now", "5 minutes")):
        page.click(f"#tab-{tab}")
        assert text in page.text_content("#busy-h")
    page.click("#tab-week")
    assert page.locator("#busy li").count() >= 3
    page.close()


def test_call_signs_key_ups_and_stage_activity_show(site, browsers):
    engine, browser = browsers[0]
    page = browser.new_page(viewport={"width": 360, "height": 800})
    page.goto(f"{site}/week/index.html")
    page.wait_for_selector("#calls li")
    calls = page.text_content("#calls")
    assert "K7ABC" in calls and "aircraft" in calls and "146.52" in calls
    busy = page.text_content("#busy")
    assert "key-ups" in busy and "tone 100.0 Hz" in busy and "WRAB123" in busy
    pipe = page.text_content("#pipe")
    for label in (
        "Survey hops",
        "Peaks detected",
        "Tracks opened",
        "Dwell time",
        "Key-ups",
        "Clips kept",
        "evicted",
        "Transcripts",
        "Call signs found",
        "Data pushed",
    ):
        assert label in pipe, label
    recent = page.text_content("#recent")
    assert "key-up, tone 100.0 Hz" in recent
    assert "W7XYZ repeater · " in recent, "recent rows must show a channel's label"
    page.click("#tab-now")
    assert "in the last hour" in page.text_content("#pipe-h")
    page.goto(f"{site}/empty/index.html")
    page.wait_for_selector("#calls li")
    assert "stay on the rig" in page.text_content("#calls")
    page.close()


def test_live_page_plays_a_clip_in_webkit_at_phone_width(tmp_path, vclock, browsers):
    """The rig's own page (live mode) at 360 px in WebKit: the Play clip button fits, and
    tapping it loads the clip from the live server. The clip here is WAV (opusenc is not
    on the test host), so Ogg Opus playback in iOS Safari is still unverified."""
    from sdrscan.clips import ClipStore, WavEncoder
    from sdrscan.config import QuotaConfig, UplinkConfig
    from sdrscan.live import LiveServer
    from sdrscan.store import Store
    from sdrscan.uplink import Ledger

    from .audio import voice
    from .test_clips import FakeFs

    store = Store.open(tmp_path / "scan.sqlite", vclock)
    store.migrate()
    clips = ClipStore(store, QuotaConfig(), data_root=tmp_path / "d", stage_root=tmp_path / "s",
                      clock=vclock, encoder=WavEncoder(), statvfs=FakeFs())  # fmt: skip
    w = clips.begin(462_562_500, vclock.now())
    w.write(voice(1.0))
    clip_id = clips.finish(w, {"class": "voice"})
    digest = _variants(tmp_path, vclock)["week"]
    digest["recent"][0]["clip"] = clip_id
    ledger = Ledger(store, UplinkConfig(cap_bytes=10_000_000), vclock)
    srv = LiveServer(store, clips, ledger, "127.0.0.1", 0, digest_fn=lambda: digest,
                     health_fn=lambda: {"ok": True}, charge_loopback=False)  # fmt: skip
    srv.start()
    webkit = [b for name, b in browsers if name == "webkit"]
    if not webkit:
        pytest.skip("Playwright WebKit is not installed")
    try:
        page = webkit[0].new_page(viewport={"width": 360, "height": 800}, device_scale_factor=2)
        errors: list[str] = []
        clip_status: list[int] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: "/clip/" in r.url and clip_status.append(r.status))
        page.goto(f"http://127.0.0.1:{srv.port}/")
        button = page.get_by_role("button", name="Play clip")
        button.wait_for()
        box = button.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44, box
        a = page.evaluate(AUDIT)
        SCREENS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENS / "live-webkit-360.png"), full_page=True)
        assert a["sw"] <= a["cw"] and not a["wide"], a
        button.click()
        page.wait_for_selector("#recent audio")
        page.wait_for_function("document.querySelector('#recent audio').readyState >= 1")
        # WebKit may cancel a first probe (status 0) before its ranged media request.
        assert {200, 206} & set(clip_status), clip_status
        assert not errors, errors
        page.close()
    finally:
        srv.shutdown()
        store.close()
