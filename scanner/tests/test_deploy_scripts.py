from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sdrscan.config import Config
from sdrscan.iq.scene import Scene
from sdrscan.iq.synthetic import SyntheticSource
from sdrscan.service import WATCHDOG_EXIT, Service

from .test_clips import FakeFs

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_passes():
    subprocess.run(["shellcheck", *map(str, DEPLOY.glob("*.sh"))], check=True)


def _stub_path(tmp_path: Path) -> tuple[dict, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("docker", "systemctl", "rtl_test", "zstd", "install", "fuser"):
        (bin_dir / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\nexit 0\n')
        (bin_dir / tool).chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SDRSCAN_ETC": str(tmp_path / "etc"),
        "SDRSCAN_UNIT_DIR": str(tmp_path / "units"),
    }
    return env, log


def test_install_dry_run_prints_every_step_and_changes_nothing(tmp_path):
    env, log = _stub_path(tmp_path)
    out = subprocess.run(
        ["bash", str(DEPLOY / "install.sh"), "--dry-run", "--image", "sdrscan.tar.zst"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for step in (
        "1. Preflight",
        "2. Load the image",
        "3. Configuration",
        "4. Service unit",
        "5. Wait for a healthy service",
    ):
        assert step in out
    for cmd in (
        "zstd -dc sdrscan.tar.zst | docker load",
        "systemctl enable sdrscan.service",
        "rtl_test -s 2400000",
        "docker exec sdrscan sdrscan health",
    ):
        assert cmd in out
    calls = log.read_text().splitlines() if log.exists() else []
    read_only = ("docker image inspect", "fuser ")
    assert all(c.startswith(read_only) for c in calls), calls
    assert not (tmp_path / "etc").exists() and not (tmp_path / "units").exists()


def test_uninstall_dry_run_keeps_data(tmp_path):
    env, log = _stub_path(tmp_path)
    out = subprocess.run(
        ["bash", str(DEPLOY / "uninstall.sh"), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "kept the sdrscan-data volume" in out and not log.exists()


def test_unit_has_no_audio_access_and_no_ordering_on_other_services():
    unit = (DEPLOY / "sdrscan.service").read_text()
    lower = unit.lower()
    for word in ("pipewire", "pulse", "/dev/snd", "audio", "ambient", "tailscale"):
        body = "\n".join(line for line in lower.splitlines() if not line.startswith("#"))
        assert word not in body, word
    ordering = [
        line
        for line in unit.splitlines()
        if line.split("=")[0] in {"After", "Before", "Requires", "Wants", "BindsTo"}
    ]
    assert ordering == ["Requires=docker.service", "After=docker.service"]
    for setting in (
        "Restart=always",
        "RestartSec=15",
        "StartLimitIntervalSec=0",
        "TimeoutStartSec=300",
        "ExecStart=/usr/local/lib/sdrscan/sdrscan-run",
    ):
        assert setting in unit, setting
    wrapper = (DEPLOY / "sdrscan-run").read_text()
    for flag in (
        "--blkio-weight 100",
        '--memory "$MEMORY"',
        '--cpus "$CPUS"',
        "--read-only",
        # Owned by the image's sdrscan user (uid 999): clips are staged here before encoding,
        # and a root-owned tmpfs made the first recorded clip crash the scanner.
        "--tmpfs /run/sdrscan:size=64m,uid=999,gid=999,mode=0700",
    ):
        assert flag in wrapper, flag
    settings = [line.split("=")[0] for line in unit.splitlines() if not line.startswith("#")]
    for dead in ("Nice", "IOSchedulingClass"):  # would reach only the docker client
        assert dead not in settings, dead
    # The whole USB bus is never mapped: the relay board shares it (see sdrscan-run).
    assert "/dev/bus/usb:/dev/bus/usb" not in unit
    assert "device-cgroup-rule" not in unit


def _fake_usb(root: Path, devices: list[tuple[str, str, int, int]]) -> Path:
    """Build a sysfs tree with (vendor, product, busnum, devnum) USB devices."""
    sysfs = root / "sys" / "bus" / "usb" / "devices"
    for i, (vid, pid, bus, dev) in enumerate(devices):
        d = sysfs / f"1-{i + 1}"
        d.mkdir(parents=True)
        (d / "idVendor").write_text(vid + "\n")
        (d / "idProduct").write_text(pid + "\n")
        (d / "busnum").write_text(f"{bus}\n")
        (d / "devnum").write_text(f"{dev}\n")
    return sysfs


def _run_wrapper(tmp_path: Path, devices, extra_env: dict[str, str] | None = None):
    sysfs = _fake_usb(tmp_path, devices)
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_OUT"\n')
    docker.chmod(0o755)
    env = {
        "PATH": os.environ["PATH"],
        "SDRSCAN_SYSFS_USB": str(sysfs),
        "SDRSCAN_DOCKER": str(docker),
        "SDRSCAN_USB_GID": "46",
        "SDRSCAN_DEV_STAT": "0",  # skip the device-node check, the fake tree has no /dev
        "SDRSCAN_NO_DONGLE_WAIT_S": "0",
        "ARGS_OUT": str(tmp_path / "args"),
        **(extra_env or {}),
    }
    r = subprocess.run(
        ["bash", str(DEPLOY / "sdrscan-run")], env=env, capture_output=True, text=True
    )
    args = (tmp_path / "args").read_text().splitlines() if (tmp_path / "args").exists() else []
    return r, args


def test_run_wrapper_passes_only_the_dongle_node(tmp_path):
    relay = ("303a", "1001", 1, 2)  # the ESP32 relay board on the same bus
    dongle = ("0bda", "2838", 1, 7)
    r, args = _run_wrapper(tmp_path, [relay, dongle])
    assert r.returncode == 0, r.stderr
    assert "/dev/bus/usb/001/007" in args
    assert not any("001/002" in a for a in args), "the relay board must stay out"
    assert not any(a.startswith("/dev/bus/usb:") for a in args)
    assert "--privileged" not in args


def test_run_wrapper_waits_before_exiting_without_a_dongle():
    # systemd restarts the unit 15 s after each exit, forever; without this wait a missing
    # dongle would write a journal line every 15 s onto a small card.
    wrapper = (DEPLOY / "sdrscan-run").read_text()
    assert 'SDRSCAN_NO_DONGLE_WAIT_S:-60' in wrapper


def test_run_wrapper_exits_71_without_a_dongle(tmp_path):
    r, args = _run_wrapper(tmp_path, [("303a", "1001", 1, 2)])
    assert r.returncode == 71
    assert args == []


def test_small_card_config_loads_with_byte_floors_and_no_stt():
    from sdrscan.config import load_config

    cfg = load_config(DEPLOY / "config.small-card.yaml")
    assert cfg.stt.engine == "none"
    assert cfg.quota.free_evict_bytes == 1024**3
    assert cfg.quota.clip_quota_bytes == 300 * 1024**2
    assert cfg.quota.raw_retention_days == 7
    assert cfg.thermal.very_hot_c == 75.0


def test_template_config_loads():
    from sdrscan.config import load_config

    cfg = load_config(DEPLOY / "config.template.yaml")
    assert cfg.uplink.target == "s3" and cfg.live.enabled is False


def test_progress_watchdog_exits_when_the_loop_is_stuck(tmp_path, vclock, sys_root):
    cfg = Config(data_root=tmp_path / "d", stage_root=tmp_path / "s")
    svc = Service(
        cfg,
        vclock,
        SyntheticSource(Scene.from_yaml("emitters: []\n", vclock), vclock),
        spectrum="psd",
        statvfs=FakeFs(0.5),
        sys_root=sys_root,
    )
    codes = []
    vclock.advance(100)
    assert svc.watchdog_check(codes.append) and not codes
    vclock.advance(30)
    assert not svc.watchdog_check(codes.append)
    assert codes == [WATCHDOG_EXIT]
    svc.shutdown()


def test_run_wrapper_cpu_cap_comes_from_the_env_file(tmp_path):
    """A hot site lowers the CPU cap in docker.env; it must survive every container restart."""
    dongle = [("0bda", "2838", 1, 7)]
    _, args = _run_wrapper(tmp_path / "default", dongle)
    assert args[args.index("--cpus") + 1] == "2"
    _, args = _run_wrapper(tmp_path / "hot", dongle, {"SDRSCAN_CPUS": "1.0"})
    assert args[args.index("--cpus") + 1] == "1.0"
