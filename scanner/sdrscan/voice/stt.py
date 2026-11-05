"""Speech-to-text adapters. Text is the most compact form of a voice clip.

Every engine takes 16 kHz mono float32 audio and returns a ``Transcript`` or ``None``. The
MVP ships ``NullStt`` (no text), ``FakeStt`` (scripted text for tests) and
``WhisperCppStt``, a subprocess wrapper around ``whisper-cli`` from whisper.cpp. The real
engine is off by default and never runs in a hot governor mode. Short-clip hallucinations
("thank you", "thanks for watching") are kept with confidence 0, so they are stored but
never shown as fact.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import subprocess
import tempfile
import typing
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt

from .. import AUDIO_RATE
from ..config import SttConfig

HALLUCINATIONS = (
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "you",
    "[blank_audio]",
    "(silence)",
)
_TOKEN_SPECIAL = re.compile(r"^\[_[A-Z]+_?\d*\]$|^\[_|^<\|")
_HOT_MODES = {"hot", "very_hot", "critical"}


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
    t = re.sub(r"[^\w\s\[\]()]", "", text.strip().lower()).strip()
    return not t or t in HALLUCINATIONS


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
    """``whisper-cli -m MODEL -t N -ac CTX -nt -ojf -of BASE -f clip.wav``.

    ``-ojf`` (full JSON) carries token probabilities; their mean is the confidence.
    ``-ac`` shrinks the audio context for short clips (1500 frames is 30 s), which is the
    main speed-up for radio-length clips. Audio is band-passed to 300 to 3000 Hz first.
    """

    def __init__(
        self,
        binary: Path,
        model: Path,
        threads: int = 2,
        work_dir: Path | None = None,
        run: typing.Callable[..., subprocess.CompletedProcess] = subprocess.run,
        timeout_s: float = 120.0,
    ) -> None:
        self.binary = Path(binary)
        self.model = Path(model)
        self.threads = threads
        self.work_dir = Path(work_dir) if work_dir is not None else None
        self._run = run
        self.timeout_s = timeout_s
        self._sos = butter(4, [300, 3000], btype="bandpass", fs=AUDIO_RATE, output="sos")

    def argv(self, wav: Path, base: Path, seconds: float) -> list[str]:
        ctx = min(1500, int(math.ceil(seconds * 50)) + 64)
        return [
            str(self.binary),
            "-m",
            str(self.model),
            "-t",
            str(self.threads),
            "-ac",
            str(ctx),
            "-nt",
            "-ojf",
            "-of",
            str(base),
            "-f",
            str(wav),
        ]

    def transcribe(self, pcm: np.ndarray) -> Transcript | None:
        audio = sosfilt(self._sos, np.asarray(pcm, dtype=np.float64))
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if peak > 0:
            audio = audio * (0.9 / peak)
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
            payload = json.loads(out.read_text())
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


def make_stt(cfg: SttConfig, mode: str, work_dir: Path | None = None) -> SttEngine:
    """The configured engine, or ``NullStt`` when STT is off or the rig is running hot."""
    if mode in _HOT_MODES or cfg.engine == "none":
        return NullStt()
    if cfg.engine == "fake":
        return FakeStt()
    if cfg.binary is None or cfg.model is None:
        return NullStt()
    return WhisperCppStt(cfg.binary, cfg.model, threads=cfg.threads, work_dir=work_dir)
