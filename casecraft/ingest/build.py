"""Assemble parsed sections into casecraft case JSON.

The governing principle here is **skip rather than guess**. A question with a
wrong `expected` value marks correct answers wrong; a rubric with invented
components probes the candidate toward something the casebook never said. Both
are worse than the question simply not existing, because they erode trust in
every other verdict the tool gives.

So each question has to clear a confidence bar to be emitted, and everything
that clears it carries a `_review` note saying which fields were inferred. The
numbers in particular — `expected`, `tolerance_pct`, and the entirely absent
`common_errors` — are starting points for a human pass, not findings.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import darden
from .darden import RawCase

USER_CASES = Path.home() / ".casecraft" / "cases"

# Question text tends to arrive as a bullet under an instruction line
# ("Ask the following question after the candidate has commented on Exhibit 1:").
BULLET_LINE = re.compile(r"^\s*[•\-\*‣▪]\s*(?P<text>.{25,}?)\s*$", re.M)
INSTRUCTION = re.compile(
    r"^\s*(?:ask the following|ask the candidate|after the candidate|"
    r"if the interviewee|note:|prompt the candidate|read the following)",
    re.I,
)


# What makes a line an actual question put to a candidate, rather than a data
# slide or a stage direction. Without this the parser happily turns
# "Current Subscribers: Disney+: 100M..." into a question.
ASKS = re.compile(
    r"(\?|^|\b)(calculate|what\s|what'?s|how many|how much|how long|how would|"
    r"estimate|determine|compute|find the|size the|work out|which option|"
    r"should (?:we|they|the client)|walk me through|talk me through)\b",
    re.I,
)
# A data slide, not a question.
DATA_SLIDE = re.compile(
    r"^\s*(?:current|projected|estimated|given|assume|data|note|if the interviewee)\b[^?]*:",
    re.I,
)


# Asking for a *quantity*, specifically. "What other information do you need?"
# is a fine question and a terrible math question.
NUMERIC_ASK = re.compile(
    r"\b(calculate|compute|estimate|how many|how much|how long|"
    r"what (?:is|are|would be) the (?:total|annual|expected|projected|new|resulting|"
    r"break-?even|payback|cost|revenue|profit|savings|value|number|price|margin)|"
    r"size the market|market size|work out|determine the (?:number|value|cost|revenue|"
    r"profit|savings|payback|break-?even|price))\b",
    re.I,
)
NOT_NUMERIC = re.compile(
    r"\b(what other|what else|what additional|what would you do|what do you think|"
    r"what are some|brainstorm|why might|what risks|how would you approach)\b", re.I)


def _asks_for_a_number(text: str) -> bool:
    return bool(text and NUMERIC_ASK.search(text) and not NOT_NUMERIC.search(text))


def _looks_like_question(text: str) -> bool:
    if not text or len(text) < 30:
        return False
    if DATA_SLIDE.match(text) and "?" not in text:
        return False
    return bool(ASKS.search(text))


def _merge_wrapped(body: str) -> str:
    """Re-join bullet lines the PDF wrapped mid-sentence.

    "…purchasing kegs rather\nthan leasing them?" is ONE bullet; matching
    per-line truncated imported questions at the wrap point.
    """
    merged: list[str] = []
    for line in body.split("\n"):
        stripped = line.strip()
        if (merged and stripped and not BULLET_LINE.match(line)
                and not stripped[0].isupper() and not stripped[0].isdigit()):
            merged[-1] = merged[-1].rstrip() + " " + stripped
        else:
            merged.append(line)
    return "\n".join(merged)


def _question_text(section: str) -> str | None:
    """Pull the question actually put to the candidate out of a slide.

    Strict on purpose. These slides interleave the question with the *answer*
    ("Brainstorming Guidance: • Exclusive releases • Influencer partnerships"),
    and a loose fallback happily returns the answer bullets as the prompt — the
    interviewer would then read the solution aloud as the question. Better to
    emit no question than to emit that, so anything that doesn't read as a
    question is rejected.
    """
    body = _merge_wrapped(darden.strip_header(section))

    # 1. A bulleted line that asks something — the interviewer's literal line.
    for m in BULLET_LINE.finditer(body):
        candidate = darden._tidy(m.group("text"))
        if not INSTRUCTION.match(candidate) and _looks_like_question(candidate):
            return candidate[:900]

    # 2. Any sentence in the slide that ends in a question mark.
    flat = darden._tidy(body)
    for sentence in re.split(r"(?<=[.?!])\s+", flat):
        sentence = sentence.strip(" •-*")
        if sentence.endswith("?") and len(sentence) > 30 and not INSTRUCTION.match(sentence):
            return sentence[:900]

    # 3. Leading prose, but only if it reads as a question or a task.
    lines = [l.strip() for l in body.split("\n") if l.strip()]
    prose = darden._tidy(" ".join(l for l in lines if not INSTRUCTION.match(l)))
    if _looks_like_question(prose):
        return prose[:900]
    return None


def _guidance_components(section: str, prefix: str) -> list[dict]:
    """Turn 'candidates should identify: • x • y' bullets into rubric buckets."""
    body = darden.strip_header(section)
    out: list[dict] = []
    for m in BULLET_LINE.finditer(body):
        text = darden._tidy(m.group("text"))
        if INSTRUCTION.match(text) or text.endswith("?") or len(text) < 25:
            continue
        label = text[:200]
        out.append({
            "id": f"{prefix}{len(out)+1}_" + (darden._slug(label)[:18] or "point"),
            "label": label,
            "weight": 2,
            "must_have": len(out) < 2,
            "accept": darden._keywords(label)[:8],
        })
        if len(out) >= 6:
            break
    return out


def build_case(raw: RawCase, *, book: str, book_id: str) -> tuple[dict | None, list[str]]:
    """Return (case_json, review_notes). None when too little survived parsing."""
    notes: list[str] = []
    secs = darden.split_sections(raw.text)

    if "prompt" not in secs:
        return None, [f"{raw.title}: no PROMPT section found — skipped"]
    prompt = darden._tidy(darden.strip_header(secs["prompt"][0]))
    if len(prompt) < 80:
        return None, [f"{raw.title}: prompt too short ({len(prompt)} chars) — skipped"]

    case_id = f"{book_id}-{darden._slug(raw.title)[:40]}"
    clarifications = darden.parse_clarifications(secs["clarifying"][0]) if "clarifying" in secs else []
    if not clarifications:
        notes.append(f"{raw.title}: no clarifying information parsed")

    questions: list[dict] = []
    exhibits: list[dict] = []
    order = 0

    # ── 1. framework → the structure question ────────────────────────────
    if "framework" in secs:
        components, guidance = darden.parse_framework(secs["framework"][0])
        if len(components) >= 2:
            order += 1
            questions.append({
                "id": "q1", "order": order, "type": "structure",
                "difficulty": max(1, min(5, raw.difficulty)),
                "tags": ["mece", "framework", raw.case_type or "general"],
                "context": _context_from(prompt),
                "prompt": "How would you approach this problem? Walk me through your framework.",
                "read_aloud": True, "time_target_sec": 180,
                "rubric": {"kind": "buckets", "components": components,
                           "bonus": _STANDARD_BONUS},
                "probes": _STRUCTURE_PROBES,
                "model_answer": guidance or " ".join(c["label"] for c in components),
                "_review": "components auto-extracted; weights and must_have are defaults",
            })
        else:
            notes.append(f"{raw.title}: framework had {len(components)} buckets — no structure question")

    # ── 2. exhibits ──────────────────────────────────────────────────────
    #
    # In the real interview the exhibit is handed across the table and the
    # candidate reads the numbers off it. Keeping only a 120-char title threw
    # that data away — leaving several "calculate X" questions with no inputs
    # anywhere in the case. Preserve the slide's text, line by line.
    for i, section in enumerate(secs.get("exhibit", []), start=1):
        lines = [darden._tidy(l) for l in darden.strip_header(section).split("\n")]
        lines = [l for l in lines if l]
        title = (lines[0] if lines else "")[:100]
        body = "\n".join(lines[1:])[:1500]
        exhibits.append({
            "id": f"ex{i}",
            "title": title or f"Exhibit {i}",
            "text": body or None,
            "read_aloud_intro": f"I'm showing you exhibit {i}. Take a moment and tell me what you see.",
            "note": "Extracted as text — the original chart graphic is in the source casebook.",
            "_review": "exhibit is text-only; the underlying chart was not extracted",
        })

    # ── 3. numbered questions, each with the answer zone that follows it ──
    #
    # The books never head worked math consistently — it lands on whichever
    # "Guidance"/"Calculation" slide comes after the question. So a question
    # owns every section up to the next question, exhibit, or conclusion.
    ordered = darden.split_ordered(raw.text)
    # Which exhibit (1-based) most recently preceded each question in document
    # order: that IS the exhibit the question is about — Darden always places
    # the exhibit slide right before the question that uses it. Without the
    # link, the room never showed imported exhibits before their question.
    zones: list[tuple[str, str, int | None]] = []   # (question, zone, exhibit_no)
    exhibit_seen = 0
    last_link_at = -1
    for idx, section in enumerate(ordered):
        if section.kind == "exhibit":
            exhibit_seen += 1
        if section.kind != "question":
            continue
        link = exhibit_seen if exhibit_seen > 0 and last_link_at < exhibit_seen else None
        if link:
            last_link_at = exhibit_seen
        tail = []
        for nxt in ordered[idx + 1:]:
            if nxt.kind in darden.ZONE_ENDERS:
                break
            tail.append(nxt.body)
        zones.append((section.body, "\n".join(tail), link))

    for i, (section, zone, exhibit_no) in enumerate(zones):
        text = _question_text(section)
        if not text:
            notes.append(f"{raw.title}: question {i+1} had no extractable text — skipped")
            continue

        # The answer may be on the question slide itself or in its zone.
        numeric = darden.parse_calculation(section) or darden.parse_calculation(zone)

        order += 1
        qid = f"q{order}"

        # A math question only ships when BOTH halves are trustworthy: the text
        # must actually ask for a number, and the answer must come off a line
        # that reads like a result rather than an intermediate step. Failing
        # either, it's demoted to a discussion question or dropped — a math
        # question with a wrong `expected` marks correct answers wrong, and one
        # of those poisons the candidate's trust in every other verdict.
        if numeric and not _asks_for_a_number(text):
            notes.append(f"{raw.title}: question {i+1} has a number but doesn't ask for one — demoted")
            numeric = None

        # Playability gate. A real interviewer HAS the inputs — on the exhibit,
        # in the clarifying data, or in the question itself — and hands them
        # over when asked. If extraction preserved none of them, the question
        # is impossible, and an impossible question that grades you "wrong" is
        # worse than no question. Demote it to a discussion.
        if numeric:
            pool = " ".join(
                [text, _context_from(prompt)]
                + [(e.get("text") or "") + " " + (e.get("title") or "") for e in exhibits]
                + [c.get("response", "") for c in clarifications])
            if len(re.findall(r"\d", pool)) < 3:
                notes.append(f"{raw.title}: question {i+1} asks for a calculation but "
                             f"no input data survived extraction — demoted to discussion")
                numeric = None

        exhibit_ref = f"ex{exhibit_no}" if exhibit_no and exhibit_no <= len(exhibits) else None
        if numeric:
            worked = darden._tidy(zone or section)[:1500]
            built_q = math_question(qid, order, text, worked, numeric,
                                    difficulty=max(1, min(5, raw.quant)),
                                    tags=["arithmetic", raw.case_type or "general"],
                                    context=_context_from(prompt))
            built_q["exhibit_id"] = exhibit_ref
            questions.append(built_q)
        else:
            components = _guidance_components(section, "g") or _guidance_components(zone, "g")
            if len(components) < 2:
                notes.append(f"{raw.title}: question {i+1} not scoreable (no number, no bullets) — skipped")
                order -= 1
                continue
            questions.append({
                "id": qid, "order": order,
                "type": "exhibit" if exhibit_ref else "brainstorm",
                "difficulty": max(1, min(5, raw.difficulty)),
                "tags": ["interpretation", raw.case_type or "general"],
                "exhibit_id": exhibit_ref,
                "context": _context_from(prompt),
                "prompt": text, "read_aloud": True, "time_target_sec": 150,
                "rubric": {"kind": "buckets", "components": components, "bonus": _STANDARD_BONUS},
                "probes": _GENERIC_PROBES,
                "model_answer": " ".join(c["label"] for c in components),
                "_review": "rubric built from guidance bullets; weights are defaults",
            })

    # ── 4. brainstorming ─────────────────────────────────────────────────
    for section in secs.get("brainstorm", [])[:1]:
        text = _question_text(section)
        components = _guidance_components(section, "b")
        if not text:
            notes.append(f"{raw.title}: brainstorm question text not identifiable — skipped")
        if text and len(components) >= 2:
            order += 1
            questions.append({
                "id": f"q{order}", "order": order, "type": "brainstorm",
                "difficulty": max(1, min(5, raw.difficulty)),
                "tags": ["creativity", "breadth", raw.case_type or "general"],
                "context": _context_from(prompt),
                "prompt": text, "read_aloud": True, "time_target_sec": 150,
                "rubric": {"kind": "buckets", "components": components, "bonus": _STANDARD_BONUS},
                "probes": ["Good. What else?", "Can you group those into categories?",
                           "Which of those would you prioritise, and why?"],
                "model_answer": " ".join(c["label"] for c in components),
                "_review": "rubric built from guidance bullets; weights are defaults",
            })

    # ── 5. conclusion → synthesis ────────────────────────────────────────
    if secs.get("conclusion"):
        model = darden._tidy(darden.strip_header(secs["conclusion"][0]))
        if len(model) > 60:
            order += 1
            questions.append({
                "id": f"q{order}", "order": order, "type": "synthesis",
                "difficulty": max(1, min(5, raw.difficulty)),
                "tags": ["recommendation", "answer_first", "risks"],
                "context": _context_from(prompt),
                "prompt": "The CEO has just walked in and has two minutes. What do you tell her?",
                "read_aloud": True, "time_target_sec": 120,
                "rubric": {"kind": "open", "criteria": _SYNTHESIS_CRITERIA,
                           "model_answer": model[:1500]},
                "probes": ["Start with your answer, then support it.",
                           "Can you put numbers behind that?",
                           "What are the risks, and how would you mitigate them?"],
                "model_answer": model[:1500],
                "_review": "synthesis criteria are casecraft standards, not from the casebook",
            })

    if len(questions) < 2:
        return None, notes + [f"{raw.title}: only {len(questions)} usable questions — skipped"]

    case = {
        "id": case_id,
        "title": raw.title,
        "source": {"casebook": book, "pages": f"{raw.start_page}-{raw.end_page}",
                   "note": "Imported from a copyrighted casebook. Local use only — do not redistribute."},
        "meta": {
            "format": "interviewer_led",
            "firm_style": "generic",
            "case_type": raw.case_type or "other",
            "industry": darden._slug(raw.industry) or "general",
            "difficulty": max(1, min(5, raw.difficulty)),
            "expected_minutes": 30,
            "tags": [t for t in [raw.case_type, darden._slug(raw.industry)] if t],
            "imported": True,
        },
        "prompt": {"text": prompt, "read_aloud": True},
        "clarifications": clarifications,
        "exhibits": exhibits,
        "questions": questions,
        "_review": notes,
    }
    return case, notes


def math_question(qid: str, order: int, text: str, worked: str, numeric: dict,
                  *, difficulty: int, tags: list[str], context: str) -> dict:
    """An imported math question, graded against its worked solution.

    Deliberately NOT a `numeric` rubric. Extracting a final answer from
    free-form worked solutions turned out to be about one-in-three reliable —
    multi-part questions, follow-ups and intermediate results all look like
    conclusions to a regex. A `numeric` rubric with a wrong `expected` marks
    correct answers wrong, and one of those costs more trust than ten missing
    questions.

    So the reliable artefact — the worked solution text — becomes the model
    answer and the host model grades against it. The heuristic number is kept
    in `_candidate_expected` so it can be checked and promoted to a real
    numeric rubric, which restores instant deterministic grading for that
    question.
    """
    guess = numeric.get("expected")
    source = numeric.get("_source_line", "")
    return {
        "id": qid, "order": order, "type": "math",
        "difficulty": difficulty, "tags": tags,
        "context": context,
        "prompt": text, "read_aloud": True, "time_target_sec": 210,
        "rubric": {"kind": "open", "criteria": _MATH_CRITERIA, "model_answer": worked},
        "probes": _MATH_PROBES,
        "model_answer": worked,
        "_candidate_expected": guess,
        "_candidate_source": source,
        "_review": "UNVERIFIED math. Graded against the worked solution, not a number. "
                   f"To make grading instant and deterministic, check _candidate_expected "
                   f"({guess}) against the source, then replace the rubric with "
                   f'{{"kind": "numeric", "expected": ..., "tolerance_pct": 2}}.',
    }


_MATH_CRITERIA = [
    {"id": "final_answer", "label": "Reaches the same final figure as the worked solution", "weight": 4,
     "must_have": True},
    {"id": "setup", "label": "Sets the calculation up correctly before computing", "weight": 3,
     "must_have": True},
    {"id": "narrates", "label": "Talks through the steps rather than going silent", "weight": 2},
    {"id": "sanity", "label": "Sanity-checks the result or states units", "weight": 1},
    {"id": "so_what", "label": "Says what the number means for the client", "weight": 2},
]


def _context_from(prompt: str) -> str:
    """One-sentence standalone framing, so a question still makes sense in a drill."""
    first = re.split(r"(?<=[.!?])\s+", prompt.strip())[0]
    return first[:220]


_STANDARD_BONUS = [
    {"id": "structured", "label": "Lays out the structure before diving into detail", "weight": 1},
    {"id": "prioritized", "label": "Says which area matters most and why", "weight": 1},
]

_STRUCTURE_PROBES = [
    "Good start — anything else you'd want to look at?",
    "Which of those would you tackle first, and why?",
    "Is there a part of the problem your structure doesn't cover yet?",
]

_MATH_PROBES = [
    "Talk me through your approach.",
    "Walk me through that calculation step by step.",
    "Check your setup — are you using all the numbers I gave you?",
]

_GENERIC_PROBES = [
    "What else stands out to you?",
    "Can you quantify that?",
    "So what does that mean for the client?",
]

_SYNTHESIS_CRITERIA = [
    {"id": "answer_first", "label": "Opens with the recommendation rather than building up to it", "weight": 3},
    {"id": "quantified", "label": "Supports the recommendation with figures derived in the case", "weight": 3},
    {"id": "structured", "label": "Gives two or three clear supporting reasons", "weight": 2},
    {"id": "risks", "label": "Names at least one risk and a mitigation", "weight": 2},
    {"id": "next_steps", "label": "Suggests a concrete next step", "weight": 1},
]
