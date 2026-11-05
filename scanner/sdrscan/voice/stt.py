"""Speech-to-text adapters. Text is the most compact form of a voice clip.

Every engine takes 16 kHz mono float32 audio and returns a ``Transcript`` or ``None``.
``NullStt`` gives no text and ``FakeStt`` scripted text for tests. The real engines are
``WhisperCppStt`` (a subprocess wrapper around ``whisper-cli``) and, in
``voice/stt_engines.py``, sherpa-onnx (Parakeet, Moonshine) and faster-whisper. Each real
engine is optional: a missing Python package or model file makes it unavailable, not a
crash. ``stt.engine: auto`` runs the engine that ``sdrscan stt-bench`` chose on this host
(``voice/stt_bench.py``). STT is off by default and never runs in a hot governor mode.

Short-clip hallucinations ("thank you", "thanks for watching") and text made only of sound
tags ("[applause]", "(music)", "*beep*") are kept with confidence 0, so they are stored but
never shown as fact.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import logging
import math
import platform
import re
import subprocess
import tempfile
import typing
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt

from .. import AUDIO_RATE
from ..config import STT_ENGINES, SttConfig

log = logging.getLogger(__name__)

HALLUCINATIONS = (
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "you",
    "[blank_audio]",
    "(silence)",
)
# A sound tag: [applause], [BLANK_AUDIO], (music), (wind blowing), *beep*, and the music
# notes whisper prints. Text made only of these is no speech.
_SOUND_TAG = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|[\u266a\u266b\u266c]")
_TOKEN_SPECIAL = re.compile(r"^\[_[A-Z]+_?\d*\]$|^\[_|^<\|")
_HOT_MODES = {"hot", "very_hot", "critical"}
_BANDPASS = butter(4, [300, 3000], btype="bandpass", fs=AUDIO_RATE, output="sos")


@dataclasses.dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float
    engine: str
    rtf: float | None = None


class SttEngine(typing.Protocol):
    def transcribe(self, pcm: np.ndarray) -> Transcript | None: ...


class NullStt:
    def transcribe(self, pcm: np.ndarray) -> None:
        return None


class FakeStt:
    """Returns scripted lines in turn. For tests and simulations only."""

    def __init__(self, lines: list[str] | None = None, confidence: float = 0.9) -> None:
        self.lines = lines or ["unit two on scene", "copy that, en route"]
        self.confidence = confidence
        self._i = 0

    def transcribe(self, pcm: np.ndarray) -> Transcript:
        text = self.lines[self._i % len(self.lines)]
        self._i += 1
        return Transcript(text=text, confidence=self.confidence, engine="fake", rtf=0.0)


def is_hallucination(text: str) -> bool:
    """True when the text is no speech: empty, only sound tags, or a known filler line."""
    t = _SOUND_TAG.sub(" ", text.lower())
    t = re.sub(r"[^\w\s]", "", t)
    t = " ".join(t.split())
    return not t or t in HALLUCINATIONS


def prepare_audio(pcm: np.ndarray, bandpass: bool = True) -> np.ndarray:
    """The audio an engine reads: optionally band-passed 300 to 3000 Hz, peak-normalised.

    On the corpus bench the band-pass helped whisper.cpp base.en (WER 0.28 to 0.25) and
    hurt Parakeet (0.17 to 0.19), so each engine sets it (spec section 5.9).
    """
    audio = np.asarray(pcm, dtype=np.float64)
    if bandpass:
        audio = sosfilt(_BANDPASS, audio)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak > 0:
        audio = audio * (0.9 / peak)
    return audio


def write_wav(path: Path, pcm: np.ndarray, rate: int = AUDIO_RATE) -> None:
    pcm16 = (np.clip(np.asarray(pcm, dtype=np.float64), -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16.tobytes())


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """A 16-bit mono WAV as float32 in [-1, 1], and its sample rate."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{Path(path).name}: expected 16-bit mono WAV")
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return (pcm.astype(np.float32) / 32768.0), rate


