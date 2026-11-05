"""Call-sign extraction from speech-to-text transcripts: formats, phonetics and negatives."""

from __future__ import annotations

import pytest

from sdrscan.callsign import extract

POSITIVE = [
    # (text, band, expected calls in order, kind of the first)
    ("K7ABC", "ham_2m", ["K7ABC"], "ham"),
    ("this is kd7xyz mobile", "ham_2m", ["KD7XYZ"], "ham"),
    ("W1AW, W7XY and N2Q", "ham_2m", ["W1AW", "W7XY", "N2Q"], "ham"),
    ("AA7BQ calling", "ham_2m", ["AA7BQ"], "ham"),
    ("kilo seven alpha bravo charlie", "ham_2m", ["K7ABC"], "ham"),
    ("Kilo Delta Seven X-ray Yankee Zulu, over.", "ham_2m", ["KD7XYZ"], "ham"),
    ("whiskey one alfa whiskey", "ham_2m", ["W1AW"], "ham"),
    ("november niner tango tango", "ham_2m", ["N9TT"], "ham"),
    ("this is k 7 a b c", "ham_2m", ["K7ABC"], "ham"),
    ("kilo 7 alpha bravo charlie clear", "ham_2m", ["K7ABC"], "ham"),
    ("WRAB123 mobile one", "gmrs_frs_462", ["WRAB123"], "gmrs"),
    ("whiskey romeo alpha bravo one two three", "gmrs_frs_462", ["WRAB123"], "gmrs"),
    ("KAE1234 base", "gmrs_frs_462", ["KAE1234"], "gmrs"),
    ("November one two three alpha bravo, cleared to land", "airband", ["N123AB"], "aviation"),
    ("N4567 runway two seven", "airband", ["N4567"], "aviation"),
    ("november five five two kilo taxi", "airband", ["N552K"], "aviation"),
]


@pytest.mark.parametrize(("text", "band", "calls", "kind"), POSITIVE)
def test_extracts_call_signs(text, band, calls, kind):
    got = extract(text, band=band)
    assert [c.call for c in got] == calls, got
    assert got[0].kind == kind
    assert all(0.0 < c.confidence <= 1.0 for c in got)


NEGATIVE = [
    ("", "ham_2m"),
    ("thank you", "ham_2m"),
    ("Thanks for watching!", "ham_2m"),
    ("unit two on scene, copy that", "vhf_lmr_2"),
    ("K9 unit responding to I-95", "vhf_lmr_2"),
    ("wear your N95 mask", "ham_2m"),
    ("the W2 and the W3C form", "ham_2m"),
    ("nitrous N2O tank", "ham_2m"),
    ("mike two is at the hotel five", "vhf_lmr_2"),  # phonetic words used as words
    ("a 1 a", "ham_2m"),
    ("echo one", "ham_2m"),
    ("November one two three", "ham_2m"),  # tail numbers only count on airband
    ("4x4 on route 66", "vhf_lmr_2"),
    ("delta 1 5 0 heading two seven zero", "airband"),  # airline flight, not a tail
]


@pytest.mark.parametrize(("text", "band"), NEGATIVE)
def test_rejects_words_numbers_and_hallucinations(text, band):
    assert extract(text, band=band) == []


def test_written_calls_are_more_confident_than_loose_letters():
    written = extract("K7ABC", band="ham_2m")[0].confidence
    spelled = extract("kilo seven alpha bravo charlie", band="ham_2m")[0].confidence
    loose = extract("k 7 a b c", band="ham_2m")[0].confidence
    assert written >= spelled > loose


def test_repeated_call_is_reported_once():
    got = extract("K7ABC this is K7ABC, kilo seven alpha bravo charlie", band="ham_2m")
    assert [c.call for c in got] == ["K7ABC"]


def test_one_by_one_special_event_call_is_less_certain():
    one = extract("N2Q", band="ham_2m")[0].confidence
    assert one < extract("N2QX", band="ham_2m")[0].confidence
