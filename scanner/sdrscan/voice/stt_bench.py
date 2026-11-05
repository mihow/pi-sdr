"""Measure every available speech-to-text engine on this host and choose one (``stt-bench``).

The reference set is a directory of 16 kHz mono WAV segments and a ``reference.json``
(``sdrscan.stt_reference/1``). Each segment is ``speech`` with reference text, or ``noise``
(audio that passed the voice gate but holds no speech). The set is private, because it
holds real people's speech; it never goes into the repository.

For each candidate engine the bench measures, on this host and at ``stt.threads``:

* word error rate (WER) over all speech segments, after ``normalize``;
* the share of noise segments whose transcript would be shown as fact (text that
  ``is_hallucination`` keeps, at a confidence of at least ``stt.min_confidence``);
* the real-time factor (processing time over audio time; model loading not included);
* the peak resident memory of a fresh process that loads the engine and runs the set;
* the size of the model files on disk.

The selection rule (``choose``) is deterministic. An engine is eligible when it ran, its
RTF is at most ``stt.max_rtf``, its peak RSS at most ``stt.max_rss_mb``, its model at most
``stt.max_model_mb``, and its shown-noise share at most ``stt.max_noise_shown_frac``. The
eligible engine with the lowest WER (rounded to 3 decimals) wins; ties go to the smaller
peak RSS, then the smaller model, then the label. With no eligible engine the choice is
``none``. The choice file records every engine's numbers, the host and the reference hash.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import platform
import re
import resource
import subprocess
import sys
import time
import typing
from pathlib import Path

from .. import AUDIO_RATE
from ..config import STT_ENGINES, SttConfig, _coerce
from .stt import (
    CHOICE_SCHEMA,
    ENGINE_FIELDS,
    MODEL_FIELD,
    SttEngine,
    build_engine,
    engine_problem,
    is_hallucination,
    read_wav,
)

REFERENCE_SCHEMA = "sdrscan.stt_reference/1"
_PREFIX = {
    "whisper_cpp": "whisper_",
    "sherpa_parakeet": "parakeet_",
    "sherpa_moonshine": "moonshine_",
    "faster_whisper": "faster_whisper_",
}


# -- text normalisation and WER -----------------------------------------------------------
# A small subset of Whisper's EnglishTextNormalizer, so that "you're" matches "you are" and
# "twenty five" matches "25". Built in, so every host scores the same way.
_CONTRACTIONS = (
    (r"\bwon't\b", "will not"),
    (r"\bcan't\b", "can not"),
    (r"\blet's\b", "let us"),
    (r"\bain't\b", "aint"),
    (r"n't\b", " not"),
    (r"'re\b", " are"),
    (r"'s\b", " is"),
    (r"'d\b", " would"),
    (r"'ll\b", " will"),
    (r"'ve\b", " have"),
    (r"'m\b", " am"),
)
_FILLERS = {"um", "uh", "hmm", "mm", "mhm", "er", "ah", "eh", "hm", "uhm", "umm"}
_UNITS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen".split()
    )
}
_TENS = {
    w: 10 * i
    for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split())
    if i >= 2
}
_ORDINALS = {
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th",
    "sixth": "6th", "seventh": "7th", "eighth": "8th", "ninth": "9th", "tenth": "10th",
    "eleventh": "11th", "twelfth": "12th", "twentieth": "20th", "thirtieth": "30th",
}  # fmt: skip


def _numbers(words: list[str]) -> list[str]:
    """Spoken cardinals to digits: "twenty five" is 25, "one hundred and two" is 102."""
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w not in _UNITS and w not in _TENS:
            out.append(_ORDINALS.get(w, w))
            i += 1
            continue
        total, cur = 0, 0
        while i < len(words):
            w = words[i]
            if w in _UNITS:
                if cur % 10 or (cur and cur < 20):
                    break  # "one two" is two numbers
                cur += _UNITS[w]
            elif w in _TENS:
                if cur % 100:
                    break
                cur += _TENS[w]
            elif w == "hundred" and cur:
                cur *= 100
            elif w == "thousand" and (cur or total):
                total, cur = (total + cur) * 1000, 0
            elif w == "and" and i + 1 < len(words) and words[i + 1] in {**_UNITS, **_TENS}:
                pass
            else:
                break
            i += 1
        out.append(str(total + cur))
    return out


def normalize(text: str | None) -> str:
    """Lower case, no sound tags or punctuation, contractions expanded, numbers as digits."""
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|<[^>]*>", " ", t)
    for pat, rep in _CONTRACTIONS:
        t = re.sub(pat, rep, t)
    t = re.sub(r"(\d),(\d)", r"\1\2", t)
    t = re.sub(r"[^\w\s.%]|_", " ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)
    words = [w for w in t.split() if w not in _FILLERS]
    return " ".join(_numbers(words))


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1]


def corpus_wer(pairs: typing.Iterable[tuple[str, str]]) -> float | None:
    """Total word edits over total reference words, both sides normalised."""
    errors = words = 0
    for ref, hyp in pairs:
        r, h = normalize(ref).split(), normalize(hyp).split()
        errors += edit_distance(r, h)
        words += len(r)
    return errors / words if words else None


# -- reference set ------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Segment:
    id: str
    path: Path
    kind: str  # speech | noise
    ref: str | None
    seconds: float


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_reference(path: Path) -> tuple[list[Segment], str]:
    """The segments and the sha256 of reference.json. Every WAV's sha256 is checked."""
    path = Path(path)
    raw = path.read_bytes()
    doc = json.loads(raw)
    if doc.get("schema") != REFERENCE_SCHEMA:
        raise ValueError(f"{path.name}: expected schema {REFERENCE_SCHEMA}")
    segs = []
    for item in doc.get("segments", []):
        wav = path.parent / item["file"]
        if item.get("sha256") and sha256_file(wav) != item["sha256"]:
            raise ValueError(f"{item['id']}: {item['file']} does not match its sha256")
        if item["kind"] not in ("speech", "noise"):
            raise ValueError(f"{item['id']}: kind must be speech or noise")
        if item["kind"] == "speech" and not normalize(item.get("ref")):
            raise ValueError(f"{item['id']}: a speech segment needs reference text")
        pcm, rate = read_wav(wav)
        if rate != AUDIO_RATE:
            raise ValueError(f"{item['id']}: expected {AUDIO_RATE} Hz, got {rate}")
        segs.append(Segment(item["id"], wav, item["kind"], item.get("ref"), len(pcm) / rate))
    if not segs:
        raise ValueError(f"{path.name}: no segments")
    return segs, hashlib.sha256(raw).hexdigest()


