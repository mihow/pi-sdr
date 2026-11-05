"""Recorded IQ: one reader for every capture format the scanner meets, and a file source.

Supported data formats are rtl_sdr's raw unsigned 8-bit interleaved I/Q (``.cu8`` or
``.raw``), signed 8-bit (``.ci8``/``.cs8``), complex float32 (``.cf32``/``.cfile``) and
SigMF pairs (``.sigmf-meta`` + ``.sigmf-data`` with ``cu8``, ``ci8``, ``ci16_le`` or
``cf32_le``). A non-SigMF file has a JSON sidecar with the same stem. Two sidecar styles are
read: the scanner's own (``rate``, ``center_hz``, ``gain_db``) and the older capture
scripts' (``sample_rate``, ``center_freq_hz``, ``gain``, ``format``, ``device``, ...). Gain may
be missing. A ``gain`` value from those scripts is the device's own setting and is not
always dB of gain (SDRplay records IF gain reduction there).

The extension decides the format when it is one of the above, because a sidecar can
describe a sibling file in another format. Samples are memory-mapped, so a multi-GB capture
costs only the slices that are read.
"""

from __future__ import annotations

import hashlib
import json
import typing
from pathlib import Path

import numpy as np

from .base import IqSourceError

IQ_SCHEMA = "sdrscan.iq/1"

# Format -> (numpy dtype of one component, offset, divisor). A sample is two components.
_FORMATS: dict[str, tuple[str, float, float]] = {
    "cu8": ("u1", 127.5, 127.5),
    "ci8": ("i1", 0.0, 128.0),
    "ci16": ("<i2", 0.0, 32768.0),
    "cf32": ("<f4", 0.0, 1.0),
}
_EXT_FORMAT = {
    ".cu8": "cu8",
    ".raw": "cu8",
    ".ci8": "ci8",
    ".cs8": "ci8",
    ".cf32": "cf32",
    ".cfile": "cf32",
}
_SIGMF_FORMAT = {"cu8": "cu8", "ci8": "ci8", "ci16_le": "ci16", "cf32_le": "cf32"}


class IqFileError(ValueError):
    """The capture or its metadata cannot be read."""


def sidecar_path(path: Path) -> Path:
    return Path(path).with_suffix(".json")


def bytes_per_sample(fmt: str) -> int:
    return 2 * np.dtype(_FORMATS[fmt][0]).itemsize


class IqFile:
    """A memory-mapped capture with its metadata. Sample indexes are complex samples."""

    def __init__(
        self,
        data_path: Path,
        *,
        fmt: str,
        rate: int,
        center_hz: int,
        gain_db: float | None,
        meta: dict | None = None,
    ) -> None:
        if fmt not in _FORMATS:
            raise IqFileError(f"unsupported IQ format {fmt!r}")
        self.path = Path(data_path)
        self.fmt = fmt
        self.rate = int(rate)
        self.center_hz = int(center_hz)
        self.gain_db = gain_db
        self.meta = dict(meta or {})
        dtype, self._offset, self._div = _FORMATS[fmt]
        self.n_samples = self.path.stat().st_size // bytes_per_sample(fmt)
        if self.n_samples:
            self.raw: np.ndarray = np.memmap(
                self.path, dtype=dtype, mode="r", shape=(2 * self.n_samples,)
            )
        else:
            self.raw = np.zeros(0, dtype=dtype)

    @property
    def duration_s(self) -> float:
        return self.n_samples / self.rate

    def read(self, start: int, n: int) -> np.ndarray:
        """``n`` samples from ``start`` (fewer at the end), complex64 scaled to [-1, 1]."""
        start = max(0, int(start))
        stop = min(self.n_samples, start + max(0, int(n)))
        comp = self.raw[2 * start : 2 * stop].astype(np.float32)
        if self._offset:
            comp -= self._offset
        if self._div != 1.0:
            comp /= self._div
        return comp.view(np.complex64)

    def blocks(
        self, n: int, start: int = 0, stop: int | None = None
    ) -> typing.Iterator[np.ndarray]:
        """Consecutive blocks of ``n`` samples over [start, stop); the last may be short."""
        stop = self.n_samples if stop is None else min(self.n_samples, int(stop))
        for i in range(int(start), stop, n):
            yield self.read(i, min(n, stop - i))

    def sample_range(self, offset_s: float = 0.0, seconds: float | None = None) -> tuple[int, int]:
        """[start, stop) sample indexes for an offset and a duration in seconds."""
        start = min(self.n_samples, int(round(offset_s * self.rate)))
        if seconds is None:
            return start, self.n_samples
        return start, min(self.n_samples, start + int(round(seconds * self.rate)))

    def byte_range(self, start: int, n: int) -> tuple[int, int]:
        bps = bytes_per_sample(self.fmt)
        return start * bps, (start + n) * bps

    def sha256(self, start: int, n: int) -> str:
        """Digest of the exact file bytes of samples [start, start + n)."""
        lo, hi = self.byte_range(start, n)
        h = hashlib.sha256()
        with open(self.path, "rb") as fh:
            fh.seek(lo)
            left = hi - lo
            while left > 0:
                chunk = fh.read(min(left, 1 << 22))
                if not chunk:
                    break
                h.update(chunk)
                left -= len(chunk)
        return h.hexdigest()


