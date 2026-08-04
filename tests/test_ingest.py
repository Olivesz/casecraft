"""Ingest tests — the font decoder and the confidence gates.

The decoder is the subtle part: a constant glyph offset that applies to a
different letter range per font, plus a dictionary repair for capitals the
shift swallowed. It's easy to break silently, so the mappings are pinned here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from casecraft.ingest import build, darden
from casecraft.ingest.extract import _decode_span, _repair, _looks_displaced

BUNDLED = Path(__file__).resolve().parent.parent / "data" / "cases"


# ── broken font decoding ─────────────────────────────────────────────────── #

def _decode(text: str, bold: bool = False) -> str:
    return _repair(_decode_span(text, bold))


def test_regular_face_displaces_only_q_to_z():
    assert _decode("FoU each pUoblem") == "For each problem"
    assert _decode("bXVineVV") == "business"
    assert _decode("\\oXU compan\\") == "your company"


def test_bold_face_displaces_the_whole_alphabet():
    assert _decode("XHVWLRQ", bold=True) == "uestion"
    assert _decode("SURILW", bold=True) == "profit"


def test_repair_restores_a_capital_the_shift_swallowed():
    # 'Q' decodes to 'n', giving "nuestion"; the dictionary puts the Q back.
    assert _decode("QXHVWLRQ", bold=True) == "Question"


def test_repair_prefers_the_capital_at_a_sentence_start():
    # "WKDW" is both "that" and "What"; position decides.
    assert _decode("QXHVWLRQ: WKDW LV \\RXU SURILW?", bold=True) == "Question: What is your profit?"


def test_repair_leaves_mid_sentence_lowercase_alone():
    assert _decode("VR WKDW LV", bold=True) == "so that is"


def test_undisplaced_text_is_untouched():
    assert _decode("The client is profitable.") == "The client is profitable."


def test_displacement_detector():
    assert _looks_displaced("YoX oZn a bXVineVV Velling high-end VhoeV. " * 12)
    assert not _looks_displaced("You own a business selling high-end shoes. " * 12)


# ── numeric extraction ───────────────────────────────────────────────────── #

def test_calculation_prefers_a_conclusion_line():
    section = (
        "Calculation\n"
        "Disney+: 100M x $7 x 12 = $8.4B\n"
        "Hulu: 35M x $11 x 12 = $4.62B\n"
        "Annual Projected Revenue with Merger = $14.35B\n"
    )
    result = darden.parse_calculation(section)
    assert result["expected"] == 14_350_000_000
    assert result["steps"], "intermediate lines should become partial-credit steps"


def test_calculation_ignores_years():
    assert darden.parse_calculation("Math\nGrowth from 2021 = 2024\n") is None


def test_calculation_returns_none_without_arithmetic():
    assert darden.parse_calculation("Guidance\nThe candidate should discuss risks.\n") is None


# ── confidence gates ─────────────────────────────────────────────────────── #

@pytest.mark.parametrize("text", [
    "Calculate the expected annual revenue from the new subscription model.",
    "How many customers a day does the store need to break even?",
    "What is the total cost of the project?",
    "Estimate the size of the US coffee market.",
])
def test_numeric_asks_are_recognised(text):
    assert build._asks_for_a_number(text)


@pytest.mark.parametrize("text", [
    "What other information do you need to assess this option?",
    "What are some ways you might improve profitability?",
    "How would you approach this problem?",
    "Current Subscribers: Disney+: 100M subscribers; Hulu: 35M subscribers",
])
def test_non_numeric_questions_are_rejected(text):
    assert not build._asks_for_a_number(text), \
        "a question that doesn't ask for a number must never get a numeric rubric"


def test_data_slides_are_not_treated_as_questions():
    assert not build._looks_like_question(
        "Current Pricing: Disney+: $7/month; Hulu: $11/month")


# ── imported cases stay out of the distributable bundle ──────────────────── #

def test_no_imported_cases_in_the_bundle():
    """Imported casebook content must never reach data/cases/.

    That directory ships with the tool; casebooks are copyrighted and at least
    one carries an explicit no-redistribution notice.
    """
    for path in BUNDLED.glob("*.json"):
        case = json.loads(path.read_text())
        assert not case.get("meta", {}).get("imported"), f"{path.name} is imported"
        assert "original" in case["source"].get("casebook", "").lower(), \
            f"{path.name} has non-original provenance: {case['source']}"


def test_imported_math_is_not_graded_on_an_unverified_number():
    """The one invariant protecting against confidently-wrong grading.

    Extracting a final answer from a free-form worked solution proved ~1-in-3
    reliable, so imported math is graded against the solution text instead. A
    numeric rubric here would mark correct answers wrong.
    """
    q = build.math_question(
        "q1", 1, "Calculate the total revenue.", "100 x $7 = $700 total",
        {"expected": 700, "_source_line": "100 x $7 = $700 total"},
        difficulty=3, tags=["arithmetic"], context="")
    assert q["rubric"]["kind"] == "open"
    assert q["_candidate_expected"] == 700
    assert "UNVERIFIED" in q["_review"]


# ── live transcription guards ────────────────────────────────────────────── #

def test_hallucinated_subtitle_junk_is_dropped():
    """Whisper emits subtitle furniture on silence — it must never reach the page."""
    from casecraft import stt
    for junk in ["[BLANK_AUDIO]", "(water splashing)", "♪", "You", "Thank you", "."]:
        assert stt._clean(junk) == "", f"{junk!r} leaked through"


def test_real_speech_survives_the_guard():
    from casecraft import stt
    text = "forty routes times six flights is 240 flights per day"
    assert stt._clean(text) == text


def test_no_case_prompt_contains_a_letter_spaced_header():
    """Every 2024 Darden prompt ended with "E D U C AT I O N | G R O W T H".

    That is the text read aloud to the candidate — the most visible surface in
    the product — and all fourteen were affected.
    """
    import glob
    import os
    import re

    spaced = re.compile(r"(?:\b[A-Z]\s){4,}[A-Z]\b")
    leaks = []
    for path in (glob.glob(str(BUNDLED / "*.json"))
                 + glob.glob(os.path.expanduser("~/.casecraft/cases/*.json"))):
        case = json.loads(open(path).read())
        if spaced.search(case["prompt"]["text"]):
            leaks.append(case["id"])
    assert not leaks, f"typeset header leaked into the prompt of: {leaks}"


def test_tidy_strips_headers_but_keeps_punctuation():
    from casecraft.ingest.darden import _tidy
    assert _tidy("Hooville serves 4,000 students. E D U C AT I O N | G R O W T H") \
        == "Hooville serves 4,000 students."
    assert _tidy("A prompt ending in a real sentence.") == "A prompt ending in a real sentence."


def test_no_imported_math_question_is_unanswerable():
    """Four imported questions asked for a calculation with ZERO input numbers
    anywhere in the case — the data was on the exhibit slide, and the parser
    kept only a 120-char title. An impossible question that grades you wrong
    is worse than no question.
    """
    import glob
    import os
    import re

    for path in glob.glob(os.path.expanduser("~/.casecraft/cases/*.json")):
        case = json.loads(open(path).read())
        pool_extra = " ".join(
            [(e.get("text") or "") + " " + (e.get("title") or "")
             for e in case.get("exhibits", [])]
            + [c.get("response", "") for c in case.get("clarifications", [])])
        for q in case.get("questions", []):
            if q["type"] != "math":
                continue
            pool = q.get("prompt", "") + " " + (q.get("context") or "") + " " + pool_extra
            assert len(re.findall(r"\d", pool)) >= 3, \
                f"{case['id']}/{q['id']} asks for a calculation with no data anywhere"
