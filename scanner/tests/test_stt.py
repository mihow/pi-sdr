from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from sdrscan.config import SttConfig
from sdrscan.voice.stt import (
    CHOICE_SCHEMA,
    FakeStt,
    NullStt,
    Transcript,
    WhisperCppStt,
    engine_problem,
    is_hallucination,
    make_stt,
    select_stt,
)

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
    assert argv[argv.index("-ac") + 1] == "1024"  # floored; a clip-sized context loops
    assert "-sns" not in argv
    assert "-nt" in argv and "-ojf" in argv and "-f" in argv
    assert isinstance(tr, Transcript)
    assert tr.text == "Engine two responding."
    assert tr.confidence == pytest.approx(np.mean([0.91, 0.83, 0.77, 0.95]), abs=0.01)
    assert tr.engine == "whisper.cpp"
    assert not list(tmp_path.iterdir()), "temporary wav and json are removed"


@pytest.mark.parametrize(
    ("seconds", "floor", "expected"),
    [(2.0, 0, None), (2.0, 768, 768), (20.0, 768, 1064), (60.0, 768, 1500), (2.0, 1, 164)],
)
def test_whisper_audio_context_is_off_or_floored(seconds, floor, expected):
    """-ac shrinks the context to the clip only when asked, and never below the floor."""
    stt = WhisperCppStt(Path("w"), Path("m"), audio_ctx=floor, suppress_nst=True)
    argv = stt.argv(Path("c.wav"), Path("o"), seconds)
    assert "-sns" in argv
    if expected is None:
        assert "-ac" not in argv
    else:
        assert int(argv[argv.index("-ac") + 1]) == expected


@pytest.mark.parametrize(
    ("text", "no_speech"),
    [
        ("", True),
        ("   ", True),
        (" Thank you.", True),
        (" Thanks for watching!", True),
        (" you", True),
        (" [BLANK_AUDIO]", True),
        ("[applause]", True),
        (" (music)", True),
        ("*beep*", True),
        (" [Music] (wind blowing) *static*", True),
        ("\u266a\u266a", True),
        ("(upbeat music) Thank you.", True),
        ("[ Silence ]", True),
        # Negatives: real words survive, with or without tags around them.
        ("Engine two responding.", False),
        ("[inaudible] copy that, en route", False),
        ("unit 5 (repeat) on scene", False),
        ("thank you, unit four", False),
        ("you copy?", False),
        ("Beep me when you get there", False),
    ],
)
def test_is_hallucination_table(text, no_speech):
    assert is_hallucination(text) is no_speech


@pytest.mark.parametrize(
    "text", [" Thank you.", " Thanks for watching!", " [BLANK_AUDIO]", " (music)"]
)
def test_whisper_hallucinations_get_zero_confidence(tmp_path, text):
    payload = {"transcription": [{"text": text, "tokens": [{"text": text, "p": 0.99}]}]}
    stt = WhisperCppStt(Path("w"), Path("m"), work_dir=tmp_path, run=_FakeRun(payload))
    assert stt.transcribe(voice(1.0)).confidence == 0.0


def test_whisper_json_with_broken_utf8_still_parses(tmp_path):
    """whisper-cli can emit half of a multi-byte character in a token."""

    def run(argv, **kw):
        body = b'{"transcription": [{"text": " unit two \xe2\x99", "tokens": []}]}'
        Path(argv[argv.index("-of") + 1] + ".json").write_bytes(body)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    tr = WhisperCppStt(Path("w"), Path("m"), work_dir=tmp_path, run=run).transcribe(voice(1.0))
    assert tr.text.startswith("unit two")


def test_fake_and_null():
    assert NullStt().transcribe(voice(0.5)) is None
    fake = FakeStt(["alpha", "bravo"])
    assert [fake.transcribe(voice(0.5)).text for _ in range(3)] == ["alpha", "bravo", "alpha"]


def _whisper_files(tmp_path):
    (tmp_path / "whisper-cli").write_text("")
    (tmp_path / "m.bin").write_text("")
    return {"whisper_binary": tmp_path / "whisper-cli", "whisper_model": tmp_path / "m.bin"}


def test_make_stt_respects_config_and_mode(tmp_path):
    assert isinstance(make_stt(SttConfig(), "normal"), NullStt)
    cfg = SttConfig(engine="whisper_cpp", **_whisper_files(tmp_path))
    assert isinstance(make_stt(cfg, "normal", work_dir=tmp_path), WhisperCppStt)
    for mode in ("hot", "very_hot", "critical"):
        assert isinstance(make_stt(cfg, mode), NullStt)
    assert isinstance(make_stt(SttConfig(engine="fake"), "normal"), FakeStt)