def open_iq(path: Path) -> IqFile:
    """Open a capture of any supported format (see the module docstring)."""
    path = Path(path)
    if path.suffix in {".sigmf-meta", ".sigmf-data", ".sigmf"}:
        return _open_sigmf(path.with_suffix(".sigmf-meta"), path.with_suffix(".sigmf-data"))
    side = sidecar_path(path)
    if not side.exists():
        raise IqFileError(f"{path.name}: no JSON sidecar ({side.name}) next to the capture")
    meta = json.loads(side.read_text())
    fmt = _EXT_FORMAT.get(path.suffix.lower()) or str(meta.get("format", ""))
    if fmt not in _FORMATS:
        raise IqFileError(f"{path.name}: cannot tell the IQ format from the name or sidecar")
    rate = meta.get("rate", meta.get("sample_rate"))
    center = meta.get("center_hz", meta.get("center_freq_hz"))
    if rate is None or center is None:
        raise IqFileError(f"{side.name}: the sidecar needs a sample rate and a center frequency")
    gain = meta.get("gain_db", meta.get("gain"))
    return IqFile(
        path,
        fmt=fmt,
        rate=int(rate),
        center_hz=int(center),
        gain_db=None if gain is None else float(gain),
        meta=meta,
    )


def _open_sigmf(meta_path: Path, data_path: Path) -> IqFile:
    if not meta_path.exists() or not data_path.exists():
        raise IqFileError(f"{meta_path.stem}: SigMF needs both .sigmf-meta and .sigmf-data")
    meta = json.loads(meta_path.read_text())
    glob = meta.get("global", {})
    datatype = glob.get("core:datatype", "")
    fmt = _SIGMF_FORMAT.get(datatype)
    if fmt is None:
        raise IqFileError(f"{meta_path.name}: unsupported SigMF datatype {datatype!r}")
    captures = meta.get("captures") or [{}]
    freq = captures[0].get("core:frequency")
    if freq is None or "core:sample_rate" not in glob:
        raise IqFileError(f"{meta_path.name}: needs core:sample_rate and a capture core:frequency")
    gain = glob.get("sdrscan:gain_db")
    return IqFile(
        data_path,
        fmt=fmt,
        rate=int(glob["core:sample_rate"]),
        center_hz=int(freq),
        gain_db=None if gain is None else float(gain),
        meta=meta,
    )


