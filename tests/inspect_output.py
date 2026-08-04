"""Read what the product actually SAYS, across varied performances.

Assertions prove a call returns. They do not prove the sentence it returns
makes sense to a person. Every bug found by reading output — praise filed under
"habits to fix", an implication scored as missing — was invisible to a green
suite.

This runs scenarios and prints the user-facing text so it can be read.

    .venv/bin/python -m tests.inspect_output
"""

from __future__ import annotations

import json
import os
import textwrap

os.environ.setdefault("CASECRAFT_NO_BROWSER", "1")
os.environ.setdefault("CASECRAFT_DB", "/tmp/casecraft-inspect.db")

from casecraft import progress as progress_store      # noqa: E402
from casecraft.library import Library                 # noqa: E402
from casecraft.session import Room, Session           # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'─' * 74}\n  {title}\n{'─' * 74}")


def wrap(text: str, indent: str = "      ") -> str:
    return textwrap.fill(str(text), 90, initial_indent=indent, subsequent_indent=indent)


ANSWERS = {
    "strong": {
        "structure": ("My hypothesis is this is a cost problem. Three areas: the revenue "
                      "build of passengers times price, fixed costs like leases, and "
                      "variable costs like fuel."),
        "math": ("240 flights times 120 passengers is 28,800 a day, times 220 dollars is "
                 "6.34 million a day, roughly 2.3 billion a year, which means revenue "
                 "is not the problem."),
        "covered": True,
    },
    "weak": {
        "structure": "I guess maybe we could sort of look at, I think, the costs probably.",
        "math": "Uh I think maybe around 900 million or something like that?",
        "covered": False,
    },
}


def run_case(profile: str) -> dict:
    lib = Library()
    session = Session(Room(), lib)
    session.load_case(lib.cases["casecraft-orchid-airlines"])
    a = ANSWERS[profile]

    while True:
        q = session.advance()
        if q is None:
            break
        text = a["math"] if q.type == "math" else a["structure"]
        if q.rubric.get("kind") == "numeric":
            session.score_answer(text, 40)
        else:
            ids = [c["id"] for c in (q.rubric.get("components")
                                     or q.rubric.get("criteria") or [])]
            covered = set(ids[:3]) if a["covered"] else set()
            session.score_answer(text, 40, covered=covered)
        progress_store.log(session.id, session.attempts[-1])
    return session.debrief()


def main() -> None:
    for profile in ("strong", "weak"):
        card = run_case(profile)
        rule(f"SCORECARD — {profile} candidate  (this is what they read)")
        for dim, v in card["dimensions"].items():
            print(f"   {dim:<15} {v['rating']}/5   {v['verdict']}")
        print(f"\n   overall          {card['overall']}")
        print(f"   limiting factor  {card['limiting_factor']}")
        print(f"   strengths        {card['strengths']}")
        print(f"   gaps             {card['gaps']}")
        print(f"   recurring habits:")
        for h in card["recurring_habits"]:
            print(wrap(f"· {h['note']}  (×{h['times']})", "     "))
        print(f"\n   per question:")
        for r in card["per_question"]:
            print(f"     {r['type']:<10} {r['outcome']:<8} {r['score']:<5} "
                  f"error={r['error']}")

    rule("PROGRESS REPORT — what 'what am I bad at?' returns")
    print(json.dumps(progress_store.report(), indent=1)[:1600])

    rule("PROBE TEXT — the nudges, read in order")
    lib = Library()
    for case_id in ("casecraft-orchid-airlines", "darden24-hooville_college"):
        case = lib.cases[case_id]
        print(f"\n   {case.title}")
        for q in case.questions[:2]:
            print(f"     [{q.type}]")
            for p in q.probes:
                print(wrap(f"· {p}", "        "))

    rule("IMPORTED CASE — what a Darden case actually says to a candidate")
    case = lib.cases["darden24-hooville_college"]
    print(wrap(f"prompt: {case.prompt}", "     "))
    for q in case.questions:
        print(f"\n     [{q.type}] {q.difficulty}")
        print(wrap(q.prompt, "        "))
        r = q.rubric
        items = r.get("components") or r.get("criteria") or []
        for c in items[:3]:
            print(wrap(f"– {c['label']}", "           "))


if __name__ == "__main__":
    main()
