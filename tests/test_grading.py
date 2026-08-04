"""Grading tests — weighted toward the failure paths.

A prep tool that only works on model answers is useless: the whole product is
what it says when you're wrong. These tests pin the diagnoses, not just the
verdicts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from casecraft.delivery import analyze
from casecraft.library import Library
from casecraft.scoring import (
    CORRECT, PARTIAL, WRONG, extract_numbers, grade_buckets, grade_numeric, rating,
)

# Drill/selection tests pin to the BUNDLED cases only. The user's imported
# casebooks live in ~/.casecraft/cases and would make counts non-deterministic;
# the leak tests below deliberately still run over the full library.
BUNDLED = Path(__file__).resolve().parent.parent / "data" / "cases"

CASE = json.loads(
    (Path(__file__).resolve().parent.parent / "data" / "cases" / "orchid-airlines.json").read_text()
)
Q = {q["id"]: q for q in CASE["questions"]}


# ── spoken numbers ──────────────────────────────────────────────────────── #

@pytest.mark.parametrize("said,expected", [
    ("about 2.3 billion", 2.3e9),
    ("two point three billion", 2.3e9),
    ("$2,312,640,000", 2312640000),
    ("roughly 2.3 bn a year", 2.3e9),
    ("call it 28,800 passengers", 28800),
    ("negative 19 million", -19e6),
    ("minus 18.6 million", -18.6e6),
    ("twenty eight thousand", 28000),
])
def test_extract_numbers(said, expected):
    assert any(abs(n - expected) < abs(expected) * 0.01 for n in extract_numbers(said))


def test_extract_ignores_no_numbers():
    assert extract_numbers("I have no idea where to even start with this") == []


# ── numeric grading: the paths that matter ──────────────────────────────── #

def test_numeric_correct_with_rounding():
    v = grade_numeric("so roughly 2.3 billion dollars a year", Q["q2"]["rubric"])
    assert v.outcome == CORRECT and v.score == 1.0


def test_numeric_names_the_load_factor_mistake():
    v = grade_numeric("I get about 2.89 billion", Q["q2"]["rubric"])
    assert v.outcome == WRONG
    assert "load factor" in v.note
    assert v.error_id is not None          # feeds repeated-mistake tracking


def test_numeric_names_the_annualization_mistake():
    v = grade_numeric("6.34 million", Q["q2"]["rubric"])
    assert "annual" in v.note.lower()


def test_numeric_partial_credit_from_intermediate_steps():
    v = grade_numeric(
        "240 flights a day, 120 passengers each, so 28,800 people... "
        "I think it's around 900 million", Q["q2"]["rubric"]
    )
    assert v.outcome == PARTIAL
    assert {"flights_per_day", "pax_per_flight", "pax_per_day"} <= set(v.hit)
    assert 0 < v.score < 1


def test_numeric_no_number_at_all_prompts_rather_than_fails():
    v = grade_numeric("honestly I'm not sure how to approach this", Q["q2"]["rubric"])
    assert v.outcome == WRONG and v.score == 0.0
    assert "didn't catch a number" in v.note


def test_numeric_catches_the_variable_cost_confusion():
    v = grade_numeric("I make it about 65.4 million", Q["q3"]["rubric"])
    assert v.outcome == WRONG
    assert "price cut isn't a volume cut" in v.note


# ── bucket policy ───────────────────────────────────────────────────────── #

RUBRIC = Q["q1"]["rubric"]


def test_buckets_all_required_passes():
    v = grade_buckets({"revenue", "fixed_costs", "variable_costs"}, RUBRIC)
    assert v.outcome == CORRECT


def test_buckets_missing_must_have_caps_at_partial():
    """Four smart observations don't rescue a missing core bucket."""
    v = grade_buckets({"revenue", "fixed_costs", "competition", "mece", "hypothesis"}, RUBRIC)
    assert v.outcome == PARTIAL
    assert "Variable costs" in v.note
    assert v.missed[0] == "variable_costs"     # biggest gap first, for probing


def test_buckets_bonus_cannot_rescue_a_hollow_answer():
    v = grade_buckets({"mece", "hypothesis"}, RUBRIC)
    assert v.outcome == WRONG


def test_buckets_bonus_lifts_but_complete_answer_still_reaches_full():
    plain = grade_buckets({"revenue", "fixed_costs", "variable_costs", "competition"}, RUBRIC)
    showy = grade_buckets(
        {"revenue", "fixed_costs", "variable_costs", "competition", "mece", "hypothesis"}, RUBRIC
    )
    assert plain.score == 1.0        # unshowy but complete is still full marks
    assert showy.score == 1.0


def test_buckets_empty_answer():
    assert grade_buckets(set(), RUBRIC).outcome == WRONG


# ── delivery ────────────────────────────────────────────────────────────── #

def test_hedging_is_flagged():
    d = analyze(
        "So I think maybe we could probably look at costs, I guess, "
        "and it might be worth sort of checking revenue as well.",
        18,
    )
    assert d.hedge_count >= 4
    assert any("conviction" in n for n in d.notes)
    assert d.score < 0.8


