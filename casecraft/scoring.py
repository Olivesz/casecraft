"""Grading a spoken answer against a rubric.

Two very different jobs live here:

  * `grade_numeric`  — fully deterministic. No model call, no latency. The whole
    reason math feedback can land the instant the candidate stops talking.
  * `grade_buckets`  — needs a model to decide *which* rubric components an
    answer covered (people say "how much they charge", not "price per unit").
    The model returns component ids; the scoring policy below turns those ids
    into a verdict. That split keeps the judgement testable and the model's job
    narrow.

The hard part of the numeric path isn't the arithmetic — it's that the input is
speech. Whisper hands us "about two point three billion", "$2.3B", "2,312,640,000"
and "roughly 2.3 bn" for the same answer, so a transcript has to be mined for
candidate numbers before anything can be compared.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ─────────────────────────── verdicts ─────────────────────────── #

CORRECT = "correct"      # move on, give positive feedback
PARTIAL = "partial"      # right track, probe toward what's missing
WRONG = "wrong"          # off track, probe from the weakest hint


@dataclass
class Verdict:
    outcome: str                          # CORRECT | PARTIAL | WRONG
    score: float                          # 0.0–1.0, for progress tracking
    hit: list[str] = field(default_factory=list)     # rubric component/step ids covered
    missed: list[str] = field(default_factory=list)  # ids not covered
    error_id: str | None = None           # named mistake from `common_errors`
    note: str = ""                        # one line shown to the candidate


# ─────────────────────── spoken-number parsing ─────────────────────── #

_SCALES = {
    "k": 1e3, "thousand": 1e3,
    "m": 1e6, "mm": 1e6, "million": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
    "t": 1e12, "trillion": 1e12,
}

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}

# "2.3", "2,312,640,000", "220" — optionally $-prefixed, optionally negative.
_NUM_RE = re.compile(
    r"(?P<neg>negative\s+|minus\s+|-)?\$?\s*(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"\s*(?P<scale>k|mm|m|bn|b|t|thousand|million|billion|trillion)?\b",
    re.IGNORECASE,
)

# "two point three billion", "twenty eight thousand"
_WORD_RE = re.compile(
    r"(?P<neg>negative\s+|minus\s+)?"
    r"(?P<words>(?:" + "|".join(_UNITS) + r"|hundred|and|point)"
    r"(?:\s+(?:" + "|".join(_UNITS) + r"|hundred|and|point))*)"
    r"\s*(?P<scale>thousand|million|billion|trillion)?\b",
    re.IGNORECASE,
)


def _words_to_number(words: str) -> float | None:
    """'two point three' -> 2.3, 'twenty eight' -> 28. Returns None if unparseable.

    Bare connectors are NOT numbers: the regex also matches lone "and"/"point",
    and treating those as 0.0 meant every transcript containing "and" asserted
    a phantom zero — so "no number given" answers were mis-routed.
    """
    whole_words, _, frac_words = words.lower().partition("point")
    total, chunk = 0.0, 0.0
    matched_any = False
    for tok in whole_words.split():
        if tok == "and":
            continue
        if tok == "hundred":
            chunk = (chunk or 1) * 100
            matched_any = True
        elif tok in _UNITS:
            chunk += _UNITS[tok]
            matched_any = True
        else:
            return None
    total += chunk

    if frac_words.strip():
        digits = ""
        for tok in frac_words.split():
            if tok not in _UNITS or _UNITS[tok] > 9:
                return None
            digits += str(_UNITS[tok])
            matched_any = True
        total += float("0." + digits) if digits else 0.0
    return total if matched_any else None


def extract_numbers(text: str) -> list[float]:
    """Every number a transcript could plausibly be asserting, in spoken order.

    Deliberately greedy: a candidate walking through their work says several
    numbers, and the final answer is usually — but not always — the last one.
    Grading tries all of them against the expected value and the known wrong
    answers, which is both more forgiving and more diagnostic than assuming
    position.
    """
    found: list[tuple[int, float]] = []

    for m in _NUM_RE.finditer(text):
        value = float(m.group("num").replace(",", ""))
        if scale := m.group("scale"):
            value *= _SCALES[scale.lower()]
        if m.group("neg"):
            value = -value
        found.append((m.start(), value))

    for m in _WORD_RE.finditer(text):
        value = _words_to_number(m.group("words"))
        if value is None:
            continue
        if scale := m.group("scale"):
            value *= _SCALES[scale.lower()]
        if m.group("neg"):
            value = -value
        found.append((m.start(), value))

    found.sort()
    return [v for _, v in found]


# ─────────────────────────── numeric grading ─────────────────────────── #

def _close(a: float, b: float, tolerance_pct: float) -> bool:
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) <= abs(b) * (tolerance_pct / 100.0)


def grade_numeric(transcript: str, rubric: dict) -> Verdict:
    """Deterministic grade for `kind: "numeric"`. No model, no network, no wait.

    Order matters: check the right answer, then the *named* wrong answers, then
    the intermediate steps. A named wrong answer is worth more than a generic
    "incorrect" — it turns feedback from a correction into a diagnosis.
    """
    said = extract_numbers(transcript)
    expected = float(rubric["expected"])
    tol = float(rubric.get("tolerance_pct", 2))

    # The candidate's answer is the LAST number that matches any known target.
    # Someone narrating their work ends on the conclusion, and someone who
    # says the right figure then "corrects" themselves to a wrong one has
    # answered wrong — scanning for the right number anywhere gave credit for
    # answers the candidate explicitly abandoned.
    for n in reversed(said):
        if _close(n, expected, tol):
            steps = [s["id"] for s in rubric.get("steps", [])]
            return Verdict(CORRECT, 1.0, hit=steps, note="Correct.")
        for err in rubric.get("common_errors", []):
            if _close(n, float(err["value"]), tol):
                return Verdict(
                    WRONG, 0.25,
                    error_id=err.get("id") or str(err["value"]),
                    note=err["diagnosis"],
                )

    # Partial credit: which intermediate steps did they get to before going wrong?
    hit = [s["id"] for s in rubric.get("steps", [])
           if any(_close(n, float(s["value"]), tol) for n in said)]
    missed = [s["id"] for s in rubric.get("steps", []) if s["id"] not in hit]

    if hit:
        total = len(rubric.get("steps", [])) or 1
        return Verdict(
            PARTIAL, round(len(hit) / total * 0.8, 2), hit=hit, missed=missed,
            note="The setup is right — the answer diverges further down.",
        )

    if not said:
        return Verdict(WRONG, 0.0, missed=missed,
                       note="I didn't catch a number in that — what did you get?")

    return Verdict(WRONG, 0.0, missed=missed, note="That's not the number I have.")


# ─────────────────────────── bucket grading ─────────────────────────── #

"""Bucket / criteria policy.