class WhisperCppStt:
    """``whisper-cli -m MODEL -t N [-ac CTX] [-sns] -nt -ojf -of BASE -f clip.wav``.

    ``-ojf`` (full JSON) carries token probabilities; their mean is the confidence.
    ``-ac`` is the audio context, sized to the clip but at least ``audio_ctx`` frames
    (1500 frames is 30 s; 0 keeps the full context). A context sized tightly to a short
    clip makes base.en repeat itself; a floor of 1024 frames had the lowest WER on the
    corpus bench and runs 40% faster than the full context (spec section 5.9). Audio is
    band-passed first.
    """

    def __init__(
        self,
        binary: Path,
        model: Path,
        threads: int = 2,
        work_dir: Path | None = None,
        run: typing.Callable[..., subprocess.CompletedProcess] = subprocess.run,
        timeout_s: float = 120.0,
        audio_ctx: int = 1024,
        suppress_nst: bool = False,
    ) -> None:
        self.audio_ctx = audio_ctx
        self.suppress_nst = suppress_nst
        self.binary = Path(binary)
        self.model = Path(model)
        self.threads = threads
        self.work_dir = Path(work_dir) if work_dir is not None else None
        self._run = run
        self.timeout_s = timeout_s

    def model_files(self) -> list[Path]:
        return [self.model]

    def argv(self, wav: Path, base: Path, seconds: float) -> list[str]:
        argv = [str(self.binary), "-m", str(self.model), "-t", str(self.threads)]
        if self.audio_ctx > 0:
            ctx = min(1500, max(self.audio_ctx, int(math.ceil(seconds * 50)) + 64))
            argv += ["-ac", str(ctx)]
        if self.suppress_nst:
            argv.append("-sns")
        return argv + [
            "-nt",
            "-ojf",
            "-of",
            str(base),
            "-f",
            str(wav),
        ]

    def transcribe(self, pcm: np.ndarray) -> Transcript | None:
        audio = prepare_audio(pcm)
        seconds = len(audio) / AUDIO_RATE
        with tempfile.TemporaryDirectory(dir=self.work_dir) as tmp:
            wav = Path(tmp) / "clip.wav"
            base = Path(tmp) / "out"
            write_wav(wav, audio)
            proc = self._run(
                self.argv(wav, base, seconds), capture_output=True, timeout=self.timeout_s
            )
            if proc.returncode != 0:
                return None
            out = Path(str(base) + ".json")
            if not out.exists():
                return None
            # whisper-cli can split a multi-byte character across tokens, which leaves
            # invalid UTF-8 in its JSON; the broken character is replaced, not fatal.
            payload = json.loads(out.read_bytes().decode("utf-8", errors="replace"))
        return self._parse(payload)

    def _parse(self, payload: dict) -> Transcript | None:
        segs = payload.get("transcription") or []
        text = " ".join(s.get("text", "").strip() for s in segs).strip()
        probs = [
            float(tok["p"])
            for s in segs
            for tok in s.get("tokens", [])
            if "p" in tok and not _TOKEN_SPECIAL.match(tok.get("text", "").strip())
        ]
        conf = float(np.mean(probs)) if probs else 0.5
        if is_hallucination(text):
            conf = 0.0
        return Transcript(text=text, confidence=round(conf, 3), engine="whisper.cpp")


# -- engine registry ----------------------------------------------------------------------
# The Python package each engine imports (whisper.cpp runs as a subprocess), and the
# SttConfig fields that configure it. stt-bench records these fields in its choice file.
_PACKAGES = {
    "whisper_cpp": None,
    "sherpa_parakeet": "sherpa_onnx",
    "sherpa_moonshine": "sherpa_onnx",
    "faster_whisper": "faster_whisper",
}
ENGINE_FIELDS = {
    "whisper_cpp": ("whisper_binary", "whisper_model", "whisper_audio_ctx", "whisper_suppress_nst"),
    "sherpa_parakeet": ("parakeet_model",),
    "sherpa_moonshine": ("moonshine_model",),
    "faster_whisper": ("faster_whisper_model", "faster_whisper_beam"),
}
MODEL_FIELD = {
    "whisper_cpp": "whisper_model",
    "sherpa_parakeet": "parakeet_model",
    "sherpa_moonshine": "moonshine_model",
    "faster_whisper": "faster_whisper_model",
}
CHOICE_SCHEMA = "sdrscan.stt_choice/1"


def engine_problem(cfg: SttConfig, name: str) -> str | None:
    """Why engine ``name`` cannot run with ``cfg`` on this host, or None when it can."""
    if name not in STT_ENGINES:
        return f"unknown engine {name!r}"
    package = _PACKAGES[name]
    if package is not None and importlib.util.find_spec(package) is None:
        return f"Python package {package} is not installed"
    model = getattr(cfg, MODEL_FIELD[name])
    if model is None:
        return f"stt.{MODEL_FIELD[name]} is not set"
    if not Path(model).exists():
        return f"model {model} does not exist"
    if name == "whisper_cpp":
        if cfg.whisper_binary is None:
            return "stt.whisper_binary is not set"
        if not Path(cfg.whisper_binary).exists():
            return f"binary {cfg.whisper_binary} does not exist"
    return None


