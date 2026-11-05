"""End to end on a compressed hour: synthetic IQ -> detection -> voice gate -> clips ->
speech-to-text (stub) -> rollups -> digest -> uplink, all on a virtual clock.

The run covers the last hour of a UTC day, so the hour boundary also closes the day.
Survey hops use the fast spectrum path and dwells render real IQ ("hybrid"), so the
channelizer, demodulators and voice gate see audio.
"""

from __future__ import annotations

import gzip
import json

from sdrscan import rollup
from sdrscan.clips import WavEncoder
from sdrscan.config import Config, DutyConfig, QuotaConfig, UplinkConfig
from sdrscan.iq.scene import Scene
from sdrscan.iq.synthetic import SyntheticSource
from sdrscan.service import Service
from sdrscan.summarize import MAX_GZ_BYTES
from sdrscan.uplink import DigestPublisher, FileTarget, Ledger
from sdrscan.voice.stt import FakeStt

from .conftest import T0
from .test_clips import FakeFs
from .test_uplink import _Server

H = 3600
GMRS, AIR, TONE = 462_562_500, 122_800_000, 467_562_500
START = T0 + 23 * H
# quantize: false because this scene's noise floor is below one 8-bit step, so rounding
# would turn the strong AM carrier and the DC spike into an intermodulation image.
SCENE = """
seed: 3
quantize: false
noise_floor: {default_db: -50}
emitters:
  - {kind: nfm_voice, hz: 462_562_500, level_db: -38, bursts: [[300, 30], [1500, 30], [2700, 30]]}
  - {kind: am_voice, hz: 122_800_000, level_db: -40, bursts: [[900, 25]]}
  - {kind: nfm_voice, hz: 467_562_500, level_db: -38, tone_hz: 1000, bursts: [[2100, 10]]}
"""
HOURLY_BUDGET_BYTES = 10_000  # one digest push with headers and the TLS estimate


