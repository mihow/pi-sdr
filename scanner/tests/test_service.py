"""Service-level behaviour on a virtual clock: start-up with the shipped configuration,
catch-up after downtime, and state that must survive a restart."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from sdrscan.config import load_config
from sdrscan.iq.scene import Scene
from sdrscan.iq.synthetic import SyntheticSource
from sdrscan.service import Service

from .test_clips import FakeFs

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _template_cfg(tmp_path: Path, uplink_env: str | None):
    """The shipped template, with the rig paths moved under ``tmp_path``."""
    cfg = load_config(DEPLOY / "config.template.yaml")
    env = tmp_path / "uplink.env"
    if uplink_env is not None:
        env.write_text(uplink_env)
    return dataclasses.replace(
        cfg,
        data_root=tmp_path / "data",
        stage_root=tmp_path / "stage",
        uplink=dataclasses.replace(cfg.uplink, secrets_env=env),
    )


def _service(cfg, vclock, sys_root, **kw) -> Service:
    src = SyntheticSource(Scene.from_yaml("emitters: []\n", vclock), vclock)
    return Service(cfg, vclock, src, spectrum="psd", statvfs=FakeFs(0.5), sys_root=sys_root, **kw)


@pytest.mark.parametrize(
    "uplink_env",
    [None, "", "# SDRSCAN_S3_BUCKET=<bucket>\n", "SDRSCAN_S3_BUCKET=b\n"],
    ids=["missing", "empty", "placeholder", "incomplete"],
)
def test_an_unconfigured_uplink_disables_pushes_but_the_scanner_runs(
    tmp_path, vclock, sys_root, uplink_env
):
    """A fresh install has no upload credentials yet. The scanner must still scan, write
    the local digest and serve the live page, and say plainly that the uplink is off."""
    vclock.set(vclock.now() + 3500)  # 100 s before an hour boundary, so a digest is due
    svc = _service(_template_cfg(tmp_path, uplink_env), vclock, sys_root)
    assert svc.publisher is None and svc.uplink_error
    svc.run(until=vclock.now() + 200)
    assert svc.digests >= 1
    assert (tmp_path / "data" / "www" / "digest.json.gz").exists()
    assert svc.last_digest["health"]["uplink"]["state"] == "unconfigured"
    assert "uplink_unconfigured" in svc.last_digest["health"]["flags"]
    svc.shutdown()
