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
    week["recent"][0]["text"] = (
        "Engine two is clear of the north gate, returning to the station now"
    )
    week["recent"][0]["conf"] = 0.8
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
    from sdrscan.store import Store

    store = Store.open(tmp_path / "empty.sqlite", vclock)
    store.migrate()
    empty = build_digest(store, weekgen.fake_health(), vclock.now())
    empty["generated_at"] = now - 60
    return {"week": week, "stale": stale, "red": red, "empty": empty}


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves ``digest.json.gz`` with ``Content-Encoding: gzip``, as the object store does."""

    def end_headers(self):
        if self.path.split("?")[0].endswith(".json.gz"):
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


@pytest.mark.parametrize("variant", ["week", "empty", "stale", "red"])
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
                assert not a["small"], (tag, a["small"])
                assert not errors, (tag, errors)
                paths = sorted({u.split(site)[1] for u in requests})
                assert paths == [f"/{variant}/digest.json.gz", f"/{variant}/index.html"], paths
                assert "Could not load" not in a["age"], (tag, a["age"])
                want = {
                    "week": "health",
                    "empty": "health",
                    "stale": "health stale",
                    "red": "health off",
                }[variant]
                assert a["health"] == want, (tag, a)


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
