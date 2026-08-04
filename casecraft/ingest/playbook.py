"""Parser for the Case Playbook math drill sets.

Layout is two halves: numbered questions grouped into Sets, then a matching
"Set N Answers" half with worked solutions. Each question also self-labels its
kind ("Type of problem: Unit Conversion"), which is unusually generous — it
gives real tags for drill filtering without any inference.

Everything here becomes a `math` question in one synthetic case per set, since
these are drills with no shared client context.
"""

from __future__ import annotations

import re

from . import build
from .darden import _slug, _tidy, parse_calculation
from .extract import Page

SET_HEAD = re.compile(r"^\s*Set\s+(\d+)\s*$", re.M | re.I)
ANSWERS_HEAD = re.compile(r"^\s*Set\s+(\d+)\s+Answers\s*$", re.M | re.I)
QUESTION_HEAD = re.compile(r"^\s*Question\s+(\d+)\s*:", re.M | re.I)
ANSWER_HEAD = re.compile(r"^\s*Question\s+(\d+)\s*[-–—]\s*(?P<kind>[A-Za-z /&\-]+?)\s*$", re.M | re.I)

ASK = re.compile(r"\bQuestion\s*:\s*(?P<ask>.+?)(?=\s*(?:Follow[- ]?Up\s*:|Type of problem\s*:|$))",
                 re.I | re.S)
FOLLOWUP = re.compile(r"\bFollow[- ]?Up\s*:\s*(?P<text>.+?)(?=\s*(?:Type of problem\s*:|$))", re.I | re.S)
TYPE_OF = re.compile(r"\bType of problem\s*:\s*(?P<kind>[^\n]{3,50})", re.I)


def parse(pages: list[Page], *, book: str, book_id: str) -> tuple[list[dict], list[str]]:
    """Return (cases, review_notes) — one case per question set."""
    notes: list[str] = []
    text = "\n".join(p.text for p in pages)

    answers_start = ANSWERS_HEAD.search(text)
    if not answers_start:
        return [], ["playbook: no 'Set N Answers' section found — cannot pair solutions"]

    questions_half = text[:answers_start.start()]
    answers_half = text[answers_start.start():]

    solutions = _parse_solutions(answers_half)
    sets = _parse_question_sets(questions_half)

    cases: list[dict] = []
    for set_no, items in sorted(sets.items()):
        built: list[dict] = []
        for number, block in sorted(items.items()):
            key = (set_no, number)
            solution = solutions.get(key)
            if not solution:
                notes.append(f"playbook set {set_no} Q{number}: no matching solution — skipped")
                continue

            ask = ASK.search(block)
            body = block[:ask.start()] if ask else block
            question_text = _tidy(body)
            if ask:
                question_text = f"{question_text} {_tidy(ask.group('ask'))}".strip()
            if len(question_text) < 60:
                notes.append(f"playbook set {set_no} Q{number}: question text too short — skipped")
                continue

            numeric = parse_calculation("Solution\n" + solution["text"])
            if not numeric:
                notes.append(f"playbook set {set_no} Q{number}: no worked solution found — skipped")
                continue

            kind = solution["kind"] or _type_of(block) or "arithmetic"
            follow = FOLLOWUP.search(block)
            worked = _tidy(solution["text"])[:1500]

            # The playbook writes each problem as ask + follow-up, and the
            # worked solution answers both. Splitting them off left the
            # follow-ups parsed but never asked — dead data. Fold them back in.
            if follow:
                question_text = (question_text.rstrip() + "\n\nOnce you have that, "
                                 "a follow-up: " + _tidy(follow.group("text")))
            question = build.math_question(
                f"q{number}", number, question_text[:1600], worked, numeric,
                difficulty=3, tags=[_slug(kind), "drill"], context="Quick math drill.")
            question["time_target_sec"] = 240 if follow else 180
            built.append(question)

        if len(built) < 2:
            notes.append(f"playbook set {set_no}: only {len(built)} usable questions — set skipped")
            continue

        cases.append({
            "id": f"{book_id}-set-{set_no}",
            "title": f"Math Drill Set {set_no}",
            "source": {"casebook": book, "note":
                       "Imported from a copyrighted source. Local use only — do not redistribute."},
            "meta": {
                "format": "interviewer_led", "firm_style": "generic",
                "case_type": "math_drill", "industry": "general",
                "difficulty": 3, "expected_minutes": 5 * len(built),
                "tags": ["math_drill", "arithmetic"], "imported": True,
            },
            "prompt": {"text": f"This is math drill set {set_no}. "
                               f"I'll read you {len(built)} problems. "
                               f"Work each one out loud and give me your answer.",
                       "read_aloud": True},
            "clarifications": [],
            "exhibits": [],
            "questions": built,
            "_review": notes,
        })

    return cases, notes


def _parse_question_sets(text: str) -> dict[int, dict[int, str]]:
    sets: dict[int, dict[int, str]] = {}
    marks = [(m.start(), int(m.group(1))) for m in SET_HEAD.finditer(text)]
    if not marks:
        marks = [(0, 1)]
    for i, (start, set_no) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        sets[set_no] = _split_questions(text[start:end])
    return sets


def _split_questions(chunk: str) -> dict[int, str]:
    out: dict[int, str] = {}
    marks = [(m.start(), int(m.group(1))) for m in QUESTION_HEAD.finditer(chunk)]
    for i, (start, number) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(chunk)
        body = QUESTION_HEAD.sub("", chunk[start:end], count=1)
        out[number] = body.strip()
    return out


def _parse_solutions(text: str) -> dict[tuple[int, int], dict]:
    """Map (set, question) -> worked solution."""
    out: dict[tuple[int, int], dict] = {}
    set_marks = [(m.start(), int(m.group(1))) for m in ANSWERS_HEAD.finditer(text)]
    for i, (start, set_no) in enumerate(set_marks):
        end = set_marks[i + 1][0] if i + 1 < len(set_marks) else len(text)
        chunk = text[start:end]
        marks = [(m.start(), int(m.group(1)), m.group("kind").strip())
                 for m in ANSWER_HEAD.finditer(chunk)]
        for j, (qstart, number, kind) in enumerate(marks):
            qend = marks[j + 1][0] if j + 1 < len(marks) else len(chunk)
            out[(set_no, number)] = {"kind": kind, "text": chunk[qstart:qend].strip()}
    return out


def _type_of(block: str) -> str | None:
    m = TYPE_OF.search(block)
    return m.group("kind").strip() if m else None
