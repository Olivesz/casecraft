"""Grading calibration: score tiered answers, assert monotonicity, read verdicts.

A grader that ranks a worse answer above a better one destroys trust faster
than any crash. This scores five tiers per rubric family and checks ordering,
plus numeric edge inputs speech actually produces.

    .venv/bin/python -m tests.calibrate
"""
from __future__ import annotations

import sys

from casecraft.delivery import analyze
from casecraft.library import Library
from casecraft.scoring import grade_buckets, grade_numeric

problems: list[str] = []


def flag(cond: bool, msg: str) -> None:
    if not cond:
        problems.append(msg)


def main() -> int:
    lib = Library(None)
    orchid = lib.cases["casecraft-orchid-airlines"]
    q1, q2 = orchid.questions[0], orchid.questions[1]

    # ── numeric tiers on q2 (expected ≈ 2.31B) ──────────────────────────
    tiers = [
        ("exact", "2,312,640,000 dollars a year", 1.0),
        ("rounded", "roughly 2.3 billion a year", 1.0),
        ("worded", "about two point three billion", 1.0),
        ("named-error", "about 2.89 billion", 0.25),
        ("partial-steps", "240 flights, 120 passengers each... then I lost it, maybe 900 million", None),
        ("wrong", "call it 5 trillion", 0.0),
        ("nothing", "I honestly do not know where to start", 0.0),
    ]
    scores = {}
    for name, text, want in tiers:
        v = grade_numeric(text, q2.rubric)
        scores[name] = v.score
        if want is not None:
            flag(abs(v.score - want) < 1e-6, f"numeric {name}: score {v.score}, wanted {want}")
    flag(scores["partial-steps"] > scores["wrong"],
         f"partial credit ({scores['partial-steps']}) must beat flat wrong ({scores['wrong']})")
    flag(scores["named-error"] > scores["wrong"],
         "a diagnosed mistake must beat an undiagnosed one")

    # numeric edge inputs speech produces
    edges = [
        ("2,300 million a year", 1.0),           # mixed scale
        ("$2.31bn", 1.0),                        # compact suffix
        ("2.3 billion... no wait, 2.89 billion", 0.25),  # final assertion decides
        ("2.89 billion... no wait, 2.3 billion", 1.0),    # corrected TO right = right
        ("it's between 2 and 3 billion", 0.0),   # a range is not an answer
    ]
    for text, want in edges:
        v = grade_numeric(text, q2.rubric)
        flag(v.score == want, f"numeric edge {text!r}: {v.score} wanted {want} ({v.note[:40]})")

    # ── bucket tiers on q1 ──────────────────────────────────────────────
    ordering = [
        ("everything", {"revenue", "fixed_costs", "variable_costs", "competition", "mece", "hypothesis"}),
        ("all-required", {"revenue", "fixed_costs", "variable_costs"}),
        ("missing-one-must", {"revenue", "fixed_costs", "competition"}),
        ("one-bucket", {"revenue"}),
        ("nothing", set()),
    ]
    prev = None
    for name, covered in ordering:
        v = grade_buckets(covered, q1.rubric)
        if prev is not None:
            flag(v.score <= prev + 1e-9, f"buckets NOT monotonic at {name}: {v.score} > {prev}")
        prev = v.score

    # ── delivery tiers ──────────────────────────────────────────────────
    strong = analyze("Three areas. First, the revenue build. Second, fixed costs like "
                     "leases. Third, variable costs, mainly fuel.", 25)
    hedgy = analyze("I think maybe we could sort of look at revenue, and perhaps "
                    "possibly costs too, I guess.", 25)
    empty = analyze("yeah so um", 25)
    flag(strong.score > hedgy.score > empty.score,
         f"delivery tiers out of order: {strong.score} / {hedgy.score} / {empty.score}")

    print(f"{len(problems)} calibration problems")
    for m in problems:
        print(f"  • {m}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