def test_confident_answer_is_not_penalized():
    d = analyze(
        "I want to look at three things. First, the revenue build. "
        "Second, the cost structure split into fixed and variable. "
        "Third, what competitors have done on price.",
        14,
    )
    assert d.hedge_count == 0
    assert d.signpost_count >= 3
    assert d.score == 1.0


def test_synthesis_must_lead_with_the_answer():
    buried = analyze(
        "Let me walk through what we found. Revenue grew, costs grew faster, "
        "fuel was up ninety percent, and therefore my recommendation is to hedge fuel.",
        20, expects_recommendation=True,
    )
    assert not buried.answer_first
    assert any("Lead with the answer" in n for n in buried.notes)

    upfront = analyze(
        "My recommendation is to attack costs, not price. Revenue is 2.3 billion "
        "and growing, but cost per seat mile is up 35 percent, which means the "
        "problem is inflation rather than demand.",
        20, expects_recommendation=True,
    )
    assert upfront.answer_first
    assert upfront.score > buried.score


def test_number_without_implication_is_flagged():
    d = analyze("It comes out to about 2.3 billion dollars.", 8, expects_number=True)
    assert any("so what" in n for n in d.notes)


def test_number_with_implication_passes():
    d = analyze(
        "It comes out to about 2.3 billion, which means revenue isn't the problem — "
        "they're growing the top line and still losing margin.",
        12, expects_number=True,
    )
    assert not any("so what" in n for n in d.notes)


def test_long_unsignposted_answer_is_flagged():
    rambling = " ".join(["the client has costs and revenue and various considerations"] * 14)
    d = analyze(rambling, 45)
    assert any("signpost" in n.lower() for n in d.notes)


def test_overrunning_the_time_target_is_flagged():
    d = analyze("Revenue and costs are the two areas I want to examine here.", 300,
                target_seconds=120)
    assert any("target" in n for n in d.notes)


# ── scorecard bands ─────────────────────────────────────────────────────── #

@pytest.mark.parametrize("score,stars", [(0.95, 5), (0.80, 4), (0.60, 3), (0.40, 2), (0.10, 1)])
def test_rating_bands(score, stars):
    assert rating(score)[0] == stars


# ── library ─────────────────────────────────────────────────────────────── #

def test_library_loads_cleanly():
    lib = Library()
    assert lib.problems == [], f"case data problems: {lib.problems}"
    assert lib.questions


def test_public_view_never_leaks_the_prompt():
    lib = Library()
    for q in lib.questions.values():
        pub = json.dumps(q.public())
        assert q.prompt[:40] not in pub
        assert "rubric" not in pub
        assert "model_answer" not in pub


def test_briefing_never_leaks_case_facts():
    lib = Library()
    for case in lib.cases.values():
        brief = json.dumps(case.briefing())
        assert case.prompt[:40] not in brief
        for c in case.clarifications:
            assert c["response"][:30] not in brief, "clarification answer leaked into briefing"


def test_rubric_for_matching_has_labels_but_no_weights():
    lib = Library(BUNDLED)
    q = next(q for q in lib.questions.values() if q.rubric.get("kind") == "buckets")
    r = q.rubric_for_matching()
    assert all(set(c) == {"id", "label"} for c in r["components"])


def test_drill_filters_by_type_and_difficulty():
    lib = Library(BUNDLED)
    picked = lib.drill(types=["math"], min_difficulty=4, limit=10)
    assert picked
    assert all(q.type == "math" and q.difficulty >= 4 for q in picked)


def test_drill_bias_favors_weak_areas():
    lib = Library(BUNDLED)
    weak = {"breakeven": 0.05, "capacity": 0.98, "revenue_build": 0.98, "load_factor": 0.98}
    picks = [lib.drill(types=["math"], limit=1, weakness=weak, seed=s)[0].id for s in range(60)]
    assert picks.count("q3") > picks.count("q2")     # q3 carries the weak tag


# ── delivery cannot reward saying nothing ────────────────────────────────── #

def test_an_empty_answer_scores_zero_on_communication():
    assert analyze("", 1).score == 0.0
    assert analyze("Blah, blah, blah.", 10).score <= 0.25


def test_a_short_but_real_answer_is_not_treated_as_empty():
    d = analyze(
        "I want to look at revenue, then split costs into fixed and variable, "
        "then look at what competitors have done on price.", 14)
    assert d.score > 0.8


# ── delivery must not judge typing as if it were speech ──────────────────── #

