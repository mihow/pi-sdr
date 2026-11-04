"""Speech-to-text and prose subprocesses run at nice 19, without lowering the scan loop."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading

import pytest

from sdrscan.lowprio import call_low, lower_this_thread

pytestmark = pytest.mark.skipif(shutil.which("nice") is None, reason="needs nice(1)")


def test_call_low_runs_children_at_nice_19_and_leaves_the_caller_alone():
    before = os.getpriority(os.PRIO_PROCESS, 0)
    out = call_low(subprocess.run, ["nice"], capture_output=True, check=True)
    assert int(out.stdout) == 19
    assert os.getpriority(os.PRIO_PROCESS, 0) == before


def test_call_low_passes_exceptions_through():
    with pytest.raises(ZeroDivisionError):
        call_low(lambda: 1 / 0)


def test_lower_this_thread_affects_only_that_thread():
    seen: list[int] = []

    def work():
        assert lower_this_thread()
        seen.append(int(subprocess.run(["nice"], capture_output=True).stdout))

    t = threading.Thread(target=work)
    t.start()
    t.join()
    assert seen == [19]
    assert int(subprocess.run(["nice"], capture_output=True).stdout) == os.getpriority(
        os.PRIO_PROCESS, 0
    )
