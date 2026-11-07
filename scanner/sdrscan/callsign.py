"""Find call signs in speech-to-text transcripts, so the digest can say who was heard.

Operators say their call sign at least every ten minutes, so a call sign is the most
compact record of who used a channel. Whisper writes a call sign either as one token
("K7ABC") or as the words the operator spoke ("kilo seven alpha bravo charlie"). This
module turns both into the written form and keeps only strings that fit a real format:

- US amateur: a 1 or 2 letter prefix (K, N, W, or AA to AL), one digit, and a 1 to 3
  letter suffix (1x1 up to 2x3, for example K7AB, KD7ABC, AA7BQ).
- GMRS: 4 letters and 3 digits (WRAB123), or the older 3 letters and 4 digits (KAE1234).
- Aviation, on airband only: a US registration ("November one two three alpha bravo" is
  N123AB). Airline flight numbers ("Delta 150") are not registrations and are not found.

Spoken letters are NATO/ITU phonetics; spoken digits are number words, with "niner".
Loose single letters ("k 7 a b c") also count, with a lower confidence, because English
uses "a" and "i" as words. A format match is the guard against ordinary speech: every
format needs letters on both sides of a digit, so "unit two" or "hotel five" never match.
A short blocklist removes the few common words that fit a format (N2O, W3C), and a
transcript that is a known whisper hallucination yields nothing.
"""

from __future__ import annotations

import dataclasses
import re

from .voice.stt import is_hallucination

PHONETIC = {
    "alpha": "A", "alfa": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E",
    "foxtrot": "F", "golf": "G", "hotel": "H", "india": "I", "juliet": "J", "juliett": "J",
    "kilo": "K", "lima": "L", "mike": "M", "november": "N", "oscar": "O", "papa": "P",
    "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T", "uniform": "U", "victor": "V",
    "whiskey": "W", "whisky": "W", "xray": "X", "yankee": "Y", "zulu": "Z",
}  # fmt: skip
DIGITS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "niner": "9",
}  # fmt: skip
FORMATS = {
    "ham": re.compile(r"(?:[KNW][A-Z]?|A[A-L])[0-9][A-Z]{1,3}"),
    "gmrs": re.compile(r"W[A-Z]{3}[0-9]{3}|K[A-Z]{2}[0-9]{4}"),
    "aviation": re.compile(r"N[1-9](?:[0-9]{1,4}|[0-9]{0,3}[A-HJ-NP-Z]|[0-9]{0,2}[A-HJ-NP-Z]{2})"),
}
BLOCKLIST = frozenset({"N2O", "W3C", "K9S"})
AVIATION_BANDS = frozenset({"airband"})
MIN_CONFIDENCE = 0.4  # weaker matches are not returned at all
SHOW_CONFIDENCE = 0.6  # stored calls below this are kept but not summarized

# Confidence by how the call was written. "written": one token that is the whole call.
_BASE = {"written": 0.9, "spoken": 0.8, "mixed": 0.7, "loose": 0.55}
_ONE_BY_ONE_PENALTY = 0.2  # K7A style special-event calls are rare; a word fits more often
_ADJACENT_PENALTY = 0.1  # more call-like symbols run on before or after the match
_MAX_TOKENS = 12
_WORD = re.compile(r"[a-z0-9]+")
_DOTTED = re.compile(r"\b(?:[A-Za-z]\.){2,}")  # W.R.A.B.
_MAX_GLUED = 12  # longest letters-and-digits token split into groups (WRAB123BOB)


@dataclasses.dataclass(frozen=True)
class Callsign:
    call: str
    kind: str  # ham | gmrs | aviation
    confidence: float