def test_explicit_engine_without_its_model_is_unavailable_not_a_crash(tmp_path):
    cfg = SttConfig(engine="whisper_cpp", whisper_binary=tmp_path / "nope", whisper_model=None)
    sel = select_stt(cfg)
    assert sel.engine == "none" and "stt_unavailable" in sel.flags
    assert "whisper_model" in sel.detail
    assert isinstance(make_stt(cfg, "normal"), NullStt)


def test_engine_problem_names_a_missing_package(tmp_path, monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda n, *a: None if n == "sherpa_onnx" else real(n, *a)
    )
    cfg = SttConfig(parakeet_model=tmp_path)
    assert "sherpa_onnx" in engine_problem(cfg, "sherpa_parakeet")
    assert "faster_whisper_model" in engine_problem(cfg, "faster_whisper")


def _choice(tmp_path, **kw) -> Path:
    import platform

    doc = {
        "schema": CHOICE_SCHEMA,
        "engine": "whisper_cpp",
        "settings": {"whisper_audio_ctx": 768},
        "host": {"machine": platform.machine()},
    } | kw
    path = tmp_path / "stt-choice.json"
    path.write_text(json.dumps(doc))
    return path


def test_auto_runs_the_benched_choice_with_its_settings(tmp_path):
    cfg = SttConfig(engine="auto", **_whisper_files(tmp_path))
    sel = select_stt(cfg, _choice(tmp_path))
    assert (sel.engine, sel.source, sel.flags) == ("whisper_cpp", "choice", ())
    assert sel.cfg.whisper_audio_ctx == 768 and sel.cfg.threads == cfg.threads
    engine = make_stt(cfg, "normal", choice_path=tmp_path / "stt-choice.json")
    assert isinstance(engine, WhisperCppStt) and engine.audio_ctx == 768


@pytest.mark.parametrize(
    "bad",
    [
        None,  # no file
        "not json",
        {"schema": "other/1"},
        {"engine": "made_up"},
        {"host": {"machine": "pdp11"}},  # measured on another kind of host
        {"engine": "sherpa_parakeet", "settings": {}},  # chosen engine has no model here
    ],
)
def test_auto_falls_back_in_order_and_flags_it(tmp_path, bad):
    path = tmp_path / "stt-choice.json"
    if isinstance(bad, dict):
        path = _choice(tmp_path, **bad)
    elif isinstance(bad, str):
        path.write_text(bad)
    cfg = SttConfig(
        engine="auto", fallback="faster_whisper,whisper_cpp", **_whisper_files(tmp_path)
    )
    sel = select_stt(cfg, path)
    assert (sel.engine, sel.source) == ("whisper_cpp", "fallback")
    assert sel.flags == ("stt_auto_fallback",) and sel.detail


def test_auto_with_nothing_available_is_off_and_flagged(tmp_path):
    sel = select_stt(SttConfig(engine="auto"), tmp_path / "missing.json")
    assert sel.engine == "none"
    assert set(sel.flags) == {"stt_auto_fallback", "stt_unavailable"}


def test_auto_respects_a_bench_that_found_no_engine(tmp_path):
    cfg = SttConfig(engine="auto", **_whisper_files(tmp_path))
    sel = select_stt(cfg, _choice(tmp_path, engine="none", settings={}))
    assert sel.engine == "none" and sel.flags == ("stt_no_engine_fits",)


@pytest.mark.skipif(not os.environ.get("SDRSCAN_WHISPER_BIN"), reason="SDRSCAN_WHISPER_BIN unset")
def test_live_whisper_binary_transcribes_real_speech(tmp_path):  # pragma: no cover - image
    """Runs the real whisper-cli and model (the arm64 test image sets these variables).
    With SDRSCAN_WHISPER_SAMPLE (whisper.cpp's jfk.wav) the words must come back."""
    import wave

    stt = WhisperCppStt(
        Path(os.environ["SDRSCAN_WHISPER_BIN"]),
        Path(os.environ["SDRSCAN_WHISPER_MODEL"]),
        work_dir=tmp_path,
        audio_ctx=int(os.environ.get("SDRSCAN_WHISPER_AUDIO_CTX", "0")),
        timeout_s=1800.0,  # qemu is 20 to 40 times slower than the Pi
    )
    sample = os.environ.get("SDRSCAN_WHISPER_SAMPLE")
    if not sample:
        assert stt.transcribe(voice(2.0)) is not None
        return
    with wave.open(sample) as w:
        assert w.getframerate() == 16_000 and w.getnchannels() == 1
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16) / 32768.0
    tr = stt.transcribe(pcm)
    assert tr is not None and "ask not what your country" in tr.text.lower(), tr
    assert tr.confidence > 0.5
