"""Pipeline stages that read one file artifact and write one, so each is testable alone.

Stage    Reads               Writes              Per-block code shared with the scan loop
capture  a radio             IQ capture          ``IqSource.tune`` and ``read``
detect   IQ capture          detections JSONL    ``detect.analyze``
demod    IQ capture + Hz     16 kHz mono WAV     ``dsp.channelizer.Channelizer.push``
gate     WAV                 segments JSONL      ``voice.gate.VoiceGate.push`` and ``summary``
stt      WAV                 transcript JSON     ``SttEngine.transcribe``
replay   detections JSONL    a store (digest)    ``Runner.ingest_hop`` and the tracker

The functions here only walk a file through the same objects the runner uses, at the same
block sizes (0.1 s hops and dwell blocks), so a stage test and the live service exercise
one code path. Every record carries a ``schema`` field; bump its number when a field
changes meaning.
"""

from __future__ import annotations

import dataclasses
import json
import typing
from pathlib import Path

import numpy as np

from . import AUDIO_RATE
from .bandplan import BandPlan, Channel
from .clock import VirtualClock
from .config import Config, DetectConfig
from .detect import HopResult, analyze
from .dsp.channelizer import Channelizer
from .iq.base import IqSource, IqSourceError
from .iq.file import IqFile, IqWriter
from .runner import Runner
from .store import Store
from .voice.gate import Gate, VoiceGate, VoiceState
from .voice.stt import SttEngine

DETECTIONS_SCHEMA = "sdrscan.detections/1"
SEGMENTS_SCHEMA = "sdrscan.segments/1"
TRANSCRIPT_SCHEMA = "sdrscan.transcript/1"
HOP_S = 0.1  # a survey hop and a dwell block are both 100 ms in the scan loop
DEMOD_MODES = ("nfm", "am")


