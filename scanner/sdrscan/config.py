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
    # Floor for dwells when one tier A sweep alone fills the revisit target (slow hops):
    # the sweep period stretches so that at least this share stays free for dwells.
    dwell_min_share: float = 0.15
    calibrate_share: float = 0.05
    calibrate_interval_s: float = 300.0
    calibrate_s: float = 1.0
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
    # A dwell transmission with no voice or data at most this long is a key-up. A repeater
    # holds its carrier 1 to 3 s after the user unkeys, so this allows for that tail.
    keyup_max_s: float = 5.0
    always_on_duty: float = 0.80


@dataclasses.dataclass(frozen=True)
class QuotaConfig:
    clip_quota_bytes: int = 2 * 1024**3
    per_channel_share: float = 0.10
    free_evict_frac: float = 0.15
    free_prune_frac: float = 0.12
    free_pinned_frac: float = 0.10
    free_stop_frac: float = 0.05
    # Absolute floors in bytes. When non-zero they replace the matching fraction, because
    # a percentage misleads on a small card (12 % of 15 GB is 1.8 GB). 0 keeps the fraction.
    free_evict_bytes: int = 0
    free_pinned_bytes: int = 0
    free_stop_bytes: int = 0
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


# Concrete speech-to-text engines (voice/stt.py) and the names the config also accepts.
STT_ENGINES = ("whisper_cpp", "sherpa_parakeet", "sherpa_moonshine", "faster_whisper")
STT_NAMES = ("none", "auto", "fake", *STT_ENGINES)


@dataclasses.dataclass(frozen=True)
class SttConfig:
    # none | auto | whisper_cpp | sherpa_parakeet | sherpa_moonshine | faster_whisper | fake.
    # auto runs the engine that `sdrscan stt-bench` chose on this host (choice_path), and
    # the first available engine of `fallback` when there is no valid choice.
    engine: str = "none"
    threads: int = 2
    min_confidence: float = 0.6
    queue_max: int = 50
    # Models are local files; nothing is downloaded at run time.
    whisper_binary: Path | None = None  # whisper-cli
    whisper_model: Path | None = None  # a ggml model file
    # -ac sized to the clip but at least this many frames (1500 = 30 s); 0 = full context.
    # 1024 had the lowest WER on the corpus bench (spec section 5.9).
    whisper_audio_ctx: int = 1024
    whisper_suppress_nst: bool = False  # whisper-cli -sns
    parakeet_model: Path | None = None  # sherpa-onnx Parakeet TDT directory
    moonshine_model: Path | None = None  # sherpa-onnx Moonshine directory
    faster_whisper_model: Path | None = None  # CTranslate2 model directory
    faster_whisper_beam: int = 5
    # engine: auto. The default choice file is <data_root>/stt-choice.json.
    choice_path: Path | None = None
    fallback: str = "sherpa_parakeet,faster_whisper,whisper_cpp"
    # The stt-bench selection limits (spec section 5.9).
    max_rtf: float = 0.5
    max_rss_mb: float = 1200.0
    max_model_mb: float = 800.0
    max_noise_shown_frac: float = 0.15


@dataclasses.dataclass(frozen=True)
class ProseConfig:
    engine: str = "none"  # none | llama_cli
    binary: Path | None = None
    model: Path | None = None
    threads: int = 2


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
    stt: SttConfig = SttConfig()
    prose: ProseConfig = ProseConfig()


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
    if not 0.0 <= d.dwell_min_share <= d.dwell_max_share:
        raise ConfigError("duty: dwell_min_share must be between 0 and dwell_max_share")
    for name in ("survey_min_share", "dwell_max_share", "calibrate_share"):
        if not 0.0 <= getattr(d, name) <= 1.0:
            raise ConfigError(f"duty.{name}: share must be between 0 and 1")
    if cfg.uplink.target not in {"file", "s3", "http_put", "http_post"}:
        raise ConfigError("uplink.target: must be file, s3, http_put or http_post")
    if cfg.stt.engine not in STT_NAMES:
        raise ConfigError(f"stt.engine: must be one of {', '.join(STT_NAMES)}")
    for name in filter(None, (n.strip() for n in cfg.stt.fallback.split(","))):
        if name not in STT_ENGINES:
            raise ConfigError(f"stt.fallback: unknown engine {name!r}")
    if cfg.stt.whisper_audio_ctx < 0 or cfg.stt.threads < 1:
        raise ConfigError("stt: whisper_audio_ctx must be >= 0 and threads >= 1")
    if cfg.prose.engine not in {"none", "llama_cli"}:
        raise ConfigError("prose.engine: must be none or llama_cli")
    if cfg.quota.clip_quota_bytes < 0:
        raise ConfigError("quota.clip_quota_bytes: must not be negative")
