"""The IQ source interface shared by the RTL-SDR, file and synthetic backends."""

from __future__ import annotations

import typing

import numpy as np

# The RTL-SDR Blog V4 switches tuner inputs at these frequencies. A retune across one
# needs a longer settle discard (spec 5.3). Both values are estimates until measured.
INPUT_SWITCH_HZ = (28_800_000, 250_000_000)
SETTLE_S = 0.030
SETTLE_SWITCH_S = 0.060


class IqSourceError(RuntimeError):
    """The source failed (unplugged, busy, read error). The caller should back off and reopen."""


@typing.runtime_checkable
class IqSource(typing.Protocol):
    def open(self) -> None: ...

    def tune(self, center_hz: int, gain_db: float) -> None:
        """Retune with a manual gain. Returns after the settle discard is done."""

    def read(self, n: int) -> np.ndarray:
        """Return ``n`` complex64 samples scaled to [-1, 1]."""

    def close(self) -> None: ...

    def health(self) -> dict: ...


def settle_s(prev_hz: int | None, new_hz: int) -> float:
    """Settle discard for a retune; longer when it crosses a V4 input switch."""
    if prev_hz is None:
        return SETTLE_SWITCH_S
    lo, hi = sorted((prev_hz, new_hz))
    if any(lo < sw <= hi for sw in INPUT_SWITCH_HZ):
        return SETTLE_SWITCH_S
    return SETTLE_S