def test_compressed_hour_end_to_end(tmp_path, vclock, sys_root):
    """The uplink goes to a local HTTP PUT sink, so the ledger counts real request headers
    plus the configured TLS estimate, as it would against an object store."""
    vclock.set(START)
    server = _Server()
    usb = sys_root / "bus/usb/devices/1-1"
    usb.mkdir(parents=True)
    (usb / "idVendor").write_text("0bda\n")
    (usb / "idProduct").write_text("2838\n")
    cfg = Config(
        data_root=tmp_path / "data",
        stage_root=tmp_path / "stage",
        duty=DutyConfig(dwell_block_s=0.1),
        quota=QuotaConfig(clip_quota_bytes=1_000_000, per_channel_share=1.0),
        uplink=UplinkConfig(
            target="http_put",
            secrets_env=tmp_path / "uplink.env",
            include_transcripts=True,
            tls_overhead_bytes=6_000,
        ),
    )
    (tmp_path / "uplink.env").write_text(
        f"SDRSCAN_UPLINK_URL=http://127.0.0.1:{server.port}/site/{{name}}\n"
    )
    scene = Scene.from_yaml(SCENE, clock=vclock)
    source = SyntheticSource(scene, vclock)
    svc = Service(
        cfg,
        vclock,
        source,
        spectrum="hybrid",
        encoder=WavEncoder(),
        statvfs=FakeFs(0.5),
        sys_root=sys_root,
        inline_stt=True,
        stt=FakeStt(["WRAB123 unit two on scene", "copy, en route"]),
    )
    page_bytes = svc.ledger.month_bytes()  # the one-time dashboard page upload
    svc.run(until=START + H + 60)
    store = svc.store

    # Events on the right channels, with the right classes.
    txs = store.transmissions()
    by_hz: dict[int, list[dict]] = {}
    for t in txs:
        by_hz.setdefault(t["hz"], []).append(t)
    assert set(by_hz) == {GMRS, AIR, TONE}, sorted(by_hz)
    assert len(by_hz[GMRS]) == 3 and len(by_hz[AIR]) == 1 and len(by_hz[TONE]) == 1
    summary = [(t["hz"], t["class"], t["voice_ratio"], t["source"], t["dur_ms"]) for t in txs]
    assert all(t["class"] == "voice" for t in by_hz[GMRS] + by_hz[AIR]), summary
    assert by_hz[TONE][0]["class"] == "data" and by_hz[TONE][0]["clip_id"] is None

    # Speech-like bursts were clipped, stored and transcribed by the stub engine.
    clips = [dict(r) for r in store.db.execute("SELECT * FROM clips ORDER BY t_start")]
    assert len(clips) == 4
    transcripts = store.db.execute("SELECT * FROM transcripts").fetchall()
    assert len(transcripts) == 4 and {r["text"] for r in transcripts} == {
        "WRAB123 unit two on scene",
        "copy, en route",
    }
    assert all(t["transcript_id"] is not None for t in by_hz[GMRS] + by_hz[AIR])
    # The call sign in a transcript is stored with its channel and transmission.
    calls = [dict(r) for r in store.db.execute("SELECT * FROM callsigns")]
    assert calls and {c["call"] for c in calls} == {"WRAB123"}
    tx_by_id = {t["id"]: t for t in store.transmissions()}
    assert all(tx_by_id[c["tx_id"]]["hz"] == c["hz"] for c in calls)

    # The clip store stayed under its quota by evicting the oldest clips first.
    assert svc.clips.used_bytes() <= cfg.quota.clip_quota_bytes
    evicted = [c for c in clips if c["evicted_at"] is not None]
    kept = [c for c in clips if c["evicted_at"] is None]
    assert evicted and kept
    assert max(c["t_start"] for c in evicted) < min(c["t_start"] for c in kept)
    assert all(c["path"] is None for c in evicted)
    assert all((cfg.data_root / c["path"]).exists() for c in kept)

    # Hour and day rollups exist.
    hour_rows = store.db.execute(
        "SELECT hz, tx_count FROM rollup_hour WHERE hour_ts = ?", (int(START),)
    ).fetchall()
    assert {r["hz"]: r["tx_count"] for r in hour_rows} == {GMRS: 3, AIR: 1, TONE: 1}
    day_rows = store.db.execute(
        "SELECT hz, tx_count, voice_tx FROM rollup_day WHERE day_ts = ?", (int(T0),)
    ).fetchall()
    assert {r["hz"]: (r["tx_count"], r["voice_tx"]) for r in day_rows} == {
        GMRS: (3, 3),
        AIR: (1, 1),
        TONE: (1, 0),
    }
    day = rollup.query_window(store, "day", vclock.now())
    assert day.channels[GMRS].tx_count == 3

    # The pushed digest is under its byte budget and says what happened.
    assert svc.digests == 1
    uploads = {path.split("/")[-1]: body for path, body, _ in server.got}
    assert sorted(uploads) == ["digest.json.gz", "index.html"]
    gz = uploads["digest.json.gz"]
    assert gz == (cfg.data_root / "www" / "digest.json.gz").read_bytes()
    assert len(gz) <= MAX_GZ_BYTES
    d = json.loads(gzip.decompress(gz))
    assert d["windows"]["day"]["top"][0]["hz"] == GMRS
    assert {t["hz"] for t in d["transcripts"]} <= {GMRS, AIR}
    assert "WRAB123" in {c["call"] for c in d["windows"]["day"]["calls"]}
    stage = d["windows"]["day"]["pipeline"]
    assert stage["survey_hops"] > 0 and stage["opened"] >= 5 and stage["dwell_s"] > 0
    assert stage["transcripts"] == 4 and stage["clips"] == 4 and stage["evicted"] >= 1
    assert d["health"]["dongle"] is True and d["health"]["uplink"]["state"] == "ok"
    pushed = svc.ledger.month_bytes() - page_bytes
    assert 0 < pushed <= HOURLY_BUDGET_BYTES, pushed
    print(
        f"\ne2e: digest {len(gz)} B gzipped, push charged {pushed} B "
        f"(projected {pushed * 24 / 1000:.0f} KB/day), page upload {page_bytes} B"
    )
    svc.shutdown()
    server.close()


def test_publisher_injected_and_reused(tmp_path, vclock, sys_root):
    """A service can take a ready publisher (the CLI uses this for simulate)."""
    cfg = Config(data_root=tmp_path / "d", stage_root=tmp_path / "s")
    scene = Scene.from_yaml("emitters: []\n", clock=vclock)
    svc = Service(
        cfg,
        vclock,
        SyntheticSource(scene, vclock),
        spectrum="psd",
        statvfs=FakeFs(0.5),
        sys_root=sys_root,
        publisher=None,
    )
    ledger = Ledger(svc.store, cfg.uplink, vclock)
    svc.publisher = DigestPublisher(
        FileTarget(tmp_path / "out"), ledger, tmp_path / "ob", vclock, cfg.uplink
    )
    svc.publish_digest()
    assert (tmp_path / "out" / "digest.json.gz").exists()
    svc.shutdown()
