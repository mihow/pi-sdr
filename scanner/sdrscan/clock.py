"""Injectable clocks. This module is the only place that reads real time or sleeps.

Every component that needs time takes a ``Clock``, so the test suite can run a simulated
week on ``VirtualClock`` in seconds.
"""

from __future__ import annotations

import time
import typing


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
