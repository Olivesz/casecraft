"""The case library — loading, validating, and slicing the question bank.

The load-bearing idea from SCHEMA.md: **questions are first-class**. A case is a
container that gives its questions shared context, but the practiceable unit is
the question. "Only hard math" is therefore a filter over a flat index, not a
search through documents — which is what makes drilling a weakness possible at
all.

Nothing here ever hands out an answer key. `Question.public()` is the only view
that crosses the MCP boundary before an answer is submitted, and it deliberately
carries no rubric, no model answer, and no prompt text.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from pathlib import Path

# Cases load from every location that exists, in this order. The user's own
# directory comes last so a case they imported can override a bundled one of
# the same id.
#
# This ordering is what makes "ship the engine empty" work: the bundled cases
# are originals we're free to distribute, and anything derived from a
# copyrighted casebook stays in ~/.casecraft/cases on the machine that made it.
_BUNDLED = Path(__file__).resolve().parent.parent / "data" / "cases"
_USER = Path(os.environ.get("CASECRAFT_CASES", Path.home() / ".casecraft" / "cases"))
CASE_DIRS = [_BUNDLED, _USER]
DATA_DIR = _BUNDLED   # back-compat for callers that want just the bundled set

# Question types, ordered as they occur in a real case.
STRUCTURE, MATH, EXHIBIT, BRAINSTORM, SYNTHESIS = (
    "structure", "math", "exhibit", "brainstorm", "synthesis",
)
ALL_TYPES = (STRUCTURE, MATH, EXHIBIT, BRAINSTORM, SYNTHESIS)

# Which MBB scorecard dimension each question type primarily loads onto.
PRIMARY_DIMENSION = {
    STRUCTURE: "structure",
    MATH: "analytics",
    EXHIBIT: "analytics",
    BRAINSTORM: "judgment",
    SYNTHESIS: "judgment",
}


@dataclass
class Question:
    id: str
    case_id: str
    case_title: str
    order: int
    type: str
    difficulty: int
    tags: list[str]
    prompt: str
    rubric: dict
    probes: list[str]
    model_answer: str
    time_target_sec: int
    exhibit_id: str | None = None
    context: str = ""          # standalone framing, for drilling out of case order

    @property
    def uid(self) -> str:
        return f"{self.case_id}/{self.id}"

    @property
    def dimension(self) -> str:
        return PRIMARY_DIMENSION.get(self.type, "judgment")

    def public(self) -> dict:
        """The only pre-answer view. Note what is absent: the prompt itself.

        The prompt is spoken by the room, never returned through MCP — that is
        the entire reason the candidate can't read ahead.
        """
        return {
            "uid": self.uid,
            "case_title": self.case_title,
            "type": self.type,
            "difficulty": self.difficulty,
            "tags": self.tags,
            "time_target_sec": self.time_target_sec,
            "has_exhibit": self.exhibit_id is not None,
        }

    def rubric_for_matching(self) -> dict:
        """Component labels only — released *after* an answer is committed.

        Used by the host model to decide what the candidate covered. Weights and
        must-have flags stay server-side so the verdict can't be argued with.
        """
        if self.rubric.get("kind") == "buckets":
            items = self.rubric.get("components", []) + self.rubric.get("bonus", [])
        elif self.rubric.get("kind") == "open":
            items = self.rubric.get("criteria", [])
        else:
            return {}
        return {
            "kind": self.rubric["kind"],
            "components": [{"id": c["id"], "label": c["label"]} for c in items],
        }


@dataclass
class Case:
    id: str
    title: str
    meta: dict
    prompt: str
    clarifications: list[dict]
    exhibits: dict[str, dict]
    questions: list[Question] = field(default_factory=list)

    @property
    def format(self) -> str:
        return self.meta.get("format", "interviewer_led")

    def briefing(self) -> dict:
        """What the host model is told when a case starts.

        Enough to run the room — never enough to answer. No prompt text, no
        numbers, no rubric. The model is a host, not a participant.
        """
        return {
            "case_id": self.id,
            "title": self.title,
            "format": self.format,
            "firm_style": self.meta.get("firm_style", "generic"),
            "case_type": self.meta.get("case_type"),
            "industry": self.meta.get("industry"),
            "difficulty": self.meta.get("difficulty"),
            "expected_minutes": self.meta.get("expected_minutes"),
            "question_count": len(self.questions),
            "question_types": [q.type for q in self.questions],
            "clarification_topics": [
                {"id": c["id"], "topic": c["topic"]} for c in self.clarifications
            ],
        }

    def match_clarification(self, asked: str) -> dict | None:
        """Keyword pre-match for a candidate's clarifying question.

        Deliberately crude: the host model does the real semantic matching
        against `clarification_topics`, which it already has. This is the fast
        path so obvious questions ("where do they operate?") never need a round
        trip.
        """
        low = asked.lower()
        best, best_hits = None, 0
        for c in self.clarifications:
            hits = sum(1 for kw in c.get("match", []) if kw.lower() in low)
            if hits > best_hits:
                best, best_hits = c, hits
        return best if best_hits else None


class Library:
    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.dirs = [Path(data_dir)] if data_dir else CASE_DIRS
        self.cases: dict[str, Case] = {}
        self.questions: dict[str, Question] = {}
        self.problems: list[str] = []
        self.reload()

    def reload(self) -> None:
        self.cases.clear()
        self.questions.clear()
        self.problems.clear()
        for directory in self.dirs:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    self._load(json.loads(path.read_text()))
                except Exception as exc:                   # noqa: BLE001
                    self.problems.append(f"{path.name}: {exc}")

    def _load(self, raw: dict) -> None:
        case = Case(
            id=raw["id"],
            title=raw["title"],
            meta=raw.get("meta", {}),
            prompt=raw["prompt"]["text"] if isinstance(raw.get("prompt"), dict) else raw.get("prompt", ""),
            clarifications=raw.get("clarifications", []),
            exhibits={e["id"]: e for e in raw.get("exhibits", [])},
        )
        for i, q in enumerate(raw.get("questions", [])):
            if not q.get("rubric"):
                self.problems.append(f"{case.id}/{q.get('id')}: no rubric — unscoreable")
                continue
            question = Question(
                id=q["id"],
                case_id=case.id,
                case_title=case.title,
                order=q.get("order", i + 1),
                type=q["type"],
                difficulty=q.get("difficulty", case.meta.get("difficulty", 3)),
                tags=q.get("tags", []),
                prompt=q["prompt"],
                rubric=q["rubric"],
                probes=q.get("probes", []),
                model_answer=q.get("model_answer") or q["rubric"].get("model_answer", ""),
                time_target_sec=q.get("time_target_sec", 120),
                exhibit_id=q.get("exhibit_id"),
                context=q.get("context", ""),
            )
            case.questions.append(question)
            self.questions[question.uid] = question
        case.questions.sort(key=lambda q: q.order)
        self.cases[case.id] = case

    # ── discovery ────────────────────────────────────────────────────────── #

    def catalog(self) -> dict:
        """What's available to practice. Safe to show — counts, never content."""
        by_type: dict[str, int] = {}
        by_tag: dict[str, int] = {}
        by_difficulty: dict[int, int] = {}
        for q in self.questions.values():
            by_type[q.type] = by_type.get(q.type, 0) + 1
            by_difficulty[q.difficulty] = by_difficulty.get(q.difficulty, 0) + 1
            for t in q.tags:
                by_tag[t] = by_tag.get(t, 0) + 1
        return {
            "cases": [
                {
                    "id": c.id,
                    "title": c.title,
                    "format": c.format,
                    "firm_style": c.meta.get("firm_style"),
                    "case_type": c.meta.get("case_type"),
                    "industry": c.meta.get("industry"),
                    "difficulty": c.meta.get("difficulty"),
                    "minutes": c.meta.get("expected_minutes"),
                    "questions": len(c.questions),
                }
                for c in self.cases.values()
            ],
            "drill_types": by_type,
            "difficulties": dict(sorted(by_difficulty.items())),
            "tags": dict(sorted(by_tag.items(), key=lambda kv: -kv[1])),
            "total_questions": len(self.questions),
            "problems": self.problems,
        }

    def drill(
        self,
        *,
        types: list[str] | None = None,
        min_difficulty: int = 1,
        max_difficulty: int = 5,
        tags: list[str] | None = None,
        case_types: list[str] | None = None,
        limit: int = 5,
        weakness: dict[str, float] | None = None,
        seed: int | None = None,
    ) -> list[Question]:
        """Select drill questions, optionally biased toward known weak spots.

        `weakness` maps a tag or question type to a 0–1 mastery score. Selection
        weights each question by its worst matching mastery, so the sampler
        drifts toward what you keep getting wrong without ever fully excluding
        the rest — pure worst-first practice is demoralizing and overfits to one
        skill.
        """
        pool = [
            q for q in self.questions.values()
            if (not types or q.type in types)
            and min_difficulty <= q.difficulty <= max_difficulty
            and (not tags or any(t in q.tags for t in tags))
            and (not case_types or self.cases[q.case_id].meta.get("case_type") in case_types)
        ]
        if not pool:
            return []

        rng = random.Random(seed)
        if not weakness:
            rng.shuffle(pool)
            return pool[:limit]

        def weight(q: Question) -> float:
            scores = [weakness[k] for k in ([q.type] + q.tags) if k in weakness]
            mastery = min(scores) if scores else 0.5   # unseen ≈ neutral
            return 0.15 + (1.0 - mastery) ** 2         # floor keeps variety alive

        chosen: list[Question] = []
        remaining = pool[:]
        for _ in range(min(limit, len(remaining))):
            weights = [weight(q) for q in remaining]
            pick = rng.choices(remaining, weights=weights, k=1)[0]
            chosen.append(pick)
            remaining.remove(pick)
        return chosen
