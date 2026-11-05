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


def pytest_collection_modifyitems(config, items):
    """Skip tests whose hardware, fixtures or tools are absent, so the default run is hermetic."""
    import importlib.util
    import os

    skips = {
        "hw": (os.environ.get("SDRSCAN_HW") != "1", "set SDRSCAN_HW=1 with a dongle attached"),
        "corpus": (
            not os.environ.get("SDRSCAN_IQ_CORPUS"),
            "set SDRSCAN_IQ_CORPUS to the directory of recorded IQ (tests/corpus/manifest.json)",
        ),
        "browser": (importlib.util.find_spec("playwright") is None, "Playwright not installed"),
    }
    for item in items:
        for marker, (skip, reason) in skips.items():
            if skip and marker in item.keywords:
                item.add_marker(pytest.mark.skip(reason=reason))
