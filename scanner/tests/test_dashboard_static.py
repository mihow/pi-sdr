from __future__ import annotations

import gzip
import re

import pytest

from sdrscan.dashboard import build_dashboard, render_page


@pytest.mark.parametrize("mode", ["static", "live"])
def test_page_is_self_contained_and_small(tmp_path, mode):
    path = build_dashboard("digest.json.gz", mode, tmp_path)
    page = path.read_bytes()
    text = page.decode()
    assert re.match(r"index-[0-9a-f]{10}\.html$", path.name)
    assert (tmp_path / "index.html").read_bytes() == page
    # The page fetches nothing off-site. The only absolute URLs are the call-sign lookup
    # pages a viewer can open by tapping a call.
    assert "http://" not in text
    assert set(re.findall(r"https://[^\"']+", text)) == {
        "https://www.qrz.com/db/",
        "https://www.repeaterbook.com/fcc/record.php?call=",
        "https://registry.faa.gov/AircraftInquiry/Search/NNumberResult?nNumberTxt=",
    }
    # The only <link> is an empty data: icon, which stops the browser fetching /favicon.ico.
    assert re.findall(r"<link[^>]*>", text) == ['<link rel="icon" href="data:,">']
    assert not re.search(r"<script[^>]+src", text)
    assert "@import" not in text and "url(" not in text
    gz = len(gzip.compress(page, 9, mtime=0))
    assert gz <= 10_000 and len(page) <= 30_000
    assert 'name="viewport"' in text and "viewport-fit=cover" in text
    assert "prefers-color-scheme" in text and "data-theme" in text
    assert "setInterval" not in text and "http-equiv" not in text  # no auto-refresh
    assert "-webkit-text-size-adjust" in text and ":has(" not in text
    assert "position:fixed" not in text.replace(" ", "")
    print(f"dashboard {mode}: {len(page)} bytes raw, {gz} bytes gzipped")


def test_digest_url_is_escaped():
    page = render_page('x"</script>', "static").decode()
    assert 'x\\"<\\/script>' in page


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        render_page("d", "remote")