# -- candidates ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Candidate:
    label: str
    engine: str
    cfg: SttConfig

    def settings(self) -> dict:
        return {k: _jsonable(getattr(self.cfg, k)) for k in ENGINE_FIELDS[self.engine]}


def _jsonable(v):
    return str(v) if isinstance(v, Path) else v


def parse_candidates(spec: str | None, cfg: SttConfig) -> list[Candidate]:
    """``name[:key=value...]`` separated by commas; default every engine with ``cfg``.

    ``key`` is ``model`` (the engine's model path) or an SttConfig field of that engine,
    with or without its prefix: ``whisper_cpp:audio_ctx=768``,
    ``faster_whisper:model=/models/faster-whisper-small.en``.
    """
    hints = typing.get_type_hints(SttConfig)
    out: list[Candidate] = []
    for part in spec.split(",") if spec else STT_ENGINES:
        name, *opts = part.strip().split(":")
        if name not in STT_ENGINES:
            raise ValueError(f"unknown engine {name!r}; engines: {', '.join(STT_ENGINES)}")
        kw, tags = {}, []
        for opt in opts:
            key, sep, value = opt.partition("=")
            if not sep:
                raise ValueError(f"{part}: options are key=value")
            field = MODEL_FIELD[name] if key == "model" else key
            if field not in ENGINE_FIELDS[name]:
                field = _PREFIX[name] + key
            if field not in ENGINE_FIELDS[name]:
                raise ValueError(f"{part}: {name} has no setting {key!r}")
            is_path = hints[field] is Path or Path in typing.get_args(hints[field])
            loaded = value if is_path else json.loads(value)
            kw[field] = _coerce(hints[field], loaded, field)
            tags.append(f"{key}={Path(value).name if key == 'model' else value}")
        label = ":".join([name, *tags])
        while label in {c.label for c in out}:
            label += "'"
        out.append(Candidate(label, name, dataclasses.replace(cfg, engine=name, **kw)))
    return out