def _symbols(text: str) -> list[list[tuple[str, str]]]:
    """Runs of call-like tokens as (characters, source) pairs; other words end a run.

    Whisper often writes a spoken call as capitals and digits apart ("WRAB 123"), with
    dots or hyphens between the letters ("W.R.A.B.", "W-R-A-B"), or with the operator's
    name run on ("WRAB123BOB"). A short all-capital group counts as written, so it can
    join the digits after it; a long letters-and-digits token is split into its groups.
    """
    text = _DOTTED.sub(lambda m: m.group(0).replace(".", ""), text).replace("-", "")
    runs: list[list[tuple[str, str]]] = []
    run: list[tuple[str, str]] = []
    for m in re.finditer(r"[A-Za-z0-9]+|[^A-Za-z0-9\s]", text):
        raw = m.group(0)
        w = raw.lower()
        syms: list[tuple[str, str]] = []
        if w in PHONETIC:
            syms = [(PHONETIC[w], "spoken")]
        elif w in DIGITS:
            syms = [(DIGITS[w], "spoken")]
        elif w.isdigit() and len(w) <= 4:
            syms = [(w, "digits")]
        elif len(w) == 1 and w.isalpha():
            syms = [(w.upper(), "loose")]
        elif raw.isalpha() and raw.isupper() and 2 <= len(raw) <= 4:
            syms = [(raw, "written")]
        elif _WORD.fullmatch(w) and not w.isalpha() and not w.isdigit():
            if len(w) <= 7:
                syms = [(w.upper(), "written")]
            elif len(w) <= _MAX_GLUED:
                syms = [(g.upper(), "written") for g in re.findall(r"[a-z]+|[0-9]+", w)]
        if not syms:
            if run:
                runs.append(run)
            run = []
        else:
            run.extend(syms)
    if run:
        runs.append(run)
    return runs


def _grade(tokens: list[tuple[str, str]]) -> float:
    sources = {s for _, s in tokens}
    if sources == {"written"}:
        return _BASE["written"] if len(tokens) == 1 else _BASE["mixed"]
    if "loose" in sources:
        return _BASE["loose"]
    if "written" in sources:
        return _BASE["mixed"]
    spoken_letters = sum(1 for c, s in tokens if s == "spoken" and c.isalpha())
    return _BASE["spoken"] if spoken_letters >= 2 else _BASE["mixed"]


def extract(text: str, band: str | None = None) -> list[Callsign]:
    """Call signs in ``text``, each once, in the order first heard.

    ``band`` is the band id of the channel; tail numbers are looked for only on airband,
    where "November" means a US registration rather than the letter N in a ham call.
    """
    if not text or is_hallucination(text):
        return []
    kinds = ("aviation",) if band in AVIATION_BANDS else ("ham", "gmrs")
    found: dict[str, tuple[tuple[int, int], Callsign]] = {}
    for r, run in enumerate(_symbols(text)):
        n = len(run)
        taken = [False] * n
        # Longest token-aligned match first, so K7ABC wins over K7AB inside it.
        spans = sorted(
            ((i, j) for i in range(n) for j in range(i + 1, min(n, i + _MAX_TOKENS) + 1)),
            key=lambda s: (s[0] - s[1], s[0]),
        )
        for i, j in spans:
            if any(taken[i:j]):
                continue
            s = "".join(c for c, _ in run[i:j])
            kind = next((k for k in kinds if FORMATS[k].fullmatch(s)), None)
            if kind is None or s in BLOCKLIST:
                continue
            conf = _grade(run[i:j])
            if kind == "ham" and re.fullmatch(r"[A-Z][0-9][A-Z]", s):
                conf -= _ONE_BY_ONE_PENALTY
            if (i > 0 and not taken[i - 1]) or (j < n and not taken[j]):
                conf -= _ADJACENT_PENALTY
            if conf < MIN_CONFIDENCE:
                continue
            for k in range(i, j):
                taken[k] = True
            prev = found.get(s)
            pos = (r, i) if prev is None else min(prev[0], (r, i))
            best = conf if prev is None else max(conf, prev[1].confidence)
            found[s] = (pos, Callsign(s, kind, round(best, 2)))
    return [c for _, c in sorted(found.values(), key=lambda pc: pc[0])]
