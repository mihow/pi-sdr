"""stt-bench: scoring, the selection rule, and the bench plumbing on a synthetic reference.

The real reference set is private (real speech) and lives outside the repository; the
``corpus`` test at the end runs it when SDRSCAN_STT_REFERENCE points at its reference.json.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import platform
import sys
from pathlib import Path

import numpy as np
import pytest

from sdrscan.cli import main
from sdrscan.config import SttConfig, load_config
from sdrscan.voice import stt_bench as sb
from sdrscan.voice.stt import Transcript, select_stt, write_wav


# -- normalisation and WER ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "norm"),
    [
        ("Copy that, en route.", "copy that en route"),
        ("You're on   scene; I'm out.", "you are on scene i am out"),
        ("I can't, won't, didn't", "i can not will not did not"),
        ("twenty five miles", "25 miles"),
        ("one hundred and two", "102"),
        ("one two three", "1 2 3"),
        ("[music] um, unit (inaudible) four", "unit 4"),
        ("162.55 megahertz, the tenth", "162.55 megahertz the 10th"),
        ("1,200 feet", "1200 feet"),
        (None, ""),
    ],
)
def test_normalize(text, norm):
    assert sb.normalize(text) == norm


def test_corpus_wer_counts_edits_over_reference_words():
    pairs = [("engine two on scene", "engine 2 on scene"), ("copy that", "copy cat now")]
    assert sb.corpus_wer(pairs) == pytest.approx(2 / 6)  # cat for that, now inserted
    assert sb.corpus_wer([("a b c", "")]) == 1.0
    assert sb.corpus_wer([]) is None


# -- the selection rule -------------------------------------------------------------------
def _r(label, wer, rtf=0.1, rss=500.0, mb=100.0, noise=0.0, status="ok"):
    return {
        "label": label,
        "engine": label.split(":")[0],
        "status": status,
        "settings": {},
        "wer": wer,
        "rtf": rtf,
        "peak_rss_mb": rss,
        "model_mb": mb,
        "noise_shown_frac": noise,
    }


LIM = sb.Limits(max_rtf=0.5, max_rss_mb=1200, max_model_mb=800, max_noise_shown_frac=0.15)


def test_lowest_wer_wins_among_eligible():
    res = [_r("faster_whisper", 0.22), _r("sherpa_parakeet", 0.15), _r("whisper_cpp", 0.24)]
    v = sb.choose(res, LIM)
    assert v["winner"]["label"] == "sherpa_parakeet"
    assert v["ranked"] == ["sherpa_parakeet", "faster_whisper", "whisper_cpp"]
    assert v["excluded"] == {}


@pytest.mark.parametrize(
    ("best", "reason"),
    [
        (_r("best", 0.10, rtf=0.51), "rtf"),
        (_r("best", 0.10, rss=1201), "peak rss"),
        (_r("best", 0.10, mb=801), "model"),
        (_r("best", 0.10, noise=0.2), "noise shown"),
        (_r("best", None), "no speech"),
        (_r("best", 0.10, status="unavailable"), "unavailable"),
        (_r("best", 0.10, status="error"), "error"),
    ],
)
def test_each_limit_excludes_an_otherwise_better_engine(best, reason):
    v = sb.choose([best, _r("ok", 0.30)], LIM)
    assert v["winner"]["label"] == "ok"
    assert any(reason in why for why in v["excluded"]["best"])


def test_limits_are_inclusive():
    v = sb.choose([_r("edge", 0.2, rtf=0.5, rss=1200, mb=800, noise=0.15)], LIM)
    assert v["winner"]["label"] == "edge"


def test_wer_ties_at_three_decimals_go_to_smaller_rss_then_model_then_label():
    a = _r("a", 0.1504, rss=900)
    b = _r("b", 0.1496, rss=400)  # rounds to the same 0.150 and is smaller
    assert sb.choose([a, b], LIM)["winner"]["label"] == "b"
    c, d = _r("c", 0.15, rss=400, mb=300), _r("d", 0.15, rss=400, mb=200)
    assert sb.choose([c, d], LIM)["winner"]["label"] == "d"
    e, f = _r("f", 0.15), _r("e", 0.15)
    assert sb.choose([e, f], LIM)["winner"]["label"] == "e"


def test_no_results_or_none_eligible_chooses_nothing():
    assert sb.choose([], LIM)["winner"] is None
    v = sb.choose([_r("slow", 0.1, rtf=2.0)], LIM)
    assert v["winner"] is None and v["ranked"] == []


# -- candidates ---------------------------------------------------------------------------
def test_parse_candidates_reads_engine_options():
    cfg = SttConfig(whisper_model=Path("/m/base.bin"))
    cands = sb.parse_candidates(
        "whisper_cpp,whisper_cpp:audio_ctx=768:suppress_nst=true,"
        "faster_whisper:model=/m/faster-whisper-small.en:beam=1",
        cfg,
    )
    assert [c.label for c in cands] == [
        "whisper_cpp",
        "whisper_cpp:audio_ctx=768:suppress_nst=true",
        "faster_whisper:model=faster-whisper-small.en:beam=1",
    ]
    assert cands[1].cfg.whisper_audio_ctx == 768 and cands[1].cfg.whisper_suppress_nst
    assert cands[2].cfg.faster_whisper_model == Path("/m/faster-whisper-small.en")
    assert cands[2].cfg.faster_whisper_beam == 1
    assert cands[2].settings()["faster_whisper_model"] == "/m/faster-whisper-small.en"
    assert [c.engine for c in sb.parse_candidates(None, cfg)] == list(sb.STT_ENGINES)
    for bad in ("nope", "whisper_cpp:beam=2", "whisper_cpp:audio_ctx"):
        with pytest.raises(ValueError):
            sb.parse_candidates(bad, cfg)


# -- bench plumbing on a synthetic reference ----------------------------------------------
SPEECH = {"s1": "engine two on scene", "s2": "copy that en route", "s3": "unit four clear"}


def _reference(tmp_path: Path) -> Path:
    """Tones of distinct lengths stand in for speech; the fake engines key on the length."""
    ref = tmp_path / "ref"
    (ref / "wav").mkdir(parents=True)
    segs = []
    items = [(k, "speech", v) for k, v in SPEECH.items()] + [("n1", "noise", None)]
    for i, (sid, kind, text) in enumerate(items):
        t = np.arange(int(16_000 * (1.0 + 0.25 * i))) / 16_000
        wav = ref / "wav" / f"{sid}.wav"
        write_wav(wav, 0.3 * np.sin(2 * np.pi * 440 * t))
        digest = hashlib.sha256(wav.read_bytes()).hexdigest()
        segs.append({"id": sid, "file": f"wav/{sid}.wav", "sha256": digest, "kind": kind,
                     "ref": text})  # fmt: skip
    path = ref / "reference.json"
    path.write_text(json.dumps({"schema": sb.REFERENCE_SCHEMA, "segments": segs}))
    return path


class _Scripted:
    """Answers by clip length: ``texts`` lists the answer per segment in order."""

    def __init__(self, texts, conf=0.9):
        self.by_len = {int(16_000 * (1.0 + 0.25 * i)): t for i, t in enumerate(texts)}
        self.conf = conf

    def transcribe(self, pcm):
        return Transcript(self.by_len[len(pcm)], self.conf, "scripted")


def _fake_whisper_files(tmp_path):
    (tmp_path / "w").write_text("")
    (tmp_path / "m.bin").write_bytes(b"\0" * 2_000_000)
    return SttConfig(whisper_binary=tmp_path / "w", whisper_model=tmp_path / "m.bin")


def test_bench_measures_scores_and_chooses(tmp_path):
    ref = _reference(tmp_path)
    cfg = _fake_whisper_files(tmp_path)
    engines = {
        "whisper_cpp": _Scripted([*SPEECH.values(), "[music]"]),  # perfect, noise filtered
        "whisper_cpp:audio_ctx=768": _Scripted(["engine two", "copy", "unit", "hello there"]),
    }
    cands = sb.parse_candidates("whisper_cpp,whisper_cpp:audio_ctx=768,sherpa_parakeet", cfg)

    def measure(cand, segs):
        return sb.measure_in_process(cand, segs, factory=lambda c: engines[c.label])

    # In-process RSS is the test runner's own, so the RSS limit is off for this test.
    lim = dataclasses.replace(LIM, max_rss_mb=float("inf"))
    choice, details = sb.run_bench(ref, cands, cfg, lim, measure_fn=measure)
    res = {r["label"]: r for r in choice["results"]}
    assert res["whisper_cpp"]["wer"] == 0.0 and res["whisper_cpp"]["noise_shown"] == 0
    bad = res["whisper_cpp:audio_ctx=768"]
    assert bad["wer"] == pytest.approx(7 / 11, abs=1e-3) and bad["noise_shown"] == 1
    assert res["sherpa_parakeet"]["status"] == "unavailable"
    assert choice["engine"] == "whisper_cpp" and choice["label"] == "whisper_cpp"
    assert choice["settings"]["whisper_audio_ctx"] == 1024
    assert choice["reference"]["speech_n"] == 3 and choice["reference"]["noise_n"] == 1
    assert choice["reference"]["sha256"] == hashlib.sha256(ref.read_bytes()).hexdigest()
    assert choice["host"]["machine"] == platform.machine()
    assert res["whisper_cpp"]["model_mb"] == pytest.approx(2.0)
    assert res["whisper_cpp"]["peak_rss_mb"] > 0 and res["whisper_cpp"]["rtf"] >= 0
    assert set(details) == {"whisper_cpp", "whisper_cpp:audio_ctx=768"}
    assert "CHOSEN" in sb.format_table(choice)


def test_bench_reports_a_failing_engine_and_keeps_going(tmp_path):
    ref = _reference(tmp_path)
    cfg = _fake_whisper_files(tmp_path)

    def measure(cand, segs):
        raise RuntimeError("model is corrupt")

    choice, _ = sb.run_bench(ref, sb.parse_candidates("whisper_cpp", cfg), cfg, LIM, measure)
    assert choice["engine"] == "none" and choice["results"][0]["status"] == "error"
    assert "corrupt" in choice["excluded"]["whisper_cpp"][0]


def test_reference_with_changed_audio_is_refused(tmp_path):
    ref = _reference(tmp_path)
    (ref.parent / "wav" / "s1.wav").write_bytes(b"RIFF....")
    with pytest.raises(ValueError, match="sha256"):
        sb.load_reference(ref)


FAKE_WHISPER = """#!{python}
import json, sys
a = sys.argv
text = {{1.0: "Engine two on scene.", 1.25: "Copy that, en route.", 1.5: "Unit four clear."}}
import wave
with wave.open(a[a.index("-f") + 1]) as w:
    secs = round(w.getnframes() / w.getframerate(), 2)
