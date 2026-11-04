"""Run slow helper work (speech-to-text, the prose model) at the lowest CPU priority.

The container's processes are children of the Docker daemon, so a systemd ``Nice=`` on the
unit never reaches them. On Linux, niceness is per thread and a child process inherits the
niceness of the thread that starts it, so lowering one worker thread lowers every
subprocess it runs while the scan loop keeps its priority.
"""

from __future__ import annotations

import os
import threading
import typing

LOW_NICE = 19


def lower_this_thread(nice: int = LOW_NICE) -> bool:
    """Lower the calling thread to ``nice``. Returns False where that is not supported."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), nice)
    except (AttributeError, OSError):
        return False
    return True


T = typing.TypeVar("T")


def call_low(fn: typing.Callable[..., T], *args, **kw) -> T:
    """Call ``fn`` on a short-lived thread at ``LOW_NICE`` and return its result."""
    box: dict[str, typing.Any] = {}

    def run() -> None:
        lower_this_thread()
        try:
            box["value"] = fn(*args, **kw)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller below
            box["error"] = exc

    t = threading.Thread(target=run, name="sdrscan-low")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]
