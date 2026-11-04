"""Build the self-contained dashboard page.

One ``index.html`` with inline CSS and JS, system fonts, no external assets and no
auto-refresh. ``static`` mode reads ``digest.json.gz`` next to the page (the off-site copy,
which costs the rig nothing per view). ``live`` mode reads the rig's own ``api/digest`` and
shows play buttons once ``healthz`` answers.

The page is written twice: ``index-<hash>.html`` for hosts that can serve it with
``Cache-Control: immutable``, and ``index.html`` for a short-lived cache with an ETag.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from importlib import resources
from pathlib import Path

MODES = {"static", "live"}
MAX_RAW_BYTES = 30_000
MAX_GZ_BYTES = 10_000


def render_page(digest_url: str, mode: str) -> bytes:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    tmpl = resources.files("sdrscan.web").joinpath("index.html.tmpl").read_text()
    # json.dumps quotes and escapes the URL; the template holds it inside a JS string.
    url = json.dumps(digest_url)[1:-1].replace("</", "<\\/")
    page = tmpl.replace("__MODE__", mode).replace("__DIGEST_URL__", url).encode()
    if len(page) > MAX_RAW_BYTES or len(gzip.compress(page, 9, mtime=0)) > MAX_GZ_BYTES:
        raise ValueError("the dashboard page is over its size budget")
    return page


def build_dashboard(digest_url: str, mode: str, out_dir: Path) -> Path:
    """Write ``index.html`` and ``index-<hash>.html`` into ``out_dir``; return the hashed one."""
    page = render_page(digest_url, mode)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = f"index-{hashlib.sha256(page).hexdigest()[:10]}.html"
    (out / name).write_bytes(page)
    (out / "index.html").write_bytes(page)
    return out / name
