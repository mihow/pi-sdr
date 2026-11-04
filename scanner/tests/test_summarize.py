from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest

from sdrscan.bandplan import BandPlan
from sdrscan.config import DEFAULT_BANDPLAN, ProseConfig
from sdrscan.llm import LlamaCliProse, NullProse, make_prose
from sdrscan.store import Store
from sdrscan.summarize import MAX_GZ_BYTES, build_digest, encode_digest

from . import weekgen
from .conftest import T0
from .weekgen import AIR, BUSY, CARRIER, END, HAM, NEW, QUIET, SPUR

GOLDEN = Path(__file__).parent / "golden" / "digest_week.json"


@pytest.fixture(scope="module")
def week(tmp_path_factory):
    from sdrscan.clock import VirtualClock

    clock = VirtualClock(T0)
    plan = BandPlan.load(DEFAULT_BANDPLAN)
    store = Store.open(tmp_path_factory.mktemp("week") / "scan.sqlite", clock)
    store.migrate()
    weekgen.build_week(store, plan, clock)
    return store, plan


def _digest(week, **kw):
    store, plan = week
    return build_digest(store, weekgen.fake_health(), END, plan=plan, site_id="site", **kw)


def test_busiest_excludes_always_on_and_spurs(week):
    d = _digest(week)
    for w in ("hour", "day", "week", "all"):
        top = [c["hz"] for c in d["windows"][w]["top"]]
        assert CARRIER not in top and SPUR not in top, w
    assert [c["hz"] for c in d["windows"]["week"]["top"][:2]] == [QUIET, BUSY]
    assert {a["hz"] for a in d["always_on"]["list"]} == {CARRIER, SPUR}


def test_new_unusual_quiet(week):
    d = _digest(week)
    assert [n["hz"] for n in d["new"]] == [NEW]
    assert d["unusual"][0]["hz"] == HAM
    assert d["unusual"][0]["hour"] == int(END - 24 * 3600 + 3 * 3600)
    assert all(u["hz"] != AIR for u in d["unusual"])
    assert [q["hz"] for q in d["quiet"]] == [QUIET]
    assert d["quiet"][0]["silent_h"] == 4


def test_digest_matches_golden_and_is_deterministic(week):
    a = encode_digest(_digest(week))
    b = encode_digest(_digest(week))
    assert a == b
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_bytes(a + b"\n")
    assert GOLDEN.read_bytes() == a + b"\n", "rerun with UPDATE_GOLDEN=1 after a deliberate change"


def test_digest_gzip_budget(week):
    raw = encode_digest(_digest(week))
    assert len(gzip.compress(raw, 9, mtime=0)) <= MAX_GZ_BYTES


def test_worst_case_500_active_channels_stays_under_4kb(tmp_path, vclock):
    plan = BandPlan.load(DEFAULT_BANDPLAN)
    store = Store.open(tmp_path / "scan.sqlite", vclock)
    store.migrate()
    from sdrscan import rollup

    for i in range(500):
        hz = 450_000_000 + i * 12_500
        for k in range(6):
            tx = weekgen._tx(plan, hz, END - 3500 + k * 500 + i % 40, 30 + i % 17)
            store.add_tx(tx)
            rollup.on_tx_closed(store, tx)
        cid = store.add_clip(hz=hz, t_start=END - 600, dur_ms=4000, nbytes=1, codec="opus",
                             path="p", tier=1, sha256="")  # fmt: skip
        store.add_transcript(cid, None, "fake", "x" * 400, 0.95, 0.1, END - 500)
    store.flush()
    d = build_digest(store, weekgen.fake_health(), END, plan=plan, include_transcripts=True)
    gz = gzip.compress(encode_digest(d), 9, mtime=0)
    assert len(gz) <= 4096, len(gz)
    assert all(len(t["text"]) <= 160 for t in d["transcripts"])


def test_transcripts_need_confidence_and_opt_in(week):
    assert "transcripts" not in _digest(week) or not _digest(week)["transcripts"]
    d = _digest(week, include_transcripts=True)
    assert d["transcripts"]
    assert all(t["conf"] >= 0.6 for t in d["transcripts"])
    per = {}
    for t in d["transcripts"]:
        per[t["hz"]] = per.get(t["hz"], 0) + 1
    assert max(per.values()) <= 3


def test_health_and_meta_fields(week):
    d = _digest(week, uplink={"month_bytes": 1234, "cap_bytes": 30_000_000, "state": "ok"})
    assert d["v"] == 1 and d["generated_at"] == int(END)
    h = d["health"]
    assert h["dongle"] is True and h["mode"] == "normal" and "coverage_pct" in h
    assert h["uplink"]["state"] == "ok"
    assert len(d["how"]) == 168 and set(d["how"]) <= set("0123456789")
    assert all(len(c["spark"]) == 24 for c in d["windows"]["day"]["top"])


def test_prose_engines(tmp_path):
    assert NullProse().rewrite({}) is None
    calls = []

    def run(argv, **kw):
        import subprocess

        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"The site was quiet. Two channels.", b"")

    p = LlamaCliProse(Path("/opt/llama-cli"), Path("/opt/q.gguf"), run=run)
    assert p.rewrite({"v": 1}, mode="hot") is None and not calls
    assert p.rewrite({"v": 1}, mode="normal") == "The site was quiet. Two channels."
    assert calls[0][:3] == ["nice", "-n", "19"]
    argv = calls[0][3:]
    for flag, val in (("-m", "/opt/q.gguf"), ("-t", "2"), ("-n", "300"), ("--temp", "0.2")):
        assert argv[argv.index(flag) + 1] == val
    assert "-no-cnv" in argv
    assert isinstance(make_prose(ProseConfig()), NullProse)
