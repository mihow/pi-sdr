"""Stage results on real captures from a private corpus (``SDRSCAN_IQ_CORPUS``).

The clips and their expected results are in ``tests/corpus/manifest.json``. Each test reads
the manifest slice straight from the corpus file, checks its sha256, runs one stage and
compares with the manifest. A clip whose file is missing or whose bytes changed skips with
the reason. Expectations are presence, absence, class and frequency within a tolerance,
never exact counts, so small detector changes do not churn them.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sdrscan import AUDIO_RATE, stages
from sdrscan.bandplan import BandPlan
from sdrscan.config import DEFAULT_BANDPLAN
from sdrscan.corpus import ClipUnavailable, check_clip, corpus_root, load_manifest
from sdrscan.iq.file import open_iq

MANIFEST = Path(__file__).parent / "corpus" / "manifest.json"
CLIPS = load_manifest(MANIFEST)
PLAN = BandPlan.load(DEFAULT_BANDPLAN)


def _cases(stage: str) -> list:
    out = []
    for clip in CLIPS:
        exp = clip.expect.get(stage)
        if exp is None:
            continue
        for i, item in enumerate(exp if isinstance(exp, list) else [exp]):
            marks = (
                [pytest.mark.xfail(reason=item["xfail"], strict=False)] if "xfail" in item else []
            )
            out.append(pytest.param(clip, item, id=f"{clip.id}-{i}", marks=marks))
    return out


def _open(clip):
    try:
        return check_clip(clip, corpus_root())
    except ClipUnavailable as exc:
        pytest.skip(str(exc))


def test_manifest_is_well_formed():
    ids = [c.id for c in CLIPS]
    assert len(ids) == len(set(ids))
    for c in CLIPS:
        assert not Path(c.file).is_absolute() and ".." not in Path(c.file).parts
        assert len(c.sha256) == 64 and 0 < c.seconds <= 5 and c.expect


@pytest.mark.corpus
@pytest.mark.parametrize(("clip", "exp"), _cases("detect"))
def test_detect(clip, exp):
    f, start, stop = _open(clip)
    recs = list(stages.detect(f, PLAN, start=start, stop=stop))
    assert len(recs) == int(clip.seconds * 10)
    peaks = [p for r in recs for p in r["peaks"]]
    if exp.get("none"):
        assert peaks == [], stages.summarize_detections(recs)["channels"][:5]
    for want in exp.get("channels", []):
        hit = sum(
            any(abs(p["hz"] - want["hz"]) <= want["tol_hz"] for p in r["peaks"]) for r in recs
        )
        assert hit / len(recs) >= want["min_hop_frac"], (want, hit, len(recs))
    clip_frac = max(r["clip_frac"] for r in recs)
    assert clip_frac <= exp.get("clip_frac_max", 1.0)
    assert clip_frac >= exp.get("clip_frac_min", 0.0)


def _audio(clip, item) -> np.ndarray:
    f, start, stop = _open(clip)
    return stages.demod(
        f, stages.demod_channel(PLAN, item["hz"], item["mode"]), start=start, stop=stop
    )


@pytest.mark.corpus
@pytest.mark.parametrize(("clip", "exp"), _cases("demod"))
def test_demod(clip, exp):
    audio = _audio(clip, exp)
    assert len(audio) == int(clip.seconds * AUDIO_RATE)
    a = audio[AUDIO_RATE // 10 :]
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a)))) ** 2
    freqs = np.fft.rfftfreq(len(a), 1 / AUDIO_RATE)
    spec[freqs < 60] = 0
    assert float(freqs[int(np.argmax(spec))]) == pytest.approx(
        exp["audio_peak_hz"], abs=exp["tol_hz"]
    )


@pytest.mark.corpus
@pytest.mark.parametrize(("clip", "exp"), _cases("gate"))
def test_gate(clip, exp):
    recs = stages.gate(_audio(clip, exp))
    voice_s = stages.segment_seconds(recs, "voice")
    assert recs[-1]["class"] == exp["class"], recs
    assert voice_s >= exp.get("min_voice_s", 0.0)
    assert voice_s <= exp.get("max_voice_s", float("inf"))
    if "ctcss_hz" in exp:
        assert recs[-1]["ctcss_hz"] == exp["ctcss_hz"], recs[-1]


@pytest.mark.corpus
@pytest.mark.parametrize(("clip", "exp"), _cases("decodes_like"))
def test_cu8_decodes_like_the_corpus_float_copy(clip, exp):
    """The reader's 8-bit scaling matches the float file the capture script made from it."""
    f, start, stop = _open(clip)
    root = corpus_root()
    other_path = root / exp["file"]
    if not other_path.exists():
        pytest.skip(f"{clip.id}: {exp['file']} is not in the corpus")
    other = open_iq(other_path)
    a, b = other.sample_range(clip.offset_s, clip.seconds)
    if other.sha256(a, b - a) != exp["sha256"]:
        pytest.skip(f"{clip.id}: sha256 of {exp['file']} differs from the manifest")
    np.testing.assert_allclose(f.read(start, stop - start), other.read(a, b - a), atol=1e-6)


def test_unusable_clips_say_why(tmp_path):
    """Unset variable, missing file and changed bytes each skip with their own reason."""
    from sdrscan.corpus import Clip
    from sdrscan.iq.file import write_iq

    clip = Clip(id="c", file="x.cu8", offset_s=0.0, seconds=0.001, sha256="0" * 64, expect={})
    with pytest.raises(ClipUnavailable, match="set SDRSCAN_IQ_CORPUS"):
        check_clip(clip, None)
    with pytest.raises(ClipUnavailable, match="not in"):
        check_clip(clip, tmp_path)
    write_iq(
        tmp_path / "x.cu8", np.zeros(100, np.complex64), rate=100_000, center_hz=1, gain_db=None
    )
    with pytest.raises(ClipUnavailable, match="sha256"):
        check_clip(clip, tmp_path)
    f = open_iq(tmp_path / "x.cu8")
    good = Clip(**{**clip.__dict__, "sha256": f.sha256(0, 100)})
    assert check_clip(good, tmp_path)[1:] == (0, 100)