# -- files ---------------------------------------------------------------------------------
def write_jsonl(path: Path, records: typing.Iterable[dict]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            n += 1
    return n


def read_jsonl(path: Path, schema: str) -> list[dict]:
    """Records of one schema; a line with another schema is an error, not skipped."""
    out = []
    for i, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("schema") != schema:
            raise ValueError(f"{Path(path).name}:{i}: schema {rec.get('schema')!r}, want {schema}")
        out.append(rec)
    return out


# -- capture -------------------------------------------------------------------------------
def capture(
    source: IqSource,
    writer: IqWriter,
    *,
    center_hz: int,
    gain_db: float,
    seconds: float,
    block_s: float = HOP_S,
) -> dict:
    """Tune once and stream ``seconds`` of IQ into ``writer``; return the source's health.

    The caller closes the writer, so it can add the health figures to the sidecar.
    """
    rate = int(writer.meta["rate"])
    total, n = int(round(seconds * rate)), max(1, int(block_s * rate))
    source.open()
    try:
        source.tune(center_hz, gain_db)
        while writer.n_samples < total:
            block = source.read(min(n, total - writer.n_samples))
            if not len(block):
                raise IqSourceError("the radio returned no samples")
            writer.write(block)
        return source.health()
    finally:
        source.close()


# -- detect --------------------------------------------------------------------------------
def detect(
    f: IqFile,
    plan: BandPlan,
    cfg: DetectConfig = DetectConfig(),  # noqa: B008  (frozen dataclass, safe default)
    *,
    start: int = 0,
    stop: int | None = None,
    hop_s: float = HOP_S,
) -> typing.Iterator[dict]:
    """One record per ``hop_s`` block: the hop's floor, clipping and peaks.

    ``t_s`` is seconds from the start of the file. Each peak carries the band-plan channel
    it snaps to, so a reader does not need the plan to ask "was 443.150 heard".
    """
    n = int(round(hop_s * f.rate))
    if n < cfg.nfft:
        raise ValueError(f"a hop of {hop_s} s is shorter than one {cfg.nfft}-point FFT")
    for i, block in enumerate(f.blocks(n, start, stop)):
        if len(block) < cfg.nfft:
            break  # a short tail cannot make one FFT
        r = analyze(block, f.center_hz, f.rate, plan, cfg)
        rec = {"schema": DETECTIONS_SCHEMA, "t_s": round((start + i * n) / f.rate, 4)}
        rec.update(r.to_record())
        for p in rec["peaks"]:
            ch = plan.channel_for(p["hz"])
            p["channel_hz"], p["band"] = ch.hz, ch.band
        yield rec


def summarize_detections(records: list[dict]) -> dict:
    """Per-channel counts over a detections file (what a person reads first)."""
    channels: dict[int, dict] = {}
    for rec in records:
        for p in rec["peaks"]:
            c = channels.setdefault(
                p["channel_hz"], {"hz": p["channel_hz"], "band": p["band"], "hops": 0}
            )
            c["hops"] += 1
            c["max_snr_db"] = max(c.get("max_snr_db", -1e9), p["snr_db"])
    return {
        "hops": len(records),
        "max_clip_frac": max((r["clip_frac"] for r in records), default=0.0),
        "channels": sorted(channels.values(), key=lambda c: (-c["hops"], c["hz"])),
    }


# -- demod ---------------------------------------------------------------------------------
def demod_channel(plan: BandPlan, hz: float, mode: str | None = None) -> Channel:
    """The band plan's channel parameters at exactly ``hz``; ``mode`` overrides the band's."""
    ch = plan.channel_for(hz)
    mode = mode or ch.mode
    if mode not in DEMOD_MODES:
        raise ValueError(
            f"{hz / 1e6:.4f} MHz is in band {ch.band!r} (mode {ch.mode}); give a mode: nfm or am"
        )
    return dataclasses.replace(ch, hz=int(round(hz)), mode=mode)


def demod(
    f: IqFile,
    channel: Channel,
    *,
    start: int = 0,
    stop: int | None = None,
    block_s: float = HOP_S,
) -> np.ndarray:
    """One channel's audio, float32 at ``AUDIO_RATE``, through the dwell channelizer."""
    if abs(channel.hz - f.center_hz) > 0.45 * f.rate:
        raise ValueError(
            f"{channel.hz / 1e6:.4f} MHz is outside the capture "
            f"({f.center_hz / 1e6:.4f} MHz +/- {f.rate / 2e6:.2f} MHz)"
        )
    chz = Channelizer(f.rate, f.center_hz, [channel])
    parts = [chz.push(b)[channel.hz] for b in f.blocks(int(block_s * f.rate), start, stop)]
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


# -- gate ----------------------------------------------------------------------------------
def gate(
    pcm: np.ndarray,
    *,
    rate: int = AUDIO_RATE,
    gate_factory: typing.Callable[[], Gate] = VoiceGate,
    chunk_s: float = HOP_S,
) -> list[dict]:
    """Segments of constant gate state, then one summary record.

    Audio goes to the gate in ``chunk_s`` pieces, as dwell blocks reach it in the scan loop.
    A segment covers consecutive pieces with the same state (``pending`` until the gate has
    10 frames). The summary's ``class`` is what the runner stores for a transmission that
    lasted the whole file.
    """
    g = gate_factory()
    if rate != g.rate:
        raise ValueError(f"the gate reads {g.rate} Hz audio, got {rate} Hz")
    n = max(1, int(chunk_s * rate))
    segs: list[dict] = []
    for i in range(0, len(pcm), n):
        state = g.push(pcm[i : i + n])
        t0, t1 = i / rate, min(len(pcm), i + n) / rate
        if segs and segs[-1]["state"] == state.value:
            segs[-1]["t1_s"] = round(t1, 3)
        else:
            segs.append(
                {
                    "schema": SEGMENTS_SCHEMA,
                    "kind": "segment",
                    "state": state.value,
                    "t0_s": round(t0, 3),
                    "t1_s": round(t1, 3),
                }
            )
    summary = {"schema": SEGMENTS_SCHEMA, "kind": "summary", "seconds": round(len(pcm) / rate, 3)}
    summary.update(g.summary())
    return [*segs, summary]


def segment_seconds(records: list[dict], state: VoiceState | str) -> float:
    want = state.value if isinstance(state, VoiceState) else state
    return sum(
        r["t1_s"] - r["t0_s"] for r in records if r["kind"] == "segment" and r["state"] == want
    )


# -- stt -----------------------------------------------------------------------------------
def transcribe(pcm: np.ndarray, engine: SttEngine, *, rate: int = AUDIO_RATE) -> dict:
    """The engine's transcript of the whole clip. Empty audio is not sent to the engine."""
    if rate != AUDIO_RATE:
        raise ValueError(f"speech-to-text reads {AUDIO_RATE} Hz audio, got {rate} Hz")
    rec: dict = {
        "schema": TRANSCRIPT_SCHEMA,
        "seconds": round(len(pcm) / rate, 3),
        "engine": None,
        "text": None,
        "confidence": None,
        "rtf": None,
    }
    tr = engine.transcribe(pcm) if len(pcm) else None
    if tr is not None:
        rec.update(engine=tr.engine, text=tr.text, confidence=tr.confidence, rtf=tr.rtf)
    return rec


# -- replay detections into a store --------------------------------------------------------
class _NoRadio:
    """The replay's source: ``Runner.ingest_hop`` never reads it."""

    def open(self) -> None:
        raise IqSourceError("a detections replay has no radio")

    def tune(self, center_hz: int, gain_db: float) -> None:
        raise IqSourceError("a detections replay has no radio")

    def read(self, n: int) -> np.ndarray:
        raise IqSourceError("a detections replay has no radio")

    def close(self) -> None:
        return None

    def health(self) -> dict:
        return {}


def replay_detections(
    records: list[dict],
    *,
    cfg: Config,
    plan: BandPlan,
    store: Store,
    clock: VirtualClock,
    source: str = "dwell",
) -> int:
    """Feed hops through the runner's tracker into ``store``; return the transmission count.

    A capture is one continuous read of one window, which the scan loop calls a dwell, so
    hops count as dwell readings by default. Voice class stays ``unknown``: that needs the
    gate, which reads audio, not detections.
    """
    t0 = clock.now()  # ``t_s`` 0 of the detections; ``store`` must share this clock
    runner = Runner(cfg=cfg, plan=plan, source=_NoRadio(), clock=clock, store=store)
    for rec in records:
        t = t0 + float(rec["t_s"])
        if t > clock.now():
            clock.advance(t - clock.now())
        runner.ingest_hop(HopResult.from_record(rec), source)
    clock.advance(HOP_S)
    runner.shutdown()
    return int(store.db.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0])