# -- measurement --------------------------------------------------------------------------
def model_mb(engine: SttEngine | None, cfg: SttConfig, name: str) -> float:
    files = engine.model_files() if hasattr(engine, "model_files") else None
    if files is None:
        root = Path(getattr(cfg, MODEL_FIELD[name]))
        files = [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else [root]
    return sum(Path(f).stat().st_size for f in files) / 1e6


def _peak_rss_mb() -> float:
    """This process's peak RSS plus its children's (whisper-cli), in MB.

    ``VmHWM`` is used for this process because ``ru_maxrss`` survives exec on Linux: a
    worker started by a large process (a test runner) would report its parent's peak. A
    child's ``ru_maxrss`` has the same flaw, so the whisper.cpp figure is an upper bound
    that can include the worker's own peak.
    """
    self_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    try:
        m = re.search(r"^VmHWM:\s+(\d+) kB", Path("/proc/self/status").read_text(), re.M)
        self_kb = int(m.group(1)) if m else self_kb
    except OSError:
        pass
    return (self_kb + resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) / 1024


def run_segments(engine: SttEngine, segments: list[Segment]) -> list[dict]:
    rows = []
    for seg in segments:
        pcm, _ = read_wav(seg.path)
        t0 = time.perf_counter()
        tr = engine.transcribe(pcm)
        dt = time.perf_counter() - t0
        rows.append(
            {
                "id": seg.id,
                "text": tr.text if tr else "",
                "confidence": tr.confidence if tr else 0.0,
                "proc_s": dt,
                "audio_s": seg.seconds,
            }
        )
    return rows


def measure_in_process(
    cand: Candidate,
    segments: list[Segment],
    factory: typing.Callable[[Candidate], SttEngine] | None = None,
    work_dir: Path | None = None,
) -> dict:
    """Load the engine here and run the set. RSS is this process's, so it is only fair in a
    fresh process (``measure`` runs this in a worker)."""
    t0 = time.perf_counter()
    if factory is not None:
        engine = factory(cand)
    else:
        engine = build_engine(cand.cfg, cand.engine, work_dir=work_dir)
    load_s = time.perf_counter() - t0
    rows = run_segments(engine, segments)
    return {
        "rows": rows,
        "load_s": load_s,
        "peak_rss_mb": _peak_rss_mb(),
        "model_mb": model_mb(engine, cand.cfg, cand.engine),
    }


def measure(cand: Candidate, segments: list[Segment], timeout_s: float | None = None) -> dict:
    """Run the candidate in a fresh Python process, so that its peak RSS is its own."""
    job = {
        "engine": cand.engine,
        "cfg": {k: _jsonable(v) for k, v in dataclasses.asdict(cand.cfg).items()},
        "segments": [dataclasses.asdict(s) | {"path": str(s.path)} for s in segments],
    }
    budget = timeout_s or 600 + 60 * sum(s.seconds for s in segments)
    proc = subprocess.run(
        [sys.executable, "-m", "sdrscan.voice.stt_bench"],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        timeout=budget,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        raise RuntimeError(tail[0])
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _worker() -> int:  # pragma: no cover - runs in the subprocess started by measure()
    from ..config import _build

    job = json.loads(sys.stdin.read())
    cfg = _build(SttConfig, job["cfg"], prefix="stt.")
    segs = [Segment(**(s | {"path": Path(s["path"])})) for s in job["segments"]]
    cand = Candidate(job["engine"], job["engine"], cfg)
    print(json.dumps(measure_in_process(cand, segs)))
    return 0


def score(cand: Candidate, segments: list[Segment], run: dict, min_confidence: float) -> dict:
    by_id = {r["id"]: r for r in run["rows"]}
    speech = [s for s in segments if s.kind == "speech"]
    noise = [s for s in segments if s.kind == "noise"]
    wer = corpus_wer((s.ref or "", by_id[s.id]["text"]) for s in speech)
    shown = sum(
        1
        for s in noise
        if by_id[s.id]["text"].strip()
        and not is_hallucination(by_id[s.id]["text"])
        and by_id[s.id]["confidence"] >= min_confidence
    )
    proc = sum(r["proc_s"] for r in run["rows"])
    audio = sum(r["audio_s"] for r in run["rows"])
    return {
        "label": cand.label,
        "engine": cand.engine,
        "status": "ok",
        "settings": cand.settings(),
        "wer": None if wer is None else round(wer, 4),
        "speech_n": len(speech),
        "empty_speech": sum(1 for s in speech if not normalize(by_id[s.id]["text"])),
        "noise_n": len(noise),
        "noise_text": sum(1 for s in noise if normalize(by_id[s.id]["text"])),
        "noise_shown": shown,
        "noise_shown_frac": round(shown / len(noise), 4) if noise else 0.0,
        "rtf": round(proc / audio, 4) if audio else None,
        "load_s": round(run["load_s"], 2),
        "peak_rss_mb": round(run["peak_rss_mb"], 1),
        "model_mb": round(run["model_mb"], 1),
    }


# -- selection rule -----------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Limits:
    max_rtf: float = 0.5
    max_rss_mb: float = 1200.0
    max_model_mb: float = 800.0
    max_noise_shown_frac: float = 0.15

    @classmethod
    def from_cfg(cls, cfg: SttConfig) -> Limits:
        return cls(cfg.max_rtf, cfg.max_rss_mb, cfg.max_model_mb, cfg.max_noise_shown_frac)


def exclusions(r: dict, lim: Limits) -> list[str]:
    """Why a measured engine is not eligible; empty when it is."""
    if r.get("status") != "ok":
        return [f"{r.get('status')}: {r.get('error', '')}".strip()]
    out = []
    if r.get("wer") is None:
        out.append("no speech segments to score")
    if r.get("rtf") is None or r["rtf"] > lim.max_rtf:
        out.append(f"rtf {r.get('rtf')} > {lim.max_rtf}")
    if r["peak_rss_mb"] > lim.max_rss_mb:
        out.append(f"peak rss {r['peak_rss_mb']} MB > {lim.max_rss_mb}")
    if r["model_mb"] > lim.max_model_mb:
        out.append(f"model {r['model_mb']} MB > {lim.max_model_mb}")
    if r["noise_shown_frac"] > lim.max_noise_shown_frac:
        out.append(f"noise shown {r['noise_shown_frac']} > {lim.max_noise_shown_frac}")
    return out


def rank_key(r: dict) -> tuple:
    return (round(r["wer"], 3), r["peak_rss_mb"], r["model_mb"], r["label"])


def choose(results: list[dict], lim: Limits) -> dict:
    """The winning result (or None) and every engine's exclusion reasons, in rank order."""
    eligible = sorted((r for r in results if not exclusions(r, lim)), key=rank_key)
    return {
        "winner": eligible[0] if eligible else None,
        "ranked": [r["label"] for r in eligible],
        "excluded": {r["label"]: exclusions(r, lim) for r in results if exclusions(r, lim)},
    }


# -- the bench ----------------------------------------------------------------------------
def host_info() -> dict:
    info = {
        "machine": platform.machine(),
        "system": platform.system(),
        "release": platform.release(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }
    try:
        text = Path("/proc/cpuinfo").read_text()
        m = re.search(r"^(?:model name|Model)\s*:\s*(.+)$", text, re.M)
        info["cpu"] = m.group(1).strip() if m else None
        mem = re.search(r"^MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text(), re.M)
        info["mem_total_mb"] = round(int(mem.group(1)) / 1024) if mem else None
    except OSError:
        pass
    return info


def run_bench(
    reference: Path,
    candidates: list[Candidate],
    cfg: SttConfig,
    lim: Limits,
    measure_fn: typing.Callable[[Candidate, list[Segment]], dict] = measure,
    log: typing.Callable[[str], None] = lambda s: None,
) -> tuple[dict, dict[str, list[dict]]]:
    """The choice document, and each engine's per-segment output (private: real speech)."""
    segments, ref_hash = load_reference(reference)
    results, details = [], {}
    for cand in candidates:
        problem = engine_problem(cand.cfg, cand.engine)
        if problem is not None:
            results.append(
                {"label": cand.label, "engine": cand.engine, "status": "unavailable",
                 "error": problem, "settings": cand.settings()}
            )  # fmt: skip
            log(f"{cand.label}: unavailable ({problem})")
            continue
        log(f"{cand.label}: running {len(segments)} segments")
        try:
            run = measure_fn(cand, segments)
        except Exception as exc:  # noqa: BLE001 - one failing engine must not stop the bench
            results.append(
                {"label": cand.label, "engine": cand.engine, "status": "error",
                 "error": str(exc)[:300], "settings": cand.settings()}
            )  # fmt: skip
            log(f"{cand.label}: error ({exc})")
            continue
        details[cand.label] = run["rows"]
        results.append(score(cand, segments, run, cfg.min_confidence))
    verdict = choose(results, lim)
    win = verdict["winner"]
    choice = {
        "schema": CHOICE_SCHEMA,
        "engine": win["engine"] if win else "none",
        "label": win["label"] if win else None,
        "settings": win["settings"] if win else {},
        "threads": cfg.threads,
        "rule": dataclasses.asdict(lim)
        | {"rank": "lowest wer (3 decimals), then peak_rss_mb, model_mb, label"},
        "ranked": verdict["ranked"],
        "excluded": verdict["excluded"],
        "results": results,
        "host": host_info(),
        "reference": {
            "sha256": ref_hash,
            "speech_n": sum(s.kind == "speech" for s in segments),
            "noise_n": sum(s.kind == "noise" for s in segments),
            "speech_s": round(sum(s.seconds for s in segments if s.kind == "speech"), 1),
        },
        "created": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }
    return choice, details


def format_table(choice: dict) -> str:
    head = f"{'engine':<40} {'WER':>6} {'RTF':>6} {'RSS MB':>7} {'model MB':>8} {'noise':>6}  "
    lines = [head + "verdict"]
    for r in choice["results"]:
        if r["status"] != "ok":
            lines.append(f"{r['label']:<40} {r['status']}: {r.get('error', '')}")
            continue
        why = choice["excluded"].get(r["label"])
        verdict = "CHOSEN" if r["label"] == choice["label"] else ("; ".join(why) if why else "ok")
        lines.append(
            f"{r['label']:<40} {r['wer']:>6.3f} {r['rtf']:>6.3f} {r['peak_rss_mb']:>7.0f} "
            f"{r['model_mb']:>8.0f} {r['noise_shown']:>2}/{r['noise_n']:<3}  {verdict}"
        )
    lines.append(f"choice: {choice['engine']} ({choice['label']})")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_worker())
