"""Persistent attempt log — the thing that turns practice into targeted practice.

One row per graded answer. Everything the drill sampler needs to know what
you're bad at is derivable from this table, so there's no separate "skill model"
to keep in sync.

The interesting column is `error_id`. Scores tell you *that* you're weak at
capacity math; `error_id` tells you that you drop the load factor specifically,
every time. That's the difference between a progress bar and a coach.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

# Overridable so tests never write into a real practice history — a polluted
# weakness model would quietly skew which questions you get served for weeks.
DB_PATH = Path(os.environ.get("CASECRAFT_DB", Path.home() / ".casecraft" / "progress.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            REAL    NOT NULL,
    session_id    TEXT    NOT NULL,
    uid           TEXT    NOT NULL,
    question_type TEXT    NOT NULL,
    dimension     TEXT    NOT NULL,
    difficulty    INTEGER NOT NULL,
    tags          TEXT    NOT NULL,
    score         REAL    NOT NULL,
    outcome       TEXT    NOT NULL,
    error_id      TEXT,
    seconds       REAL,
    probes_used   INTEGER DEFAULT 0,
    delivery_score REAL,
    transcript    TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_uid  ON attempts(uid);
CREATE INDEX IF NOT EXISTS idx_attempts_type ON attempts(question_type);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def log(session_id: str, attempt) -> None:  # noqa: ANN001 — session.Attempt
    v = attempt.verdict
    with _connect() as con:
        con.execute(
            "INSERT INTO attempts (at, session_id, uid, question_type, dimension, "
            "difficulty, tags, score, outcome, error_id, seconds, probes_used, "
            "delivery_score, transcript) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(), session_id, attempt.uid, attempt.question_type,
                attempt.dimension, attempt.difficulty, json.dumps(attempt.tags),
                v.score if v else 0.0, v.outcome if v else "unanswered",
                v.error_id if v else None, attempt.seconds, attempt.probes_used,
                attempt.delivery.get("score"), attempt.transcript,
            ),
        )


def weakness() -> dict[str, float]:
    """Mastery per question-type and per-tag, on 0–1. Feeds the drill sampler.

    Recent attempts count more — a habit you fixed three weeks ago shouldn't
    keep dominating what you get served. Weight decays over roughly a month.
    """
    now = time.time()
    buckets: dict[str, list[tuple[float, float]]] = {}
    with _connect() as con:
        for row in con.execute(
            "SELECT question_type, tags, score, at FROM attempts "
            "ORDER BY at DESC LIMIT 500"
        ):
            age_days = (now - row["at"]) / 86400
            w = 0.5 ** (age_days / 30)
            for key in [row["question_type"], *json.loads(row["tags"])]:
                buckets.setdefault(key, []).append((row["score"], w))

    return {
        key: sum(s * w for s, w in pairs) / sum(w for _, w in pairs)
        for key, pairs in buckets.items()
        if sum(w for _, w in pairs) > 0
    }


# Tags are internal rubric ids. Shown raw, "answer_first" and "mece" are not
# skills a candidate recognises as their own weakness.
_READABLE = {
    "mece": "MECE structuring", "answer_first": "Leading with the answer",
    "hypothesis": "Hypothesis-driven thinking", "breakeven": "Break-even analysis",
    "margin": "Margin analysis", "sensitivity": "Sensitivity analysis",
    "capacity": "Capacity maths", "revenue_build": "Building up revenue",
    "load_factor": "Utilisation and load factors", "market_sizing": "Market sizing",
    "chart_reading": "Reading exhibits", "cost_structure": "Cost structure",
    "profit_equation": "The profit equation", "risks": "Naming risks",
    "recommendation": "Making a recommendation", "synthesis": "Synthesis",
    "structure": "Structuring", "math": "Quantitative work",
    "exhibit": "Exhibit interpretation", "brainstorm": "Brainstorming breadth",
    "unit_economics": "Unit economics", "contribution_margin": "Contribution margin",
    "arithmetic": "Mental arithmetic", "drill": "Drill questions",
}


def _mistake_label(error_id: str) -> str:
    """`error_id` falls back to the wrong figure when a case names no id.

    A bare "2890800000" in a progress report is meaningless to a person, so
    render it as the number it is until the case data gives it a name.
    """
    if error_id and error_id.replace("-", "").replace(".", "").isdigit():
        return f"recurring wrong answer ({float(error_id):,.0f})"
    return (error_id or "").replace("_", " ").capitalize()


def readable_area(tag: str) -> str:
    return _READABLE.get(tag, tag.replace("_", " ").capitalize())


def report(limit: int = 8) -> dict:
    """Human-facing progress summary — weakest areas and repeated mistakes."""
    with _connect() as con:
        total = con.execute("SELECT COUNT(*) c FROM attempts").fetchone()["c"]
        if not total:
            return {"attempts": 0, "message": "No practice logged yet."}

        by_type = [
            dict(r) for r in con.execute(
                "SELECT question_type, COUNT(*) n, ROUND(AVG(score), 2) avg_score, "
                "ROUND(AVG(delivery_score), 2) avg_delivery "
                "FROM attempts GROUP BY question_type ORDER BY avg_score ASC"
            )
        ]
        repeated_rows = [
            dict(r) for r in con.execute(
                "SELECT error_id, COUNT(*) n FROM attempts "
                "WHERE error_id IS NOT NULL GROUP BY error_id "
                "HAVING n >= 2 ORDER BY n DESC LIMIT ?", (limit,)
            )
        ]
        repeated = [{"mistake": _mistake_label(r["error_id"]), "times": r["n"],
                     "error_id": r["error_id"]} for r in repeated_rows]
        recent = [
            dict(r) for r in con.execute(
                "SELECT uid, question_type, score, outcome, at FROM attempts "
                "ORDER BY at DESC LIMIT 10"
            )
        ]

    w = weakness()
    weakest = sorted(w.items(), key=lambda kv: kv[1])[:limit]
    return {
        "attempts": total,
        "by_question_type": by_type,
        "weakest_areas": [{"area": readable_area(k), "tag": k, "mastery": round(v, 2)}
                          for k, v in weakest],
        "repeated_mistakes": repeated,
        "recent": recent,
    }
