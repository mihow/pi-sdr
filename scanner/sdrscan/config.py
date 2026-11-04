"""Load the scanner configuration into frozen dataclasses with defaults and validation.

Every field has a default, so an empty or missing file gives a working configuration.
Unknown keys are errors, because a misspelt key that silently keeps its default is the
hardest kind of configuration bug to see on a remote rig. Secrets never live in this file:
they come from the env file named by ``uplink.secrets_env`` (spec section 12).
"""

from __future__ import annotations

import dataclasses
import re
import typing
from pathlib import Path

import yaml

DATA_DIR = Path(__file__).parent / "data"
EXAMPLE_CONFIG = DATA_DIR / "config.example.yaml"
DEFAULT_BANDPLAN = DATA_DIR / "bandplan_us.yaml"

_SECRET_KEY = re.compile(r"(token|password|passwd|secret|api_?key|credential)", re.I)
_SECRET_ALLOWED = {"secrets_env"}


class ConfigError(ValueError):
    """The configuration file is invalid. The message names the offending key."""


@dataclasses.dataclass(frozen=True)
class DutyConfig:
    survey_min_share: float = 0.55
    dwell_max_share: float = 0.40
    calibrate_share: float = 0.05
    survey_hop_s: float = 0.1
    dwell_max_s: float = 60.0
    dwell_cooldown_s: float = 30.0
    dwell_block_s: float = 0.1
    tier_a_revisit_s: float = 15.0
    critical_hop_interval_s: float = 1.0


@dataclasses.dataclass(frozen=True)
class DetectConfig:
    threshold_db: float = 10.0
    fm_threshold_db: float = 15.0
    nfft: int = 4096
    navg: int = 50
    crop_frac: float = 0.10
    dc_mask_bins: int = 3
    tune_offset_hz: int = 150_000
    min_width_hz: float = 500.0  # one 586 Hz bin passes; averaged noise rarely spikes 10 dB
    clip_level: float = 0.99
    dwell_open_snr_db: float = 20.0
    hang_s: float = 2.0
    always_on_duty: float = 0.80


@dataclasses.dataclass(frozen=True)
class QuotaConfig:
    clip_quota_bytes: int = 2 * 1024**3
    per_channel_share: float = 0.10
    free_evict_frac: float = 0.15
    free_prune_frac: float = 0.12
    free_pinned_frac: float = 0.10
    free_stop_frac: float = 0.05
    raw_retention_days: int = 90
    min_clip_s: float = 1.5


@dataclasses.dataclass(frozen=True)
class ThermalConfig:
    hot_c: float = 70.0
    very_hot_c: float = 78.0
    critical_c: float = 82.0
    hysteresis_c: float = 3.0
    sample_interval_s: float = 30.0


@dataclasses.dataclass(frozen=True)
class UplinkConfig:
    target: str = "file"
    url: str | None = None
    path: Path | None = None
    secrets_env: Path | None = None
    cap_bytes: int = 30 * 1_000_000
    interval_s: float = 3600.0
    slow_interval_s: float = 3 * 3600.0
    slow_frac: float = 0.80
    tls_overhead_bytes: int = 6_000
    outbox_keep: int = 48
    include_transcripts: bool = False


@dataclasses.dataclass(frozen=True)
class LiveConfig:
    enabled: bool = False
    bind_address: str | None = None
    port: int = 8073


@dataclasses.dataclass(frozen=True)
class Config:
    site_id: str = "site"
    data_root: Path = Path("/var/lib/sdrscan")
    stage_root: Path = Path("/run/sdrscan")
    bandplan_path: Path = DEFAULT_BANDPLAN
    labels_path: Path | None = None
    duty: DutyConfig = DutyConfig()
    detect: DetectConfig = DetectConfig()
    quota: QuotaConfig = QuotaConfig()
    thermal: ThermalConfig = ThermalConfig()
    uplink: UplinkConfig = UplinkConfig()
    live: LiveConfig = LiveConfig()


def load_config(path: Path | None = None) -> Config:
    """Read ``path`` (YAML) over the defaults. ``None`` returns the defaults."""
    raw: dict = {}
    if path is not None:
        text = Path(path).read_text()
        loaded = yaml.safe_load(text)
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        raw = loaded
    _refuse_secrets(raw, prefix="")
    cfg = _build(Config, raw, prefix="")
    _validate(cfg)
    return cfg


def _refuse_secrets(raw: dict, prefix: str) -> None:
    for key, value in raw.items():
        name = f"{prefix}{key}"
        if key not in _SECRET_ALLOWED and _SECRET_KEY.search(str(key)):
            raise ConfigError(
                f"{name}: secrets are not allowed in the config file; "
                "put them in the env file named by uplink.secrets_env"
            )
        if isinstance(value, dict):
            _refuse_secrets(value, prefix=f"{name}.")


def _build(cls: type, raw: dict, prefix: str):
    hints = typing.get_type_hints(cls)
    fields = {f.name for f in dataclasses.fields(cls)}
    kwargs = {}
    for key, value in raw.items():
        name = f"{prefix}{key}"
        if key not in fields:
            raise ConfigError(f"{name}: unknown key")
        kwargs[key] = _coerce(hints[key], value, name)
    return cls(**kwargs)


def _coerce(hint, value, name: str):
    if dataclasses.is_dataclass(hint):
        if not isinstance(value, dict):
            raise ConfigError(f"{name}: expected a mapping")
        return _build(hint, value, prefix=f"{name}.")
    args = typing.get_args(hint)
    optional = type(None) in args
    if optional:
        if value is None:
            return None
        hint = next(a for a in args if a is not type(None))
    if hint is Path:
        if not isinstance(value, str):
            raise ConfigError(f"{name}: expected a path string")
        return Path(value)
    if hint is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{name}: expected true or false")
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{name}: expected an integer")
        return value
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{name}: expected a number")
        return float(value)
    if hint is str:
        if not isinstance(value, str):
            raise ConfigError(f"{name}: expected a string")
        return value
    raise ConfigError(f"{name}: unsupported type {hint!r}")  # pragma: no cover


def _validate(cfg: Config) -> None:
    d = cfg.duty
    if d.dwell_max_share + d.calibrate_share + d.survey_min_share > 1.0 + 1e-9:
        raise ConfigError("duty: survey_min_share + dwell_max_share + calibrate_share exceed 1")
    for name in ("survey_min_share", "dwell_max_share", "calibrate_share"):
        if not 0.0 <= getattr(d, name) <= 1.0:
            raise ConfigError(f"duty.{name}: share must be between 0 and 1")
    if cfg.uplink.target not in {"file", "http_put", "http_post"}:
        raise ConfigError("uplink.target: must be file, http_put or http_post")
    if cfg.quota.clip_quota_bytes < 0:
        raise ConfigError("quota.clip_quota_bytes: must not be negative")
