"""Injectable clocks. This module is the only place that reads real time or sleeps.

Every component that needs time takes a ``Clock``, so the test suite can run a simulated
week on ``VirtualClock`` in seconds.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import time
import typing

_TIME_ERROR = 5  # adjtimex(2): the kernel clock is not synchronized


@typing.runtime_checkable
class Clock(typing.Protocol):
    def now(self) -> float:
        """Wall time, unix seconds UTC. Can jump (NTP corrections)."""

    def monotonic(self) -> float:
        """Seconds that never go backward. Use it for durations."""

    def sleep(self, s: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, s: float) -> None:
        if s > 0:
            time.sleep(s)


class VirtualClock:
    """A clock that only moves when told to. ``sleep`` advances it instantly."""

    def __init__(self, start: float) -> None:
        self._now = float(start)
        self._mono = 0.0

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, s: float) -> None:
        if s > 0:
            self.advance(s)

    def advance(self, s: float) -> None:
        if s < 0:
            raise ValueError("a virtual clock cannot advance by a negative amount")
        self._now += s
        self._mono += s

    def set(self, t: float) -> None:
        """Jump wall time to ``t``, as an NTP correction would. Monotonic time is unchanged."""
        self._now = float(t)


@functools.cache
def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def kernel_clock_synced() -> bool | None:
    """Whether NTP has synchronized the kernel clock, from a read-only ``adjtimex(2)``.

    Works inside a container (reading needs no capability). None where it is unavailable.
    """
    try:
        libc = _libc()
        buf = ctypes.create_string_buffer(512)  # struct timex, modes = 0 means read only
        state = libc.adjtimex(buf)
    except (OSError, AttributeError):
        return None
    if state < 0:
        return None
    return state != _TIME_ERROR