def build_engine(cfg: SttConfig, name: str, work_dir: Path | None = None) -> SttEngine:
    """The concrete engine ``name``. Loads the model, so call it once and keep the result."""
    problem = engine_problem(cfg, name)
    if problem is not None:
        raise ValueError(f"{name}: {problem}")
    if name == "whisper_cpp":
        return WhisperCppStt(
            cfg.whisper_binary,
            cfg.whisper_model,
            threads=cfg.threads,
            work_dir=work_dir,
            audio_ctx=cfg.whisper_audio_ctx,
            suppress_nst=cfg.whisper_suppress_nst,
        )
    from . import stt_engines

    if name == "sherpa_parakeet":
        return stt_engines.SherpaParakeetStt(cfg.parakeet_model, threads=cfg.threads)
    if name == "sherpa_moonshine":
        return stt_engines.SherpaMoonshineStt(cfg.moonshine_model, threads=cfg.threads)
    return stt_engines.FasterWhisperStt(
        cfg.faster_whisper_model, threads=cfg.threads, beam_size=cfg.faster_whisper_beam
    )


@dataclasses.dataclass(frozen=True)
class SttSelection:
    """Which engine runs, with the settings it runs with, and why.

    ``source`` is ``config`` (an explicit engine name), ``choice`` (the stt-bench choice
    file), ``fallback`` (auto without a valid choice) or ``off``. ``flags`` go into health.
    """

    engine: str
    cfg: SttConfig
    source: str
    flags: tuple[str, ...] = ()
    detail: str = ""


def read_choice(path: Path) -> tuple[dict | None, str]:
    """The choice file's content when it is valid for this host, else None and the reason."""
    try:
        choice = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None, f"no choice file at {path}"
    except (OSError, ValueError) as exc:
        return None, f"choice file {path} is unreadable: {exc}"
    if not isinstance(choice, dict) or choice.get("schema") != CHOICE_SCHEMA:
        return None, f"choice file {path} is not {CHOICE_SCHEMA}"
    engine = choice.get("engine")
    if engine != "none" and engine not in STT_ENGINES:
        return None, f"choice file names unknown engine {engine!r}"
    machine = (choice.get("host") or {}).get("machine")
    if machine != platform.machine():
        return None, f"choice was measured on {machine}, this host is {platform.machine()}"
    return choice, ""


def _apply_settings(cfg: SttConfig, engine: str, settings: dict) -> SttConfig:
    fields = {f.name: f for f in dataclasses.fields(SttConfig)}
    kw = {}
    for key in ENGINE_FIELDS[engine]:
        if key in settings:
            value = settings[key]
            if value is not None and "Path" in str(fields[key].type):
                value = Path(value)
            kw[key] = value
    return dataclasses.replace(cfg, engine=engine, **kw)


def select_stt(cfg: SttConfig, choice_path: Path | None = None) -> SttSelection:
    """Resolve ``stt.engine`` to the concrete engine that will run on this host."""
    if cfg.engine in ("none", "fake"):
        return SttSelection(cfg.engine, cfg, "off" if cfg.engine == "none" else "config")
    if cfg.engine != "auto":
        problem = engine_problem(cfg, cfg.engine)
        if problem is None:
            return SttSelection(cfg.engine, cfg, "config")
        return SttSelection("none", cfg, "config", ("stt_unavailable",), problem)
    path = cfg.choice_path or choice_path
    reason = "no choice file configured"
    if path is not None:
        choice, reason = read_choice(path)
        if choice is not None and choice["engine"] == "none":
            return SttSelection(
                "none", cfg, "choice", ("stt_no_engine_fits",), "stt-bench found no engine"
            )
        if choice is not None:
            chosen = _apply_settings(cfg, choice["engine"], choice.get("settings") or {})
            problem = engine_problem(chosen, choice["engine"])
            if problem is None:
                return SttSelection(choice["engine"], chosen, "choice")
            reason = f"chosen engine {choice['engine']} cannot run: {problem}"
    for name in filter(None, (n.strip() for n in cfg.fallback.split(","))):
        if engine_problem(cfg, name) is None:
            return SttSelection(
                name,
                dataclasses.replace(cfg, engine=name),
                "fallback",
                ("stt_auto_fallback",),
                reason,
            )
    return SttSelection(
        "none",
        cfg,
        "fallback",
        ("stt_auto_fallback", "stt_unavailable"),
        f"{reason}; no fallback engine is available",
    )


def make_stt(
    cfg: SttConfig,
    mode: str,
    work_dir: Path | None = None,
    choice_path: Path | None = None,
    selection: SttSelection | None = None,
) -> SttEngine:
    """The configured engine, or ``NullStt`` when STT is off, unavailable or the rig is hot.

    This loads the model. The service calls it once and keeps the engine.
    """
    if mode in _HOT_MODES or cfg.engine == "none":
        return NullStt()
    sel = selection or select_stt(cfg, choice_path)
    if sel.engine == "fake":
        return FakeStt()
    if sel.engine == "none":
        if sel.detail:
            log.warning("speech-to-text off: %s", sel.detail)
        return NullStt()
    return build_engine(sel.cfg, sel.engine, work_dir=work_dir)
