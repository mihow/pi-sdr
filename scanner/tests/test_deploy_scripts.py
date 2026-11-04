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
        "Nice=10",
        "IOSchedulingClass=idle",
        "--memory 1g",
        "--cpus 2",
        "--read-only",
        "--tmpfs /run/sdrscan:size=64m",
    ):
        assert setting in unit, setting


def test_template_config_loads():
    from sdrscan.config import load_config

    cfg = load_config(DEPLOY / "config.template.yaml")
    assert cfg.uplink.target == "http_put" and cfg.live.enabled is False


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
