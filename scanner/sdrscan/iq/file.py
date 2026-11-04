"""Replay of recorded ``.cf32`` IQ with a JSON sidecar (rate, center_hz, gain_db)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .base import IqSourceError


def sidecar_path(path: Path) -> Path:
    return Path(path).with_suffix(".json")


def write_cf32(path: Path, iq: np.ndarray, *, rate: int, center_hz: int, gain_db: float) -> None:
    path = Path(path)
    np.asarray(iq, dtype=np.complex64).tofile(path)
    meta = {"rate": int(rate), "center_hz": int(center_hz), "gain_db": float(gain_db)}
    sidecar_path(path).write_text(json.dumps(meta, sort_keys=True) + "\n")


class FileSource:
    """Plays a capture back. ``tune`` to any other center raises, so wrong tuning is visible."""

    def __init__(self, path: Path, loop: bool = False) -> None:
        self.path = Path(path)
        self.loop = loop
        meta = json.loads(sidecar_path(self.path).read_text())
        self.rate = int(meta["rate"])
        self.center_hz = int(meta["center_hz"])
        self.gain_db = float(meta["gain_db"])
        self._data: np.ndarray | None = None
        self._pos = 0
        self._reads = 0

    def open(self) -> None:
        self._data = np.fromfile(self.path, dtype=np.complex64)
        self._pos = 0

    def tune(self, center_hz: int, gain_db: float) -> None:
        if int(center_hz) != self.center_hz:
            raise ValueError(
                f"file was recorded at center {self.center_hz} Hz, cannot tune to {center_hz}"
            )

    def read(self, n: int) -> np.ndarray:
        if self._data is None:
            raise IqSourceError("file source is not open")
        data = self._data
        if len(data) == 0:
            raise EOFError(str(self.path))
        if not self.loop:
            if self._pos >= len(data):
                raise EOFError(str(self.path))
            out = data[self._pos : self._pos + n]
            self._pos += len(out)
            self._reads += 1
            return out.copy()
        idx = (self._pos + np.arange(n)) % len(data)
        self._pos = (self._pos + n) % len(data)
        self._reads += 1
        return data[idx]

    def close(self) -> None:
        self._data = None

    def health(self) -> dict:
        return {"dropped": 0, "reopen_count": 0, "last_block_age_s": 0.0, "reads": self._reads}
