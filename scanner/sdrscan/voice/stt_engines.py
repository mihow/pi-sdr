"""In-process speech-to-text engines: sherpa-onnx (Parakeet, Moonshine) and faster-whisper.

Each class imports its package when it is built, so a host without the package can still
import sdrscan; ``stt.engine_problem`` reports the engine as unavailable instead. Models
are local directories and are never downloaded. Building an engine loads the model (about
1 GB resident for Parakeet), so the service builds one engine and keeps it. The engine's
worker threads inherit the nice value of the thread that builds it.

The confidence is the geometric mean of the token probabilities (sherpa-onnx) or of the
segment probabilities (faster-whisper). Text that ``is_hallucination`` rejects gets 0.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .. import AUDIO_RATE
from .stt import Transcript, is_hallucination, prepare_audio

PARAKEET_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
MOONSHINE_FILES = (
    "preprocess.onnx",
    "encode.int8.onnx",
    "uncached_decode.int8.onnx",
    "cached_decode.int8.onnx",
    "tokens.txt",
)
# The files faster-whisper needs in a CTranslate2 model directory. It also reads
# tokenizer.json and vocabulary.txt or .json (older and newer conversions) when present.
FASTER_WHISPER_FILES = ("model.bin", "config.json")
FASTER_WHISPER_OPTIONAL = (
    "tokenizer.json",
    "vocabulary.txt",
    "vocabulary.json",
    "preprocessor_config.json",
)


def _files(model_dir: Path, names: tuple[str, ...]) -> list[Path]:
    paths = [Path(model_dir) / n for n in names]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"{model_dir}: missing {', '.join(missing)}")
    return paths


def _transcript(text: str, conf: float, engine: str) -> Transcript:
    text = text.strip()
    if is_hallucination(text):
        conf = 0.0
    return Transcript(text=text, confidence=round(conf, 3), engine=engine)


class _SherpaOffline:
    engine = "sherpa-onnx"
    # Parakeet read the clip better without the 300 to 3000 Hz band-pass (WER 0.17 vs 0.19).
    bandpass = False

    def __init__(self, recognizer, files: list[Path]) -> None:
        self._rec = recognizer
        self._files = files

    def model_files(self) -> list[Path]:
        return list(self._files)

    def transcribe(self, pcm: np.ndarray) -> Transcript | None:
        if not len(pcm):
            return None
        stream = self._rec.create_stream()
        stream.accept_waveform(AUDIO_RATE, prepare_audio(pcm, self.bandpass).astype(np.float32))
        self._rec.decode_stream(stream)
        res = stream.result
        logp = list(getattr(res, "ys_log_probs", []) or [])
        conf = math.exp(float(np.mean(logp))) if logp else (0.5 if res.text.strip() else 0.0)
        return _transcript(res.text, conf, self.engine)


class SherpaParakeetStt(_SherpaOffline):
    """NVIDIA Parakeet TDT 0.6B v2 (English), int8, as a sherpa-onnx offline transducer."""

    engine = "parakeet"

    def __init__(self, model_dir: Path, threads: int = 2) -> None:
        import sherpa_onnx

        enc, dec, join, tokens = _files(model_dir, PARAKEET_FILES)
        rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(enc),
            decoder=str(dec),
            joiner=str(join),
            tokens=str(tokens),
            num_threads=threads,
            model_type="nemo_transducer",
        )
        super().__init__(rec, [enc, dec, join, tokens])


class SherpaMoonshineStt(_SherpaOffline):
    """Useful Sensors Moonshine (tiny or base, English), int8, through sherpa-onnx."""

    engine = "moonshine"

    def __init__(self, model_dir: Path, threads: int = 2) -> None:
        import sherpa_onnx

        pre, enc, unc, cac, tokens = _files(model_dir, MOONSHINE_FILES)
        rec = sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=str(pre),
            encoder=str(enc),
            uncached_decoder=str(unc),
            cached_decoder=str(cac),
            tokens=str(tokens),
            num_threads=threads,
        )
        super().__init__(rec, [pre, enc, unc, cac, tokens])


class FasterWhisperStt:
    """A Whisper model converted to CTranslate2, int8 on the CPU (faster-whisper)."""

    engine = "faster-whisper"

    def __init__(self, model_dir: Path, threads: int = 2, beam_size: int = 5) -> None:
        from faster_whisper import WhisperModel

        model_dir = Path(model_dir)
        self._files = _files(model_dir, FASTER_WHISPER_FILES)
        self._files += [model_dir / n for n in FASTER_WHISPER_OPTIONAL if (model_dir / n).exists()]
        self.beam_size = beam_size
        self._model = WhisperModel(
            str(model_dir),
            device="cpu",
            compute_type="int8",
            cpu_threads=threads,
            num_workers=1,
            local_files_only=True,
        )

    def model_files(self) -> list[Path]:
        return list(self._files)

    def transcribe(self, pcm: np.ndarray) -> Transcript | None:
        if not len(pcm):
            return None
        segments, _info = self._model.transcribe(
            prepare_audio(pcm).astype(np.float32),
            language="en",
            beam_size=self.beam_size,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
        )
        segments = list(segments)
        text = " ".join(s.text.strip() for s in segments)
        conf = math.exp(float(np.mean([s.avg_logprob for s in segments]))) if segments else 0.0
        return _transcript(text, conf, self.engine)
