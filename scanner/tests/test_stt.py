from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from sdrscan.config import SttConfig
from sdrscan.voice.stt import FakeStt, NullStt, Transcript, WhisperCppStt, make_stt

from .audio import voice

# A trimmed real shape of whisper-cli -ojf output (token probabilities in "p").
WHISPER_JSON = {
    "transcription": [
        {
            "offsets": {"from": 0, "to": 2100},
            "text": " Engine two responding.",
            "tokens": [
                {"text": " Engine", "p": 0.91},
                {"text": " two", "p": 0.83},
                {"text": " responding", "p": 0.77},
                {"text": ".", "p": 0.95},
                {"text": "[_TT_105]", "p": 0.40},
            ],
        }
    ]
}


class _FakeRun:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.argv: list[str] | None = None

    def __call__(self, argv, **kw):
        self.argv = list(argv)
        out = Path(argv[argv.index("-of") + 1] + ".json")
        out.write_text(json.dumps(self.payload))
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def test_whisper_builds_argv_and_parses_json(tmp_path):
    run = _FakeRun(WHISPER_JSON)
    stt = WhisperCppStt(
        Path("/opt/whisper-cli"), Path("/opt/ggml-tiny.en.bin"), work_dir=tmp_path, run=run
    )
    tr = stt.transcribe(voice(2.0))
    argv = run.argv
    assert argv[0] == "/opt/whisper-cli"
    assert argv[argv.index("-m") + 1] == "/opt/ggml-tiny.en.bin"
    assert argv[argv.index("-t") + 1] == "2"
    assert int(argv[argv.index("-ac") + 1]) < 1500  # short clip, smaller audio context
    assert "-nt" in argv and "-ojf" in argv and "-f" in argv
    assert isinstance(tr, Transcript)
    assert tr.text == "Engine two responding."
    assert tr.confidence == pytest.approx(np.mean([0.91, 0.83, 0.77, 0.95]), abs=0.01)
    assert tr.engine == "whisper.cpp"
    assert not list(tmp_path.iterdir()), "temporary wav and json are removed"


@pytest.mark.parametrize("text", [" Thank you.", " Thanks for watching!", " [BLANK_AUDIO]"])
def test_whisper_hallucinations_get_zero_confidence(tmp_path, text):
    payload = {"transcription": [{"text": text, "tokens": [{"text": text, "p": 0.99}]}]}
    stt = WhisperCppStt(Path("w"), Path("m"), work_dir=tmp_path, run=_FakeRun(payload))
    assert stt.transcribe(voice(1.0)).confidence == 0.0


def test_fake_and_null():
    assert NullStt().transcribe(voice(0.5)) is None
    fake = FakeStt(["alpha", "bravo"])
    assert [fake.transcribe(voice(0.5)).text for _ in range(3)] == ["alpha", "bravo", "alpha"]


def test_make_stt_respects_config_and_mode(tmp_path):
    assert isinstance(make_stt(SttConfig(), "normal"), NullStt)
    cfg = SttConfig(engine="whisper_cpp", binary=Path("w"), model=Path("m"))
    assert isinstance(make_stt(cfg, "normal", work_dir=tmp_path), WhisperCppStt)
    for mode in ("hot", "very_hot", "critical"):
        assert isinstance(make_stt(cfg, mode), NullStt)
    assert isinstance(make_stt(SttConfig(engine="fake"), "normal"), FakeStt)


@pytest.mark.skipif(not os.environ.get("SDRSCAN_WHISPER_BIN"), reason="SDRSCAN_WHISPER_BIN unset")
def test_live_whisper_binary_runs(tmp_path):  # pragma: no cover - needs a local build
    stt = WhisperCppStt(
        Path(os.environ["SDRSCAN_WHISPER_BIN"]),
        Path(os.environ["SDRSCAN_WHISPER_MODEL"]),
        work_dir=tmp_path,
    )
    assert stt.transcribe(voice(2.0)) is not None
