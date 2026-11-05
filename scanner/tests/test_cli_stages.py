"""The stage commands chained by hand, as the README shows, on a mocked radio."""

from __future__ import annotations

import gzip
import json

import numpy as np
import pytest

from sdrscan import IQ_RATE
from sdrscan.cli import _hz, main
from sdrscan.clock import VirtualClock
from sdrscan.iq.file import open_iq, write_iq
from sdrscan.iq.scene import Scene
from sdrscan.iq.synthetic import SyntheticSource

CENTER = 462_406_250
GMRS = 462_562_500
SCENE = f"""
noise_floor: {{default_db: -45}}
dc_db: -30
emitters:
  - {{kind: nfm_voice, hz: {GMRS}, level_db: -20, bursts: [[0.2, 2.2]]}}
"""


class FakeRtl:
    """Stands in for ``RtlSource``: a synthetic scene, with the calls it received."""

    instances: list[FakeRtl] = []

    def __init__(self, clock, device_index=0, rate=IQ_RATE, threaded=True, scene=SCENE):
        self.rate, self.threaded, self.calls = rate, threaded, []
        vclock = VirtualClock(0.0)
        self._src = SyntheticSource(Scene.from_yaml(scene, clock=vclock), vclock)
        FakeRtl.instances.append(self)

    def open(self):
        self.calls.append("open")
        self._src.open()

    def tune(self, center_hz, gain_db):
        self.calls.append(("tune", center_hz, gain_db))
        self._src.tune(center_hz, gain_db)

    def read(self, n):
        return self._src.read(n)

    def close(self):
        self.calls.append("close")

    def health(self):
        return {"dropped": 0, "reopen_count": 0}


@pytest.fixture
def fake_rtl(monkeypatch):
    import sdrscan.iq.rtl

    FakeRtl.instances = []
    monkeypatch.setattr(sdrscan.iq.rtl, "RtlSource", FakeRtl)
    return FakeRtl


def _last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.mark.parametrize("fmt", ["cu8", "cf32"])
def test_capture_writes_the_file_and_sidecar(tmp_path, capsys, fake_rtl, fmt):
    out = tmp_path / f"cap.{fmt}"
    argv = ["capture", "--freq", "462.40625M", "--seconds", "0.5", "--gain", "29.7"]
    assert main([*argv, "--format", fmt, "--out", str(out)]) == 0
    rtl = fake_rtl.instances[0]
    assert rtl.calls == ["open", ("tune", CENTER, 29.7), "close"] and not rtl.threaded
    f = open_iq(out)
    assert (f.fmt, f.rate, f.center_hz, f.gain_db, f.n_samples) == (
        fmt,
        IQ_RATE,
        CENTER,
        29.7,
        IQ_RATE // 2,
    )
    side = json.loads(out.with_suffix(".json").read_text())
    assert side["schema"] == "sdrscan.iq/1" and side["device"] == "RTL-SDR"
    assert side["recorded_at"] and _last_json(capsys)["n_samples"] == IQ_RATE // 2


def test_stage_commands_chain_from_capture_to_dashboard(tmp_path, capsys, fake_rtl):
    iq = tmp_path / "cap.cu8"
    main(["capture", "--freq", str(CENTER), "--seconds", "3", "--gain", "30", "--out", str(iq)])
    capsys.readouterr()

    assert main(["detect", "--iq", str(iq), "--out", str(tmp_path / "det.jsonl")]) == 0
    summary = _last_json(capsys)
    assert summary["hops"] == 30 and summary["channels"][0]["hz"] == GMRS
    lines = (tmp_path / "det.jsonl").read_text().splitlines()
    assert len(lines) == 30 and json.loads(lines[0])["schema"] == "sdrscan.detections/1"

    wav = tmp_path / "ch.wav"
    assert main(["demod", "--iq", str(iq), "--freq", "462.5625M", "--out", str(wav)]) == 0
    assert _last_json(capsys) == {"out": str(wav), "hz": GMRS, "mode": "nfm", "seconds": 3.0}

    assert main(["gate", "--wav", str(wav), "--out", str(tmp_path / "seg.jsonl")]) == 0
    assert _last_json(capsys)["class"] == "voice"

    tr = tmp_path / "tr.json"
    assert main(["stt", "--wav", str(wav), "--engine", "fake", "--out", str(tr)]) == 0
    assert json.loads(tr.read_text())["text"] == "unit two on scene"

    dig = tmp_path / "www"
    assert main(["digest", "--events", str(tmp_path / "det.jsonl"), "--out", str(dig)]) == 0
    d = json.loads(gzip.decompress((dig / "digest.json.gz").read_bytes()))
    assert [c["hz"] for c in d["windows"]["hour"]["top"]] == [GMRS]

    assert main(["dashboard", "--digest", str(dig / "digest.json.gz"), "--out", str(dig)]) == 0
    assert (dig / "index.html").exists()


