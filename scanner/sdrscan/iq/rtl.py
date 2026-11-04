"""RTL-SDR Blog V4 backend through pyrtlsdr on the system librtlsdr.

Gain is always manual: automatic gain makes dB values incomparable between hours, and v1
logged AGC gain as 0 dB. A reader thread drains the USB stream into a bounded ring, so
slow processing drops counted blocks instead of overflowing the dongle (v1 logged 90,805
overflows without one). pyrtlsdr is imported only on ``open``, so the package and its tests
run on hosts without librtlsdr.
"""

from __future__ import annotations

import collections
import numbers
import threading

import numpy as np

from .. import IQ_RATE
from ..clock import Clock
from .base import IqSourceError, settle_s

__all__ = ["RtlSource", "settle_s"]

_CHUNK = 16_384 * 4
_READ_TIMEOUT_S = 2.0


class RtlSource:
    def __init__(
        self,
        clock: Clock,
        device_index: int = 0,
        rate: int = IQ_RATE,
        threaded: bool = True,
        ring_s: float = 1.0,
    ) -> None:
        self.clock = clock
        self.device_index = device_index
        self.rate = rate
        self.threaded = threaded
        self._ring: collections.deque[np.ndarray] = collections.deque(
            maxlen=max(2, int(ring_s * rate / _CHUNK))
        )
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._sdr = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._center: int | None = None
        self._discard = 0
        self._stats = {"dropped": 0, "reopen_count": 0, "settle_discarded": 0}
        self._opened_once = False
        self._last_block: float | None = None
        self._pending = np.zeros(0, dtype=np.complex64)

    def open(self) -> None:
        try:
            import rtlsdr  # lazy: optional dependency
        except ImportError as exc:
            raise IqSourceError("pyrtlsdr is not installed (pip install sdrscan[rtl])") from exc
        if rtlsdr is None:  # pragma: no cover - only when the import is blocked
            raise IqSourceError("pyrtlsdr is not installed (pip install sdrscan[rtl])")
        try:
            self._sdr = rtlsdr.RtlSdr(device_index=self.device_index)
            self._sdr.sample_rate = self.rate
        except Exception as exc:  # librtlsdr raises IOError/LibUSBError variants
            self._sdr = None
            raise IqSourceError(f"cannot open RTL-SDR: {exc}") from exc
        if self._opened_once:
            self._stats["reopen_count"] += 1
        self._opened_once = True
        self._center = None
        self._error = None
        if self.threaded:
            self._stop.clear()
            self._thread = threading.Thread(target=self._reader, name="rtl-reader", daemon=True)
            self._thread.start()

    def tune(self, center_hz: int, gain_db: float) -> None:
        if gain_db is None or isinstance(gain_db, bool) or not isinstance(gain_db, numbers.Real):
            raise ValueError(f"a manual gain in dB is required, got {gain_db!r}")
        if self._sdr is None:
            raise IqSourceError("RTL-SDR is not open")
        discard = int(settle_s(self._center, int(center_hz)) * self.rate)
        with self._lock:
            try:
                self._sdr.center_freq = int(center_hz)
                self._sdr.gain = float(gain_db)
            except Exception as exc:
                raise IqSourceError(f"retune failed: {exc}") from exc
            with self._cond:
                self._ring.clear()
                self._pending = np.zeros(0, dtype=np.complex64)
                self._discard = discard
        self._center = int(center_hz)
        self._stats["settle_discarded"] = discard

    def read(self, n: int) -> np.ndarray:
        if self._sdr is None:
            raise IqSourceError("RTL-SDR is not open")
        if not self.threaded:
            return self._read_sync(n)
        parts = [self._pending] if len(self._pending) else []
        have = len(self._pending)
        with self._cond:
            while have < n:
                if self._error is not None:
                    raise IqSourceError(f"RTL-SDR read failed: {self._error}")
                if not self._ring:
                    if not self._cond.wait(timeout=_READ_TIMEOUT_S):
                        raise IqSourceError("RTL-SDR read timed out")
                    continue
                block = self._ring.popleft()
                parts.append(block)
                have += len(block)
        data = np.concatenate(parts) if parts else np.zeros(0, np.complex64)
        self._pending = data[n:]
        return data[:n]

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_READ_TIMEOUT_S + 1)
            self._thread = None
        if self._sdr is not None:
            try:
                self._sdr.close()
            finally:
                self._sdr = None

    def health(self) -> dict:
        age = None if self._last_block is None else self.clock.monotonic() - self._last_block
        return {**self._stats, "last_block_age_s": age}

    # -- internals ---------------------------------------------------------------------
    def _read_sync(self, n: int) -> np.ndarray:
        try:
            if self._discard:
                self._sdr.read_samples(self._discard)
                self._discard = 0
            data = np.asarray(self._sdr.read_samples(n), dtype=np.complex64)
        except Exception as exc:
            raise IqSourceError(f"RTL-SDR read failed: {exc}") from exc
        self._last_block = self.clock.monotonic()
        return data

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    block = np.asarray(self._sdr.read_samples(_CHUNK), dtype=np.complex64)
            except Exception as exc:
                with self._cond:
                    self._error = exc
                    self._cond.notify_all()
                return
            with self._cond:
                if self._discard:
                    skip = min(self._discard, len(block))
                    self._discard -= skip
                    block = block[skip:]
                if len(block):
                    if len(self._ring) == self._ring.maxlen:
                        self._stats["dropped"] += 1
                    self._ring.append(block)
                    self._last_block = self.clock.monotonic()
                    self._cond.notify_all()
