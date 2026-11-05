"""The IQ file reader: rtl_sdr cu8, cf32, ci8 and SigMF, with either sidecar style."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from sdrscan.iq.file import FileSource, IqFileError, open_iq, write_iq

RATE = 2_400_000


def _tone(n: int, f: float = 100_000.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(n) / RATE
    return (amp * np.exp(2j * np.pi * f * t)).astype(np.complex64)


def test_cu8_round_trip_is_within_one_code(tmp_path: Path):
    iq = _tone(10_000)
    path = tmp_path / "cap.cu8"
    write_iq(path, iq, rate=RATE, center_hz=146_520_000, gain_db=29.7)
    f = open_iq(path)
    assert (f.fmt, f.rate, f.center_hz, f.gain_db, f.n_samples) == (
        "cu8",
        RATE,
        146_520_000,
        29.7,
        10_000,
    )
    assert path.stat().st_size == 2 * 10_000
    np.testing.assert_allclose(f.read(0, 10_000), iq, atol=1 / 127.5)
    sidecar = json.loads(path.with_suffix(".json").read_text())
    assert sidecar["schema"] == "sdrscan.iq/1" and sidecar["format"] == "cu8"


def test_raw_rtl_sdr_file_reads_with_the_corpus_sidecar_style(tmp_path: Path):
    """rtl_sdr writes unsigned 8-bit I/Q pairs; the corpus sidecar names other keys and
    says cf32 because it describes a sibling file. The extension decides the format."""
    codes = np.array([255, 128, 0, 127, 128, 255], dtype=np.uint8)
    (tmp_path / "x.raw").write_bytes(codes.tobytes())
    (tmp_path / "x.json").write_text(
        json.dumps({"center_freq_hz": 121_000_000, "sample_rate": RATE, "format": "cf32"})
    )
    f = open_iq(tmp_path / "x.raw")
    assert (f.fmt, f.center_hz, f.rate, f.gain_db, f.n_samples) == (
        "cu8",
        121_000_000,
        RATE,
        None,
        3,
    )
    got = f.read(0, 3)
    np.testing.assert_allclose(got.real, [1.0, -1.0, 0.0039], atol=1e-3)
    np.testing.assert_allclose(got.imag, [0.0039, -0.0039, 1.0], atol=1e-3)


def test_cf32_with_corpus_sidecar(tmp_path: Path):
    iq = _tone(5_000)
    iq.tofile(tmp_path / "c.cf32")
    (tmp_path / "c.json").write_text(
        json.dumps({"center_freq_hz": 443_150_000, "sample_rate": RATE, "gain": 35})
    )
    f = open_iq(tmp_path / "c.cf32")
    assert (f.fmt, f.gain_db, f.duration_s) == ("cf32", 35.0, 5_000 / RATE)
    np.testing.assert_array_equal(f.read(1_000, 10), iq[1_000:1_010])


@pytest.mark.parametrize(
    ("datatype", "dtype", "scale"),
    [("cu8", np.uint8, None), ("ci8", np.int8, 128.0), ("cf32_le", "<f4", 1.0)],
)
def test_sigmf_pair(tmp_path: Path, datatype, dtype, scale):
    iq = _tone(4_000, amp=0.6)
    if datatype == "cu8":
        raw = np.empty(8_000, dtype=np.uint8)
        raw[0::2] = np.round(iq.real * 127.5 + 127.5)
        raw[1::2] = np.round(iq.imag * 127.5 + 127.5)
    else:
        inter = np.empty(8_000, dtype=np.float64)
        inter[0::2], inter[1::2] = iq.real, iq.imag
        raw = np.round(inter * scale).astype(dtype) if datatype == "ci8" else inter.astype(dtype)
    (tmp_path / "rec.sigmf-data").write_bytes(raw.tobytes())
    meta = {
        "global": {"core:datatype": datatype, "core:sample_rate": RATE},
        "captures": [{"core:sample_start": 0, "core:frequency": 162_475_000}],
        "annotations": [],
    }
    (tmp_path / "rec.sigmf-meta").write_text(json.dumps(meta))
    for name in ("rec.sigmf-meta", "rec.sigmf-data"):
        f = open_iq(tmp_path / name)
        assert (f.center_hz, f.rate, f.gain_db, f.n_samples) == (162_475_000, RATE, None, 4_000)
        np.testing.assert_allclose(f.read(0, 4_000), iq, atol=1 / 127)


def test_sigmf_rejects_an_unsupported_datatype(tmp_path: Path):
    (tmp_path / "r.sigmf-data").write_bytes(b"\0" * 16)
    (tmp_path / "r.sigmf-meta").write_text(
        json.dumps({"global": {"core:datatype": "cf32_be", "core:sample_rate": 1}, "captures": []})
    )
    with pytest.raises(IqFileError, match="cf32_be"):
        open_iq(tmp_path / "r.sigmf-meta")


def test_missing_sidecar_is_a_clear_error(tmp_path: Path):
    (tmp_path / "lonely.cu8").write_bytes(b"\x80" * 8)
    with pytest.raises(IqFileError, match="sidecar"):
        open_iq(tmp_path / "lonely.cu8")


def test_blocks_respect_offset_and_duration(tmp_path: Path):
    iq = np.arange(1_000, dtype=np.float32).astype(np.complex64)
    write_iq(tmp_path / "r.cf32", iq, rate=1_000, center_hz=1_000_000, gain_db=0.0)
    f = open_iq(tmp_path / "r.cf32")
    blocks = list(f.blocks(300, start=100, stop=900))
    assert [len(b) for b in blocks] == [300, 300, 200]
    np.testing.assert_array_equal(np.concatenate(blocks).real, np.arange(100, 900))


def test_slice_digest_hashes_the_exact_bytes(tmp_path: Path):
    write_iq(tmp_path / "r.cu8", _tone(1_000), rate=RATE, center_hz=1, gain_db=None)
    f = open_iq(tmp_path / "r.cu8")
    raw = (tmp_path / "r.cu8").read_bytes()
    assert f.sha256(100, 50) == hashlib.sha256(raw[200:300]).hexdigest()


def test_reader_is_memory_mapped(tmp_path: Path):
    """A multi-GB capture must not be loaded whole; the samples view is an np.memmap."""
    write_iq(tmp_path / "r.cf32", _tone(1_000), rate=RATE, center_hz=1, gain_db=None)
    f = open_iq(tmp_path / "r.cf32")
    assert isinstance(f.raw, np.memmap)


def test_file_source_plays_cu8_and_loops(tmp_path: Path):
    iq = _tone(1_000)
    write_iq(tmp_path / "r.cu8", iq, rate=RATE, center_hz=5_000_000, gain_db=10.0)
    src = FileSource(tmp_path / "r.cu8", loop=True)
    src.open()
    src.tune(5_000_000, 10.0)
    got = src.read(2_500)
    np.testing.assert_allclose(got, np.concatenate([iq, iq, iq[:500]]), atol=1 / 127.5)
