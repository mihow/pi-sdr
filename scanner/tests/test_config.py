from __future__ import annotations

from pathlib import Path

import pytest

from sdrscan.config import EXAMPLE_CONFIG, ConfigError, load_config


def test_example_config_loads_with_spec_defaults():
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.quota.clip_quota_bytes == 2 * 1024**3
    assert cfg.uplink.cap_bytes == 30 * 1_000_000
    assert cfg.duty.dwell_max_share == pytest.approx(0.40)
    assert cfg.detect.threshold_db == pytest.approx(10.0)
    assert cfg.detect.fm_threshold_db == pytest.approx(15.0)
    assert cfg.bandplan_path.name == "bandplan_us.yaml"
    assert cfg.labels_path is None


def test_none_path_gives_defaults_equal_to_example():
    assert load_config(None) == load_config(EXAMPLE_CONFIG)


def test_unknown_key_raises_naming_the_key(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("duty:\n  dwell_max_shar: 0.3\n")
    with pytest.raises(ConfigError, match="duty.dwell_max_shar"):
        load_config(p)


def test_unknown_top_level_key_raises(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("frobnicate: 1\n")
    with pytest.raises(ConfigError, match="frobnicate"):
        load_config(p)


@pytest.mark.parametrize("key", ["token", "password", "secret", "api_key"])
def test_secret_looking_key_is_refused(tmp_path: Path, key: str):
    p = tmp_path / "c.yaml"
    p.write_text(f"uplink:\n  {key}: hunter2\n")
    with pytest.raises(ConfigError, match="secrets_env"):
        load_config(p)


def test_secrets_env_path_itself_is_allowed(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("uplink:\n  secrets_env: /etc/sdrscan/uplink.env\n")
    cfg = load_config(p)
    assert cfg.uplink.secrets_env == Path("/etc/sdrscan/uplink.env")


def test_override_merges_over_defaults(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("site_id: test-site\nquota:\n  clip_quota_bytes: 1000\n")
    cfg = load_config(p)
    assert cfg.site_id == "test-site"
    assert cfg.quota.clip_quota_bytes == 1000
    assert cfg.quota.per_channel_share == pytest.approx(0.10)


def test_wrong_type_raises(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("detect:\n  threshold_db: loud\n")
    with pytest.raises(ConfigError, match="detect.threshold_db"):
        load_config(p)


def test_shares_must_fit_in_one(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("duty:\n  dwell_max_share: 0.9\n")
    with pytest.raises(ConfigError, match="share"):
        load_config(p)