def test_typing_speed_is_not_treated_as_nerves():
    """A fast typist was told they were "speaking fast (663 wpm)".

    Pace is a speech measure. Applied to a typed answer it is noise, and it
    dragged the communication score down on a genuinely excellent one.
    """
    answer = ("My recommendation is to attack costs, not price. Revenue is 2.3 billion "
              "and growing, but cost per seat mile is up 35 percent, driven by fuel.")
    spoken = analyze(answer, 4, expects_recommendation=True)
    typed = analyze(answer, 4, expects_recommendation=True, typed=True)

    assert any("fast" in n for n in spoken.notes), "spoken pace should still be judged"
    assert not any("fast" in n for n in typed.notes)
    assert typed.score > spoken.score


def test_three_moves_counts_as_signposting():
    """"Three moves: ..." is signposting; the noun list was too narrow."""
    for phrase in ["Three moves: hedge fuel, cut routes, grow ancillary.",
                   "Two options here.", "Three steps.", "Four levers.",
                   "Three priorities.", "Two recommendations."]:
        assert analyze(phrase + " " * 0, 10).signpost_count >= 1, phrase


def test_praise_is_not_reported_as_a_habit_to_fix():
    """"Recurring habits" is the work-on list; a strength there reads as criticism."""
    from casecraft.delivery import CLEAN_DELIVERY
    from casecraft.library import Library
    from casecraft.session import Room, Session

    session = Session(Room(), Library())
    session.load_case(Library().cases["casecraft-orchid-airlines"])
    session.advance()
    for _ in range(4):
        session.score_answer(
            "I want to look at three areas. First, the revenue build. Second, fixed "
            "costs like leases. Third, variable costs such as fuel.", 20,
            covered={"revenue", "fixed_costs", "variable_costs"})

    card = session.debrief()
    assert any(CLEAN_DELIVERY.startswith(h["note"]) for a in [] for h in []) is False
    assert not any("Clean delivery" in h["note"] for h in card["recurring_habits"]), \
        f"praise leaked into the work-on list: {card['recurring_habits']}"


def test_consequence_phrasing_counts_as_the_so_what():
    """"...pushes them into a loss" is the implication, stated as an outcome."""
    for phrase in [
        "Profit goes from plus 213 million to negative 19 million — the price cut "
        "alone pushes them into a loss.",
        "Revenue is growing, so revenue is not the problem here at all.",
        "A 35 percent cost increase wipes out the margin entirely for them.",
    ]:
        d = analyze(phrase, 15, expects_number=True)
        assert d.so_what, f"missed the implication in: {phrase[:60]}"


# ── what the candidate actually reads ────────────────────────────────────── #

def test_the_scorecard_names_strengths_and_gaps():
    """Both fields were declared and never populated — always an empty list."""
    from casecraft.scoring import Scorecard

    card = Scorecard()
    card.add("structure", 0.95)
    card.add("analytics", 0.20)
    out = card.as_dict()
    assert "Structuring the problem" in out["strengths"]
    assert "Quantitative work" in out["gaps"]
    assert out["strengths"] and out["gaps"], "dead fields shipped to the candidate"


def test_heavy_hedging_is_below_the_bar():
    """"I guess maybe we could sort of look at, I think, the costs probably"
    used to score 3/5 — "At the bar. Advances." """
    from casecraft.scoring import rating

    d = analyze("I guess maybe we could sort of look at, I think, the costs probably "
                "and perhaps also possibly the revenue side of things somehow.", 20)
    assert d.hedge_count >= 5
    assert rating(d.score)[0] <= 2, f"scored {rating(d.score)[0]}/5 on {d.score}"


def test_one_habit_is_not_reported_twice():
    """"You hedged 5 times" and "some hedging" are the same habit."""
    from casecraft.delivery import categorise

    heavy = "You hedged 5 times (i guess, i think). At that rate it reads as no view."
    light = "Some hedging (i think, maybe). Minor, but it softens the answer."
    assert categorise(heavy) == categorise(light)


def test_praise_has_no_habit_category():
    from casecraft.delivery import CLEAN_DELIVERY, categorise
    assert categorise(CLEAN_DELIVERY) is None


def test_weak_areas_are_named_in_english():
    """Raw rubric ids — "answer_first", "mece" — mean nothing to a candidate."""
    from casecraft.progress import readable_area
    assert readable_area("answer_first") == "Leading with the answer"
    assert readable_area("mece") == "MECE structuring"
    assert readable_area("some_new_tag") == "Some new tag"


def test_bare_connectors_are_not_numbers():
    """extract_numbers returned a phantom 0.0 for any text containing "and"."""
    assert extract_numbers("revenue and costs and competition") == []
    assert extract_numbers("point taken, costs matter") == []
    assert extract_numbers("one hundred and twenty") == [120.0]


def test_the_final_assertion_decides():
    """"2.3 billion... no wait, 2.89 billion" was graded correct because the
    right number appeared SOMEWHERE. The candidate abandoned it."""
    v = grade_numeric("2.3 billion... no wait, 2.89 billion", Q["q2"]["rubric"])
    assert v.outcome == WRONG and "load factor" in v.note
    v = grade_numeric("2.89 billion... hold on, no — 2.3 billion", Q["q2"]["rubric"])
    assert v.outcome == CORRECT
