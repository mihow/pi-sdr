"""Shared fixtures: a virtual clock, a temporary data root and a fake sysfs root."""

from __future__ import annotations

from pathlib import Path

import pytest

# 2026-06-01 00:00:00 UTC, a Monday. Fixed so hour-of-week maths is stable in tests.
T0 = 1_780_272_000.0


@pytest.fixture
def vclock():
    from sdrscan.clock import VirtualClock

    return VirtualClock(start=T0)


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture
def sys_root(tmp_path: Path) -> Path:
    root = tmp_path / "sys"
    (root / "class/thermal/thermal_zone0").mkdir(parents=True)
    (root / "class/thermal/thermal_zone0/temp").write_text("45000\n")
    return root