Three calls are baked in here, all tunable at the top of the function:

  1. `must_have` is near-absolute — missing one caps you at PARTIAL regardless
     of weight. This is authentic: an interviewer will not move on from a
     profitability case where you never mentioned costs, no matter how sharp
     the rest was. It also makes probing meaningful, since there is always a
     specific thing to probe toward.
  2. Bonus weight is excluded from the denominator and added on top, capped at
     1.0. So a complete-but-unshowy answer can still score 1.0, and flourishes
     can lift a good answer without rescuing a hollow one.
  3. PASS at 0.75, PARTIAL down to 0.35. Deliberately not lower — a tool that
     rubber-stamps vague answers is worse than no tool, because it trains
     exactly the habit that gets people dinged.
"""

PASS_AT = 0.75
PARTIAL_AT = 0.35


def _apply(covered: set[str], required: list[dict], bonus: list[dict]) -> Verdict:
    total = sum(c.get("weight", 1) for c in required) or 1
    earned = sum(c.get("weight", 1) for c in required if c["id"] in covered)

    bonus_earned = sum(b.get("weight", 1) for b in bonus if b["id"] in covered)
    bonus_total = sum(b.get("weight", 1) for b in bonus) or 1
    lift = 0.10 * (bonus_earned / bonus_total) if bonus else 0.0

    frac = min(1.0, earned / total + lift)

    missed_musts = [c for c in required if c.get("must_have") and c["id"] not in covered]
    # Biggest gap first, so the caller's probe targets what's most worth having.
    missed = sorted(
        (c for c in required if c["id"] not in covered),
        key=lambda c: (not c.get("must_have"), -c.get("weight", 1)),
    )

    if missed_musts:
        outcome = PARTIAL if frac >= PARTIAL_AT else WRONG
        gap = missed_musts[0]["label"]
        note = f"Core gap: {gap}."
    elif frac >= PASS_AT:
        outcome, note = CORRECT, "Solid — the essential pieces are all there."
    elif frac >= PARTIAL_AT:
        outcome = PARTIAL
        note = f"On track, but incomplete — {missed[0]['label']}." if missed else "Incomplete."
    else:
        outcome, note = WRONG, "That misses most of what I'd want to hear."

    return Verdict(
        outcome, round(frac, 2),
        hit=[c["id"] for c in required if c["id"] in covered]
            + [b["id"] for b in bonus if b["id"] in covered],
        missed=[c["id"] for c in missed],
        note=note,
    )


def grade_buckets(covered: set[str], rubric: dict) -> Verdict:
    """Score a framework/brainstorm answer from the component ids it covered.

    `covered` comes from the host model, which sees only component *labels* and
    the committed transcript. No NLP happens here — this is pure policy, which
    is precisely why it lives in code: the verdict is then deterministic,
    testable, and identical for every candidate.
    """
    return _apply(covered, rubric.get("components", []), rubric.get("bonus", []))


def grade_open(covered: set[str], rubric: dict) -> Verdict:
    """Score a synthesis/recommendation answer against its criteria."""
    return _apply(covered, rubric.get("criteria", []), [])


def grade(question_rubric: dict, *, transcript: str = "", covered: set[str] | None = None) -> Verdict:
    """Dispatch on rubric kind. Numeric never needs `covered`; the rest require it."""
    kind = question_rubric.get("kind")
    if kind == "numeric":
        return grade_numeric(transcript, question_rubric)
    if kind == "buckets":
        return grade_buckets(covered or set(), question_rubric)
    if kind == "open":
        return grade_open(covered or set(), question_rubric)
    raise ValueError(f"unknown rubric kind: {kind!r}")


# ─────────────────────────── the scorecard ─────────────────────────── #

# The four boxes on a real MBB feedback form. Every firm words them slightly
# differently — McKinsey's "Problem Structuring / Analytical / Synthesis" and
# Bain's "Structure / Analytics / Judgment / Presence" are the same four axes —
# so scoring on these transfers regardless of which firm you're targeting.
DIMENSIONS = ("structure", "analytics", "judgment", "communication")

# 3 is the bar. Below it you don't advance; above it you're differentiating.
_BANDS = [
    (0.90, 5, "Exceptional — clearly above the bar for a final round."),
    (0.75, 4, "Strong — comfortably at or above the bar."),
    (0.55, 3, "At the bar. Advances, but nothing here differentiates you."),
    (0.35, 2, "Below the bar. This is where the round is usually lost."),
    (0.00, 1, "Well below the bar — needs rebuilding, not polishing."),
]


def rating(score: float) -> tuple[int, str]:
    for threshold, stars, blurb in _BANDS:
        if score >= threshold:
            return stars, blurb
    return 1, _BANDS[-1][2]


@dataclass
class Scorecard:
    dimensions: dict[str, float] = field(default_factory=dict)   # 0–1 per dimension
    counts: dict[str, int] = field(default_factory=dict)
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def add(self, dimension: str, score: float) -> None:
        n = self.counts.get(dimension, 0)
        prev = self.dimensions.get(dimension, 0.0)
        self.dimensions[dimension] = (prev * n + score) / (n + 1)
        self.counts[dimension] = n + 1

    def as_dict(self) -> dict:
        out = {}
        for dim in DIMENSIONS:
            if dim not in self.dimensions:
                continue
            stars, blurb = rating(self.dimensions[dim])
            out[dim] = {
                "score": round(self.dimensions[dim], 2),
                "rating": stars,
                "verdict": blurb,
                "observations": self.counts[dim],
            }
        rated = [v["rating"] for v in out.values()]
        # These were declared and never populated — the candidate read an empty
        # "strengths" list after a strong interview.
        readable = {"structure": "Structuring the problem",
                    "analytics": "Quantitative work",
                    "judgment": "Business judgment and insight",
                    "communication": "Communication and presence"}
        strengths = [readable[d] for d, v in out.items() if v["rating"] >= 4]
        gaps = [readable[d] for d, v in out.items() if v["rating"] <= 2]
        return {
            "dimensions": out,
            "overall": round(sum(rated) / len(rated), 1) if rated else None,
            # Real interviewers decide on the weakest box, not the average —
            # one 2 sinks a candidate with three 4s.
            "limiting_factor": min(out, key=lambda d: out[d]["rating"]) if out else None,
            "strengths": strengths or self.strengths,
            "gaps": gaps or self.gaps,
        }
