"""Each pipeline stage on small synthetic files, with one noise-in, nothing-out case each."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from sdrscan import AUDIO_RATE, IQ_RATE, stages
from sdrscan.bandplan import BandPlan
from sdrscan.clock import VirtualClock
from sdrscan.config import DEFAULT_BANDPLAN, Config
from sdrscan.iq.file import open_iq, write_iq
from sdrscan.iq.scene import Scene
from sdrscan.iq.synthetic import SyntheticSource
from sdrscan.store import Store
from sdrscan.voice.stt import FakeStt, NullStt, WhisperCppStt

from .audio import dtmf, fm_noise, voice

CENTER = 462_406_250
GMRS = 462_562_500
T0 = 1_780_272_000.0


@pytest.fixture(scope="module")
def plan() -> BandPlan:
    return BandPlan.load(DEFAULT_BANDPLAN)


def render(path: Path, emitters: str, seconds: float, center: int = CENTER) -> Path:
    """Render a synthetic scene to a capture (cu8 for .cu8 paths, as a V4 would write)."""
    clock = VirtualClock(T0)
    scene = Scene.from_yaml(
        f"noise_floor: {{default_db: -45}}\ndc_db: -30\nemitters:\n{emitters or '  []'}\n",
        clock=clock,
    )
    src = SyntheticSource(scene, clock, simulate_settle=False)
    src.open()
    src.tune(center, 30.0)
    write_iq(path, src.read(int(seconds * IQ_RATE)), rate=IQ_RATE, center_hz=center, gain_db=30.0)
    return path


@pytest.fixture(scope="module")
def tone_file(tmp_path_factory) -> Path:
    em = f"  - {{kind: nfm_voice, hz: {GMRS}, level_db: -20, tone_hz: 1000}}"
    return render(tmp_path_factory.mktemp("iq") / "tone.cu8", em, 1.0)


@pytest.fixture(scope="module")
def noise_file(tmp_path_factory) -> Path:
    return render(tmp_path_factory.mktemp("iq") / "noise.cu8", "", 1.0)


# -- detect --------------------------------------------------------------------------------
def test_detect_finds_the_carrier_every_hop(plan, tone_file):
    recs = list(stages.detect(open_iq(tone_file), plan))
    assert len(recs) == 10 and all(r["schema"] == stages.DETECTIONS_SCHEMA for r in recs)
    assert [r["t_s"] for r in recs[:3]] == [0.0, 0.1, 0.2]
    for r in recs:
        hits = [p for p in r["peaks"] if p["channel_hz"] == GMRS]
        assert hits and abs(hits[0]["hz"] - GMRS) < 2_000 and hits[0]["snr_db"] > 15
        assert hits[0]["band"] == "gmrs_frs_462"
    summary = stages.summarize_detections(recs)
    assert summary["channels"][0]["hz"] == GMRS and summary["channels"][0]["hops"] == 10


def test_detect_on_noise_finds_nothing(plan, noise_file):
    recs = list(stages.detect(open_iq(noise_file), plan))
    assert len(recs) == 10
    assert all(r["peaks"] == [] for r in recs)


def test_detect_honours_offset_and_duration(plan, tone_file):
    f = open_iq(tone_file)
    start, stop = f.sample_range(0.5, 0.3)
    recs = list(stages.detect(f, plan, start=start, stop=stop))
    assert [r["t_s"] for r in recs] == [0.5, 0.6, 0.7]


def test_hop_record_round_trips(plan, tone_file):
    from sdrscan.detect import HopResult

    rec = next(stages.detect(open_iq(tone_file), plan))
    hop = HopResult.from_record(rec)
    again = hop.to_record()
    assert again["peaks"][0]["hz"] == rec["peaks"][0]["hz"]
    assert (again["lo_hz"], again["hi_hz"]) == (rec["lo_hz"], rec["hi_hz"])


# -- demod ---------------------------------------------------------------------------------
def _strongest_hz(audio: np.ndarray) -> tuple[float, float]:
    """(strongest audio frequency above 100 Hz, its share of the power above 100 Hz)."""
    a = audio[AUDIO_RATE // 10 :]
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a)))) ** 2
    freqs = np.fft.rfftfreq(len(a), 1 / AUDIO_RATE)
    band = freqs > 100
    k = int(np.argmax(np.where(band, spec, 0)))
    near = band & (np.abs(freqs - freqs[k]) < 20)
    return float(freqs[k]), float(spec[near].sum() / spec[band].sum())


def test_demod_recovers_the_tone(plan, tone_file):
    audio = stages.demod(open_iq(tone_file), stages.demod_channel(plan, GMRS))
    assert audio.dtype == np.float32 and len(audio) == AUDIO_RATE
    hz, share = _strongest_hz(audio)
    assert hz == pytest.approx(1000, abs=10) and share > 0.5


def test_demod_of_an_empty_channel_has_no_tone(plan, noise_file):
    audio = stages.demod(open_iq(noise_file), stages.demod_channel(plan, GMRS))
    _, share = _strongest_hz(audio)
    assert share < 0.05


def test_demod_refuses_a_frequency_outside_the_capture(plan, tone_file):
    with pytest.raises(ValueError, match="outside"):
        stages.demod(open_iq(tone_file), stages.demod_channel(plan, 470_000_000, "nfm"))


def test_demod_channel_needs_a_mode_outside_voice_bands(plan):
    with pytest.raises(ValueError, match="give a mode"):
        stages.demod_channel(plan, 97_100_000)
    ch = stages.demod_channel(plan, 97_100_000, "am")
    assert (ch.hz, ch.mode) == (97_100_000, "am")


# -- gate ----------------------------------------------------------------------------------
def test_gate_marks_voice(plan):
    recs = stages.gate(voice(2.0))
    assert recs[-1]["kind"] == "summary" and recs[-1]["class"] == "voice"
    assert recs[0]["state"] == "pending" and recs[0]["t0_s"] == 0.0
    assert stages.segment_seconds(recs, "voice") >= 1.5
    assert all(r["schema"] == stages.SEGMENTS_SCHEMA for r in recs)


def test_gate_marks_tones_as_data():
    recs = stages.gate(dtmf(1.5))
    assert recs[-1]["class"] == "data" and stages.segment_seconds(recs, "data") >= 1.0


def test_gate_on_fm_noise_finds_no_voice_or_data():
    recs = stages.gate(fm_noise(2.0))
    assert recs[-1]["class"] == "unknown"
    assert stages.segment_seconds(recs, "voice") == 0 and stages.segment_seconds(recs, "data") == 0


def test_gate_refuses_another_rate():
    with pytest.raises(ValueError, match="16000"):
        stages.gate(voice(1.0), rate=8_000)


# -- stt -----------------------------------------------------------------------------------
def _whisper(tmp_path: Path, text: str) -> WhisperCppStt:
    def run(argv, **kw):
        out = Path(argv[argv.index("-of") + 1]).with_suffix(".json")
        tokens = [{"text": w, "p": 0.9} for w in text.split()]
        out.write_text(json.dumps({"transcription": [{"text": text, "tokens": tokens}]}))

        class Done:
            returncode = 0

        return Done()

    return WhisperCppStt(Path("whisper-cli"), Path("m.bin"), work_dir=tmp_path, run=run)


def test_stt_returns_the_engine_text(tmp_path):
    rec = stages.transcribe(voice(1.0), _whisper(tmp_path, "unit two on scene"))
    assert rec["schema"] == stages.TRANSCRIPT_SCHEMA
    assert (rec["text"], rec["engine"], rec["seconds"]) == ("unit two on scene", "whisper.cpp", 1.0)
    assert rec["confidence"] == pytest.approx(0.9)


def test_stt_on_noise_keeps_a_hallucination_at_zero_confidence(tmp_path):
    rec = stages.transcribe(fm_noise(1.0), _whisper(tmp_path, "Thank you."))
    assert rec["confidence"] == 0.0


def test_stt_of_empty_audio_calls_no_engine():
    class Boom(FakeStt):
        def transcribe(self, pcm):
            raise AssertionError("the engine must not see empty audio")

    rec = stages.transcribe(np.zeros(0, np.float32), Boom())
    assert rec["text"] is None and rec["seconds"] == 0.0
    assert stages.transcribe(voice(0.5), NullStt())["text"] is None


# -- replay detections into a digest -------------------------------------------------------
def _replay(tmp_path: Path, plan, recs: list[dict]) -> Store:
    clock = VirtualClock(T0)
    store = Store.open(tmp_path / "replay.sqlite", clock)
    store.migrate()
    stages.replay_detections(recs, cfg=Config(), plan=plan, store=store, clock=clock)
    return store


def test_replay_turns_a_burst_into_one_transmission(tmp_path, plan):
    em = f"  - {{kind: nfm_voice, hz: {GMRS}, level_db: -20, tone_hz: 1000, bursts: [[0.3, 2.0]]}}"
    path = render(tmp_path / "burst.cu8", em, 3.0)
    recs = list(stages.detect(open_iq(path), plan))
    store = _replay(tmp_path, plan, recs)
    rows = store.transmissions()
    assert [r["hz"] for r in rows] == [GMRS]
    assert rows[0]["dur_ms"] == pytest.approx(2_000, abs=300)


def test_replay_of_noise_stores_no_transmission(tmp_path, plan, noise_file):
    store = _replay(tmp_path, plan, list(stages.detect(open_iq(noise_file), plan)))
    assert store.transmissions() == []