def _encode(iq: np.ndarray, fmt: str) -> bytes:
    iq = np.ascontiguousarray(iq, dtype=np.complex64)
    if fmt == "cf32":
        return iq.tobytes()
    comp = iq.view(np.float32)
    return np.clip(np.round(comp * 127.5 + 127.5), 0, 255).astype(np.uint8).tobytes()


class IqWriter:
    """Streams blocks to a ``.cu8`` or ``.cf32`` capture, then writes the sidecar on close."""

    def __init__(
        self,
        path: Path,
        *,
        rate: int,
        center_hz: int,
        gain_db: float | None,
        fmt: str | None = None,
        extra: dict | None = None,
    ) -> None:
        self.path = Path(path)
        self.fmt = fmt or _EXT_FORMAT.get(self.path.suffix.lower(), "")
        if self.fmt not in {"cu8", "cf32"}:
            raise IqFileError(f"{self.path.name}: write cu8 or cf32 (got {self.fmt!r})")
        self.meta = {
            **(extra or {}),
            "schema": IQ_SCHEMA,
            "rate": int(rate),
            "center_hz": int(center_hz),
            "gain_db": None if gain_db is None else float(gain_db),
            "format": self.fmt,
        }
        self.n_samples = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "wb")  # noqa: SIM115 - closed in close()

    def write(self, iq: np.ndarray) -> None:
        self._fh.write(_encode(iq, self.fmt))
        self.n_samples += len(iq)

    def close(self, extra: dict | None = None) -> None:
        self._fh.close()
        meta = {**(extra or {}), **self.meta, "n_samples": self.n_samples}
        sidecar_path(self.path).write_text(json.dumps(meta, sort_keys=True) + "\n")

    def __enter__(self) -> IqWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def write_iq(
    path: Path,
    iq: np.ndarray,
    *,
    rate: int,
    center_hz: int,
    gain_db: float | None,
    fmt: str | None = None,
    extra: dict | None = None,
) -> None:
    """Write a whole capture (format from ``fmt`` or the extension) and its sidecar."""
    with IqWriter(path, rate=rate, center_hz=center_hz, gain_db=gain_db, fmt=fmt, extra=extra) as w:
        w.write(iq)


def write_cf32(path: Path, iq: np.ndarray, *, rate: int, center_hz: int, gain_db: float) -> None:
    write_iq(path, iq, rate=rate, center_hz=center_hz, gain_db=gain_db, fmt="cf32")


class FileSource:
    """Plays a capture back. ``tune`` to any other center raises, so wrong tuning is visible."""

    def __init__(self, path: Path, loop: bool = False) -> None:
        self.path = Path(path)
        self.loop = loop
        f = open_iq(self.path)
        self.rate = f.rate
        self.center_hz = f.center_hz
        self.gain_db = f.gain_db
        self._file: IqFile | None = None
        self._pos = 0
        self._reads = 0

    def open(self) -> None:
        self._file = open_iq(self.path)
        self._pos = 0

    def tune(self, center_hz: int, gain_db: float) -> None:
        if int(center_hz) != self.center_hz:
            raise ValueError(
                f"file was recorded at center {self.center_hz} Hz, cannot tune to {center_hz}"
            )

    def read(self, n: int) -> np.ndarray:
        f = self._file
        if f is None:
            raise IqSourceError("file source is not open")
        if f.n_samples == 0:
            raise EOFError(str(self.path))
        if not self.loop:
            if self._pos >= f.n_samples:
                raise EOFError(str(self.path))
            out = f.read(self._pos, n)
            self._pos += len(out)
            self._reads += 1
            return out
        parts, need = [], n
        while need > 0:
            part = f.read(self._pos, need)
            parts.append(part)
            need -= len(part)
            self._pos = (self._pos + len(part)) % f.n_samples
        self._reads += 1
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def close(self) -> None:
        self._file = None

    def health(self) -> dict:
        return {"dropped": 0, "reopen_count": 0, "last_block_age_s": 0.0, "reads": self._reads}
