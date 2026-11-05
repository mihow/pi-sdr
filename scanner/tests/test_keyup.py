"""Short carriers without speech become key-up rows, and every row keeps its CTCSS tone."""

from __future__ import annotations

import numpy as np
import pytest

from sdrscan.bandplan import BandPlan
from sdrscan.config import DEFAULT_BANDPLAN, Config
from sdrscan.runner import Runner, tx_class
from sdrscan.store import Store
from sdrscan.tracker import Transmission
from sdrscan.voice.gate import VoiceGate

from .audio import ctcss, voice

HZ = 146_940_000


@pytest.fixture
def runner(tmp_path, vclock):
    store = Store.open(tmp_path / "scan.sqlite", vclock)
    store.migrate()
    plan = BandPlan.load(DEFAULT_BANDPLAN)
    r = Runner(cfg=Config(), plan=plan, source=None, clock=vclock, store=store)
    yield r
    store.close()


def _tx(t: float, dur: float, source: str = "dwell") -> Transmission:
    return Transmission(hz=HZ, band="ham_2m", mode="nfm", t_start=t, t_end=t + dur,
                        dur_ms=int(dur * 1000), peak_snr_db=25.0, med_snr_db=22.0,
                        floor_db=-60.0, bw_hz=11_000.0, source=source)  # fmt: skip


def _close(runner: Runner, pcm: np.ndarray | None, dur: float, source: str = "dwell") -> dict:
    if pcm is not None:
        gate = runner._gates[HZ] = VoiceGate()
        for i in range(0, len(pcm), 1600):
            gate.push(pcm[i : i + 1600])
    runner._close_tx(_tx(runner.clock.now(), dur, source))
    runner.store.flush()
    return runner.store.transmissions()[-1]


def _quieted(seconds: float, tone: float | None = 100.0) -> np.ndarray:
    """An FM carrier with no modulation demodulates to near silence, plus its tone."""
    rng = np.random.default_rng(5)
    pcm = 0.0005 * rng.normal(size=int(seconds * 16_000)).astype(np.float32)
    return pcm + (ctcss(seconds, hz=tone, amp=0.1) if tone else 0)


def test_short_carrier_with_a_tone_is_a_key_up_with_its_tone(runner):
    row = _close(runner, _quieted(1.0), dur=1.0)
    assert (row["class"], row["ctcss_hz"]) == ("keyup", 100.0)
    assert runner.store.counts(runner.clock.now(), runner.clock.now())["keyups"] == 1


def test_short_carrier_without_a_tone_is_still_a_key_up(runner):
    row = _close(runner, _quieted(0.6, tone=None), dur=0.6)
    assert (row["class"], row["ctcss_hz"]) == ("keyup", None)


def test_voice_keeps_its_class_and_records_the_tone(runner):
    pcm = voice(2.5) + ctcss(2.5, hz=100.0, amp=0.15)
    row = _close(runner, pcm, dur=2.5)
    assert (row["class"], row["ctcss_hz"]) == ("voice", 100.0)


def test_long_carrier_without_voice_is_not_a_key_up(runner):
    row = _close(runner, _quieted(4.0), dur=9.0)
    assert (row["class"], row["ctcss_hz"]) == ("unknown", 100.0)


def test_survey_only_transmission_has_no_audio_evidence(runner):
    row = _close(runner, None, dur=1.0, source="survey")
    assert (row["class"], row["ctcss_hz"]) == ("unknown", None)


@pytest.mark.parametrize(
    ("cls", "dur_ms", "want"),
    [("voice", 800, "voice"), ("data", 800, "data"), ("unknown", 800, "keyup"),
     ("unknown", 5000, "keyup"), ("unknown", 5001, "unknown")],
)  # fmt: skip
def test_tx_class(cls, dur_ms, want):
    assert tx_class({"class": cls}, dur_ms, keyup_max_s=5.0) == want