def test_stage_commands_on_noise_report_nothing(tmp_path, capsys):
    clock = VirtualClock(0.0)
    src = SyntheticSource(Scene.from_yaml("noise_floor: {default_db: -45}\n", clock=clock), clock)
    src.open()
    src.tune(CENTER, 30.0)
    iq = tmp_path / "noise.cu8"
    write_iq(iq, src.read(IQ_RATE), rate=IQ_RATE, center_hz=CENTER, gain_db=30.0)
    main(["detect", "--iq", str(iq), "--out", str(tmp_path / "d.jsonl")])
    assert _last_json(capsys)["channels"] == []
    main(["demod", "--iq", str(iq), "--freq", str(GMRS), "--out", str(tmp_path / "n.wav")])
    main(["gate", "--wav", str(tmp_path / "n.wav"), "--out", str(tmp_path / "s.jsonl")])
    capsys.readouterr()
    main(["stt", "--wav", str(tmp_path / "n.wav"), "--out", str(tmp_path / "t.json")])
    assert json.loads((tmp_path / "t.json").read_text())["text"] is None  # engine none
    main(["digest", "--events", str(tmp_path / "d.jsonl"), "--out", str(tmp_path / "w")])
    d = json.loads((tmp_path / "w" / "digest.json").read_text())
    assert d["windows"]["hour"]["top"] == []


def test_detect_reads_an_offset_slice(tmp_path, capsys, fake_rtl):
    iq = tmp_path / "cap.cu8"
    main(["capture", "--freq", str(CENTER), "--seconds", "1", "--gain", "30", "--out", str(iq)])
    out = tmp_path / "d.jsonl"
    main(["detect", "--iq", str(iq), "--offset", "0.5", "--seconds", "0.2", "--out", str(out)])
    assert [json.loads(x)["t_s"] for x in out.read_text().splitlines()] == [0.5, 0.6]


def test_corpus_cut_copies_cu8_codes_exactly_and_reports_the_source_hash(tmp_path, capsys):
    rng = np.random.default_rng(3)
    codes = rng.integers(0, 256, size=2 * IQ_RATE, dtype=np.uint8)
    (tmp_path / "big.raw").write_bytes(codes.tobytes())
    (tmp_path / "big.json").write_text(
        json.dumps({"center_freq_hz": 146_500_000, "sample_rate": IQ_RATE, "format": "cf32"})
    )
    out = tmp_path / "clip.cu8"
    argv = ["corpus", "cut", "--src", str(tmp_path / "big.raw"), "--offset", "0.25"]
    assert main([*argv, "--seconds", "0.5", "--out", str(out)]) == 0
    res = _last_json(capsys)
    lo, hi = 2 * IQ_RATE // 4, 2 * (IQ_RATE // 4 + IQ_RATE // 2)
    assert out.read_bytes() == codes[lo:hi].tobytes()
    import hashlib

    assert res["sha256"] == hashlib.sha256(codes[lo:hi].tobytes()).hexdigest()
    assert (res["file"], res["offset_s"], res["seconds"], res["scale"]) == (
        "big.raw",
        0.25,
        0.5,
        1.0,
    )
    assert open_iq(out).center_hz == 146_500_000


def test_corpus_cut_scales_a_quiet_float_capture_into_eight_bits(tmp_path, capsys):
    rng = np.random.default_rng(4)
    iq = (0.01 * (rng.normal(size=IQ_RATE) + 1j * rng.normal(size=IQ_RATE))).astype(np.complex64)
    write_iq(tmp_path / "q.cf32", iq, rate=IQ_RATE, center_hz=1, gain_db=None)
    main(["corpus", "cut", "--src", str(tmp_path / "q.cf32"), "--out", str(tmp_path / "q.cu8")])
    res = _last_json(capsys)
    assert res["scale"] > 10
    codes = np.frombuffer((tmp_path / "q.cu8").read_bytes(), dtype=np.uint8)
    assert len(np.unique(codes)) > 100


@pytest.mark.parametrize(
    ("text", "hz"),
    [
        ("443150000", 443_150_000),
        ("443.15M", 443_150_000),
        ("162.475MHz", 162_475_000),
        ("162.475e6", 162_475_000),
        ("27185k", 27_185_000),
    ],
)
def test_frequency_arguments(text, hz):
    assert _hz(text) == hz
