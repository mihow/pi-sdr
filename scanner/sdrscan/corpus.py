"""A private corpus of real IQ captures, referenced by a committed manifest.

Real recordings never enter the repository. ``tests/corpus/manifest.json`` names each clip
by its path relative to the corpus directory (``SDRSCAN_IQ_CORPUS``), the slice used (offset
and duration in seconds) and the sha256 of that slice's exact bytes, plus the stage results
a test expects. ``check_clip`` explains why a clip cannot be used (unset variable, missing
file, changed bytes), so a test can skip with that reason.

``cut_clip`` copies a short slice of a large capture into a ``.cu8`` (or ``.cf32``) file
with a scanner sidecar, for quick experiments with the stage commands.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import numpy as np

from .iq.file import IqFile, IqWriter, open_iq

CORPUS_ENV = "SDRSCAN_IQ_CORPUS"
MANIFEST_SCHEMA = "sdrscan.corpus/1"


class ClipUnavailable(RuntimeError):
    """The clip cannot be checked or used here; the message says why."""


@dataclasses.dataclass(frozen=True)
class Clip:
    """One manifest entry: a slice of a corpus file and what each stage should report."""

    id: str
    file: str
    offset_s: float
    seconds: float
    sha256: str
    expect: dict
    notes: str = ""


def load_manifest(path: Path) -> list[Clip]:
    raw = json.loads(Path(path).read_text())
    if raw.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"{Path(path).name}: schema {raw.get('schema')!r}, want {MANIFEST_SCHEMA}")
    return [Clip(**c) for c in raw["clips"]]


def corpus_root(env: dict | None = None) -> Path | None:
    value = (env if env is not None else os.environ).get(CORPUS_ENV)
    return Path(value) if value else None


def check_clip(clip: Clip, root: Path | None) -> tuple[IqFile, int, int]:
    """The opened file and the clip's [start, stop) samples, or ``ClipUnavailable``."""
    if root is None:
        raise ClipUnavailable(f"set {CORPUS_ENV} to the capture directory")
    path = root / clip.file
    if not path.exists():
        raise ClipUnavailable(f"{clip.id}: {clip.file} is not in {CORPUS_ENV}")
    f = open_iq(path)
    start, stop = f.sample_range(clip.offset_s, clip.seconds)
    if stop - start < int(round(clip.seconds * f.rate)):
        raise ClipUnavailable(f"{clip.id}: {clip.file} is shorter than the clip")
    digest = f.sha256(start, stop - start)
    if digest != clip.sha256:
        raise ClipUnavailable(
            f"{clip.id}: sha256 of {clip.file} at {clip.offset_s}+{clip.seconds} s is "
            f"{digest[:12]}..., the manifest says {clip.sha256[:12]}... (file changed?)"
        )
    return f, start, stop


def auto_scale(iq: np.ndarray) -> float:
    """Gain that puts the 99.99th percentile component at 0.9, for 8-bit output.

    Float captures from other receivers can sit far below full scale, which would leave
    only a few of the 256 codes in use.
    """
    comp = np.abs(np.ascontiguousarray(iq).view(np.float32))
    level = float(np.percentile(comp, 99.99)) if len(comp) else 0.0
    return 0.9 / level if level > 0 else 1.0


def cut_clip(
    src: Path,
    out: Path,
    *,
    offset_s: float,
    seconds: float,
    scale: float | None = None,
    block: int = 1 << 20,
) -> dict:
    """Write a slice of ``src`` to ``out`` (format from its extension) with a sidecar.

    ``scale`` multiplies the samples; ``None`` picks ``auto_scale`` when an 8-bit file is
    cut from a float capture and 1.0 otherwise, so a cu8 to cu8 cut copies codes exactly.
    Returns the manifest fields for the slice (its source sha256 included).
    """
    f = open_iq(src)
    start, stop = f.sample_range(offset_s, seconds)
    if stop <= start:
        raise ValueError(f"{Path(src).name}: nothing at {offset_s} s (it lasts {f.duration_s} s)")
    if scale is None:
        to_8bit = Path(out).suffix.lower() in {".cu8", ".raw"}
        scale = (
            auto_scale(f.read(start, min(stop - start, f.rate)))
            if to_8bit and f.fmt == "cf32"
            else 1.0
        )
    extra = {
        "source_file": Path(src).name,
        "source_offset_s": offset_s,
        "scale": scale,
        "device": f.meta.get("device"),
    }
    with IqWriter(out, rate=f.rate, center_hz=f.center_hz, gain_db=f.gain_db, extra=extra) as w:
        for b in f.blocks(block, start, stop):
            w.write(b * np.float32(scale) if scale != 1.0 else b)
    return {
        "offset_s": offset_s,
        "seconds": round((stop - start) / f.rate, 6),
        "sha256": f.sha256(start, stop - start),
        "out": str(out),
        "scale": scale,
    }
