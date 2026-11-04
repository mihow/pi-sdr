from __future__ import annotations

import time

from sdrscan.clock import Clock, RealClock, VirtualClock


def test_virtual_sleep_advances_both_clocks_without_real_delay():
    c = VirtualClock(start=1000.0)
    t_real = time.perf_counter()
    c.sleep(3600)
    assert time.perf_counter() - t_real < 0.5
    assert c.now() == 4600.0
    assert c.monotonic() == 3600.0


def test_set_backward_changes_now_but_never_decreases_monotonic():
    c = VirtualClock(start=1000.0)
    c.advance(10)
    m = c.monotonic()
    c.set(500.0)
    assert c.now() == 500.0
    assert c.monotonic() == m
    c.set(2000.0)
    assert c.now() == 2000.0
    assert c.monotonic() == m


def test_negative_advance_is_refused():
    c = VirtualClock(start=0.0)
    try:
        c.advance(-1)
    except ValueError:
        return
    raise AssertionError("advance(-1) should raise")


def test_both_clocks_satisfy_protocol():
    assert isinstance(RealClock(), Clock)
    assert isinstance(VirtualClock(0.0), Clock)
    r = RealClock()
    assert r.monotonic() <= r.monotonic()
