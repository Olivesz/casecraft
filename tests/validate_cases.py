"""Deep structural validation of every case, bundled and imported.

`--check` proves the JSON loads. This proves the case is COHERENT: references
resolve, ids are unique, weights are sane, numeric rubrics are internally
consistent, and no numeric answer collides with a given fact or another target.

    .venv/bin/python -m tests.validate_cases
"""
from __future__ import annotations

import re
import sys

from casecraft.library import Library
from casecraft.scoring import _close, extract_numbers

problems: list[str] = []


def flag(cond: bool, msg: str) -> None:
    if not cond:
        problems.append(msg)


def main() -> int:
    lib = Library()
    for case in lib.cases.values():
        cid = case.id
        qids = [q.id for q in case.questions]
        flag(len(qids) == len(set(qids)), f"{cid}: duplicate question ids {qids}")
        flag(bool(case.prompt.strip()), f"{cid}: empty prompt")

        clar_ids = [c["id"] for c in case.clarifications]
        flag(len(clar_ids) == len(set(clar_ids)), f"{cid}: duplicate clarification ids")
        for c in case.clarifications:
            flag(bool(c.get("topic")), f"{cid}: clarification {c['id']} has no topic")
            flag(bool(c.get("response")), f"{cid}: clarification {c['id']} has no response")
            # The topic is shown BEFORE the candidate asks; it must not contain
            # the fact itself (numbers are the usual leak).
            flag(not any(abs(n) >= 10 for n in extract_numbers(c.get("topic", ""))),
                 f"{cid}: clarification topic leaks a number: {c['topic']!r}")

        for q in case.questions:
            uid = f"{cid}/{q.id}"
            if q.exhibit_id:
                flag(q.exhibit_id in case.exhibits,
                     f"{uid}: exhibit_id {q.exhibit_id!r} does not exist")
            r = q.rubric
            kind = r.get("kind")
            flag(kind in ("numeric", "buckets", "open"), f"{uid}: bad rubric kind {kind!r}")

            if kind == "numeric":
                exp = float(r["expected"])
                tol = float(r.get("tolerance_pct", 2))
                seen = [exp]
                for e in r.get("common_errors", []):
                    v = float(e["value"])
                    flag(not any(_close(v, s0, tol) for s0 in seen),
                         f"{uid}: common_error {v} indistinguishable from another target")
                    seen.append(v)
                    flag(bool(e.get("diagnosis")), f"{uid}: error {v} has no diagnosis")
                for st in r.get("steps", []):
                    flag(not _close(float(st["value"]), exp, tol),
                         f"{uid}: step {st['id']} equals the final answer — free credit")
                # Restating a GIVEN from the prompt must never grade as correct.
                for given in extract_numbers(q.prompt):
                    flag(not _close(given, exp, tol),
                         f"{uid}: expected answer {exp} appears verbatim in the prompt")
            else:
                comps = r.get("components") or r.get("criteria") or []
                flag(len(comps) >= 2, f"{uid}: only {len(comps)} rubric components")
                ids = [c["id"] for c in comps] + [b["id"] for b in r.get("bonus", [])]
                flag(len(ids) == len(set(ids)), f"{uid}: duplicate component ids")
                for c in comps:
                    flag(c.get("weight", 1) > 0, f"{uid}: non-positive weight on {c['id']}")
                    flag(len(c.get("label", "")) >= 8, f"{uid}: label too thin on {c['id']}")
                if q.type in ("structure", "brainstorm", "exhibit"):
                    flag(bool(q.probes), f"{uid}: no probes on a probe-able question")

            flag(bool(q.model_answer and len(q.model_answer) > 20),
                 f"{uid}: model_answer missing or trivial")

    print(f"{len(problems)} integrity problems")
    for msg in problems:
        print(f"  • {msg}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