seg = {{"text": text.get(secs, "(static)"), "tokens": [{{"text": "x", "p": 0.9}}]}}
out = {{"transcription": [seg]}}
open(a[a.index("-of") + 1] + ".json", "w").write(json.dumps(out))
"""


def test_cli_stt_bench_runs_a_real_worker_and_auto_reads_its_choice(tmp_path, capsys):
    """End to end: a fake whisper-cli, the subprocess worker, choice.json, then auto."""
    ref = _reference(tmp_path)
    binary = tmp_path / "whisper-cli"
    binary.write_text(FAKE_WHISPER.format(python=sys.executable))
    binary.chmod(0o755)
    (tmp_path / "m.bin").write_bytes(b"\0" * 1000)
    conf = tmp_path / "c.yaml"
    conf.write_text(
        f"data_root: {tmp_path / 'data'}\n"
        f"stt:\n  engine: auto\n  whisper_binary: {binary}\n  whisper_model: {tmp_path / 'm.bin'}\n"
    )
    rc = main(["stt-bench", "--reference", str(ref), "--config", str(conf),
               "--engines", "whisper_cpp:audio_ctx=500", "--threads", "1"])  # fmt: skip
    assert rc == 0
    choice = json.loads((tmp_path / "data" / "stt-choice.json").read_text())
    assert choice["engine"] == "whisper_cpp" and choice["threads"] == 1
    r = choice["results"][0]
    assert (r["wer"], r["noise_shown"], r["status"]) == (0.0, 0, "ok")
    assert "CHOSEN" in capsys.readouterr().out
    cfg = load_config(conf)
    sel = select_stt(cfg.stt, cfg.data_root / "stt-choice.json")
    assert (sel.engine, sel.source, sel.cfg.whisper_audio_ctx) == ("whisper_cpp", "choice", 500)


# -- the real reference set (private) -----------------------------------------------------
@pytest.mark.corpus
@pytest.mark.skipif(
    not os.environ.get("SDRSCAN_STT_REFERENCE"),
    reason="set SDRSCAN_STT_REFERENCE to the private reference.json",
)
def test_corpus_stt_bench_chooses_an_engine(tmp_path):  # pragma: no cover - private data
    """Runs the real engines on the private set. SDRSCAN_STT_CONFIG names a config whose
    stt section points at local models; SDRSCAN_STT_ENGINES optionally picks candidates."""
    conf = os.environ.get("SDRSCAN_STT_CONFIG")
    cfg = load_config(Path(conf) if conf else None).stt
    cfg = dataclasses.replace(cfg, threads=int(os.environ.get("SDRSCAN_STT_THREADS", "2")))
    cands = sb.parse_candidates(os.environ.get("SDRSCAN_STT_ENGINES"), cfg)
    choice, _ = sb.run_bench(
        Path(os.environ["SDRSCAN_STT_REFERENCE"]), cands, cfg, sb.Limits.from_cfg(cfg)
    )
    out = os.environ.get("SDRSCAN_STT_CHOICE_OUT")
    if out:
        Path(out).write_text(json.dumps(choice, indent=1, sort_keys=True) + "\n")
    print("\n" + sb.format_table(choice))
    ran = [r for r in choice["results"] if r["status"] == "ok"]
    assert ran, "no engine could run; check SDRSCAN_STT_CONFIG model paths"
    assert all(0.0 <= r["wer"] < 1.0 for r in ran)
    assert choice["reference"]["speech_n"] > 0 and choice["reference"]["noise_n"] > 0
