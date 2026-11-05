from __future__ import annotations

from pathlib import Path

import pytest

from sdrscan.config import Config
from sdrscan.health import HealthMonitor


def _usb(sys_root: Path, vid: str = "0bda", pid: str = "2838") -> Path:
    d = sys_root / "bus/usb/devices/1-1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "idVendor").write_text(vid + "\n")
    (d / "idProduct").write_text(pid + "\n")
    return d


def _temp(sys_root: Path, c: float) -> None:
    (sys_root / "class/thermal/thermal_zone0/temp").write_text(f"{int(c * 1000)}\n")


def _monitor(sys_root, vclock, tmp_path, **kw):
    def no_vcgencmd(*a, **k):
        raise FileNotFoundError("vcgencmd")

    kw.setdefault("run", no_vcgencmd)
    return HealthMonitor(Config(data_root=tmp_path / "d"), vclock, sys_root=sys_root, **kw)


def test_thermal_modes_with_hysteresis(sys_root, vclock, tmp_path):
    _usb(sys_root)
    hm = _monitor(sys_root, vclock, tmp_path)
    seen = []
    for c in (60, 71, 79, 83, 80, 78.5, 76, 74, 68, 66):
        _temp(sys_root, c)
        hm.sample()
        seen.append(hm.mode())
    assert seen == [
        "normal",
        "hot",
        "very_hot",
        "critical",
        "critical",  # 80 is not below 82 - 3
        "very_hot",  # 78.5 is below 79, and still at least 78
        "very_hot",  # 76 is not below 75
        "hot",
        "hot",  # 68 is not below 67
        "normal",
    ]


def test_throttle_flag_means_very_hot(sys_root, vclock, tmp_path):
    import subprocess

    _usb(sys_root)

    def vcgencmd(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, b"throttled=0x50005\n", b"")

    hm = _monitor(sys_root, vclock, tmp_path, run=vcgencmd)
    h = hm.sample()
    assert h.throttled == 0x50005 and hm.mode() == "very_hot"


def test_missing_dongle_is_flagged(sys_root, vclock, tmp_path):
    hm = _monitor(sys_root, vclock, tmp_path)
    h = hm.sample()
    assert not h.dongle_ok and "no_dongle" in h.flags
    _usb(sys_root)
    assert hm.sample().dongle_ok


def test_missing_vcgencmd_is_not_an_error(sys_root, vclock, tmp_path):
    _usb(sys_root)
    h = _monitor(sys_root, vclock, tmp_path).sample()
    assert h.throttled is None and h.temp_c == pytest.approx(45.0)


def test_watchdog_needs_fresh_iq(sys_root, vclock, tmp_path):
    _usb(sys_root)
    sent = []
    age = {"s": 1.0}
    hm = _monitor(sys_root, vclock, tmp_path, iq_age=lambda: age["s"], notify=sent.append)
    assert hm.notify_watchdog()
    age["s"] = 11.0
    assert not hm.notify_watchdog()
    assert sent == ["WATCHDOG=1"]


def test_health_row_and_digest(sys_root, vclock, tmp_path):
    _usb(sys_root)
    hm = _monitor(sys_root, vclock, tmp_path)
    vclock.advance(7200)
    h = hm.sample()
    row = h.to_row()
    assert row["ts"] == int(vclock.now()) and row["dongle_ok"] == 1 and row["mode"] == "normal"
    d = h.to_digest()
    assert d["up_s"] == 7200 and d["dongle"] is True and d["temp_c"] == 45.0
    assert set(d) >= {"mode", "flags", "disk_free_pct", "quota_used_mb", "clock_ok", "iq_age_s"}


def _throttled_file(sys_root: Path, value: str) -> None:
    """The firmware's sysfs node, which the container can read without vcgencmd."""
    p = sys_root / "devices/platform/soc/soc:firmware/get_throttled"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value + "\n")


def test_throttle_state_comes_from_sysfs_without_vcgencmd(sys_root, vclock, tmp_path):
    _usb(sys_root)
    _throttled_file(sys_root, "50005")
    hm = _monitor(sys_root, vclock, tmp_path)
    h = hm.sample()
    assert h.throttled == 0x50005 and hm.mode() == "very_hot"
    assert "under_voltage" in h.flags


def test_a_past_brown_out_is_reported_but_does_not_hold_the_governor(sys_root, vclock, tmp_path):
    """Bits 16 to 19 mean "has occurred since boot". One brown-out must not disable
    dwells, clips and speech-to-text until the next reboot."""
    _usb(sys_root)
    _throttled_file(sys_root, "50005")
    hm = _monitor(sys_root, vclock, tmp_path)
    hm.sample()
    assert hm.mode() == "very_hot"
    _throttled_file(sys_root, "50000")  # the supply recovered; the sticky bits stay
    h = hm.sample()
    assert hm.mode() == "normal"
    assert "power_fault_since_boot" in h.flags and "under_voltage" not in h.flags


def test_dongle_present_but_unreadable_is_not_ok(sys_root, vclock, tmp_path):
    # After a USB reset the dongle re-enumerates at a new node, so sysfs still lists it, but
    # the scanner holds the old node and every read fails. Health must not report it as OK.
    _usb(sys_root)
    _temp(sys_root, 50)
    status = {"mode": "normal"}
    hm = _monitor(sys_root, vclock, tmp_path, status=lambda: status)
    assert hm.sample().dongle_ok is True
    status["mode"] = "no_dongle"
    h = hm.sample()
    assert h.dongle_ok is False
    assert "dongle_busy" in h.flags
