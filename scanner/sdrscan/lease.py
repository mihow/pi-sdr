"""Hand the tuner to an external decoder (for example rtl_433) for a time slot.

Only one process can hold the RTL-SDR. The lease closes our source, waits for USB to let
go, yields, and always reopens the source afterwards, even if the job fails.
"""

from __future__ import annotations

import contextlib
import typing

from .clock import Clock

RELEASE_S = 0.3


class ExternalJob(typing.Protocol):
    name: str
    max_runtime_s: float

    def argv(self) -> list[str]: ...

    def parse(self, line: str) -> list[dict]: ...


class _Closeable(typing.Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...


class DongleLease:
    def __init__(self, source: _Closeable, clock: Clock, release_s: float = RELEASE_S) -> None:
        self.source = source
        self.clock = clock
        self.release_s = release_s
        self.active: ExternalJob | None = None

    @contextlib.contextmanager
    def lease(self, job: ExternalJob) -> typing.Iterator[None]:
        self.source.close()
        self.clock.sleep(self.release_s)
        self.active = job
        try:
            yield
        finally:
            self.active = None
            self.source.open()
