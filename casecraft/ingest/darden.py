"""Parser for Darden-format casebooks (2018-19 and 2024-25 layouts).

Both books are slide decks with a rigid section grammar, which is what makes a
hard-coded parser viable at all:

    <case title page>   CLARIFYING INFORMATION / PROMPT / BEHAVIORAL
    <framework page>    Framework Guidance + category headers with bullets
    <exhibit pages>     EXHIBIT n
    <question pages>    Question n, then Calculation
    <brainstorm page>   BRAINSTORMING
    <conclusion page>   CONCLUSION / Recommendation

Two things are extracted with high confidence — prose (prompts, clarifications,
question text) and structure (which sections exist, in what order). Two are
not: numeric answers, which have to be picked out of a worked solution, and
rubric weights, which the book never states. Those are emitted as best guesses
and flagged in `_review`, because a wrong `expected` value silently marks
correct answers wrong, which is worse than no question at all.

Every case parsed here is written to ~/.casecraft/cases/ and never to the
bundled data/ directory — casebooks are copyrighted, and the Darden book carries
an explicit restriction on redistribution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .extract import Page

# ── page-level anchors ───────────────────────────────────────────────────── #

# Case pages carry a letter-spaced running header: "0 3  |  C AT C H  M E  O R"
RUNNING_HEADER = re.compile(r"^\s*\d\s*\d?\s*\|\s*[A-Z][A-Z\s,'\.\&\-]{6,}$", re.M)

SECTION_PATTERNS = {
    "clarifying": re.compile(r"^\s*CLARIFYING\s+INFORMATION\s*:?", re.I | re.M),
    "prompt": re.compile(r"^\s*PROMPT\s*:?", re.I | re.M),
    "behavioral": re.compile(r"^\s*BEHAVIORAL\b", re.I | re.M),
    "framework": re.compile(r"^\s*FRAMEWORK\s+GUIDANCE\s*:?", re.I | re.M),
    "exhibit": re.compile(r"^\s*EXHIBIT\s+(\d+)\s*$", re.I | re.M),
    "question": re.compile(r"^\s*QUESTIONS?\s+(\d+)\b", re.I | re.M),
    "calculation": re.compile(
        r"^\s*(?:CALCULATIONS?|EXPECTED CALCULATIONS?|MATH GUIDANCE|MARKET SIZING GUIDANCE)\b",
        re.I | re.M),
    # Where the answers actually live. The books never use one consistent
    # heading for worked math — it hides under whichever "Guidance" slide
    # follows the question, which is why these have to be sections too.
    "guidance": re.compile(
        r"^\s*(?:EXHIBIT (?:OR QUESTION )?(?:\d+ )?GUIDANCE|QUESTION \d+ GUIDANCE|"
        r"EXHIBIT \d+ INTERVIEWER GUIDANCE|QUESTION GUIDANCE[^\n]*|"
        r"BEST CANDIDATES DISPLAY)\s*:?", re.I | re.M),
    "brainstorm": re.compile(r"^\s*BRAINSTORM(?:ING)?\b", re.I | re.M),
    "conclusion": re.compile(r"^\s*(?:CONCLUSION|RECOMMENDATION)\s*:?\s*$", re.I | re.M),
}

# Sections that end a question's "answer zone" — anything else that follows a
# question belongs to it.
ZONE_ENDERS = {"question", "brainstorm", "conclusion", "exhibit", "framework", "clarifying", "prompt"}

# Not a section boundary: in the Darden layout "How to Move Forward" sits
# *between* the framework header and its buckets, so splitting on it would cut
# the rubric away from the section it belongs to.
MOVE_FORWARD = re.compile(r"^\s*How to Move Forward\s*:?", re.I | re.M)

# Page-number echoes sit immediately after a page marker. Stripping them
# POSITIONALLY (here) instead of deleting every standalone 1-3 digit line
# (the old NOISE rule) is what lets table data — "500 / mL / ounces" — survive.
PAGE_ECHO = re.compile(r"<<<PAGE \d+>>>\s*(?:\n\s*\d{1,3}\s*$){0,3}", re.M)


def strip_page_furniture(text: str) -> str:
    return PAGE_ECHO.sub("", text)


NOISE = re.compile(
    r"^\s*(?:UVA Darden School of Business.*|Darden Case Book.*|<<<PAGE \d+>>>|"
    r"CALCULATIONS? ON NEXT PAGE|Note: There are many possible.*|"
    r"Note: This is just one possible.*|"
    # Letter-spaced running header: "0 7  |  C A S E : T R A NS P O R TAT I O N"
    r"\d[\s\d]*\|[\sA-Z:,.'&\-’]+)\s*$",
    re.I | re.M,
)
# The same header can also be glued mid-line by the extractor.
INLINE_HEADER = re.compile(r"\d\s*\d?\s*\|(?:\s*[A-Z][\s.:,'&\-’]*){6,}")

# Slide decks set the running header in letter-spaced caps, which the extractor
# renders as "E D U C AT I O N | G R O W T H". It has no leading page number,
# so the pattern above misses it — and it was landing at the end of EVERY 2024
# case prompt, i.e. in the text read aloud to the candidate.
_SPACED_TOKEN = re.compile(r"^[A-Z&][A-Z]?$")


def _strip_spaced_caps(text: str) -> str:
    """Drop trailing runs of letter-spaced capitals.

    Detected by shape rather than by a fixed pattern: five or more consecutive
    one- or two-letter uppercase tokens is a typeset header, never prose.
    """
    tokens = text.split()
    run_start = None
    caps_in_run = 0
    for i in range(len(tokens) - 1, -1, -1):
        token = tokens[i].strip("|")
        if _SPACED_TOKEN.match(token):
            caps_in_run += 1
            run_start = i
        elif token == "" or token.isdigit() and len(token) <= 3:
            # page numbers and pipes ride along with the header furniture
            run_start = i
        else:
            break
    # Only strip when the run is unmistakably a typeset header — at least five
    # spaced-caps tokens — so a sentence ending in real numbers survives.
    if run_start is not None and caps_in_run >= 5:
        return " ".join(tokens[:run_start]).rstrip(" |—-")
    return text


@dataclass
class RawCase:
    title: str
    industry: str = ""
    case_type: str = ""
    difficulty: int = 3
    quant: int = 3
    start_page: int = 0
    end_page: int = 0
    pages: list[Page] = field(default_factory=list)

    @property
    def text(self) -> str:
        return strip_page_furniture(
            "\n".join(f"<<<PAGE {p.number}>>>\n{p.text}" for p in self.pages))


# ── table of contents ────────────────────────────────────────────────────── #

# 2024-25:  Title (N) Industry CaseType d d d Page
TOC_2024 = re.compile(
    r"^(?P<title>[A-Z][^\n]*?)\s*\((?P<status>[NR])\)\s+"
    r"(?P<industry>[A-Za-z&\s/\-]+?)\s+"
    r"(?P<case_type>Market Entry|Market Sizing|Profitability|Operations|Growth|M&A|"
    r"Pricing|Cost Improvement|Customer Experience|Product Launch|NPV|Valuation|Other)\s+"
    r"(?P<d1>[1-5])\s+(?P<d2>[1-5])\s+(?P<d3>[1-5])\s+(?P<page>\d{1,3})\s*$",
    re.M,
)

# 2018-19:  Title (N)* Firm Industry Round q qual ovr Page
TOC_2018 = re.compile(
    r"^(?P<title>[A-Z][^\n]*?)\s*\((?P<status>[NR])\)\*?\s+"
    r"(?P<firm>Bain & Co\.|BCG|McKinsey|AT Kearney|Parthenon EY|Deloitte|Accenture|LEK|Other)\s+"
    r"(?P<industry>[A-Za-z&\s/\-]+?)\s+"
    r"(?P<round>[12])\s+(?P<quant>[1-3])\s+(?P<qual>[1-3])\s+(?P<ovr>[1-3])\s+"
    r"(?P<page>\d{1,3})\s*$",
    re.M,
)


def parse_toc(pages: list[Page]) -> list[RawCase]:
    """Read the index table. It gives titles, metadata and start pages for free."""
    blob = "\n".join(p.text for p in pages[:12])
    cases: list[RawCase] = []

    for m in TOC_2024.finditer(blob):
        cases.append(RawCase(
            title=_clean_title(m.group("title")),
            industry=m.group("industry").strip(),
            case_type=_normalise_type(m.group("case_type")),
            difficulty=int(m.group("d1")),
            quant=int(m.group("d2")),
            start_page=int(m.group("page")),
        ))

    if not cases:
        for m in TOC_2018.finditer(blob):
            cases.append(RawCase(
                title=_clean_title(m.group("title")),
                industry=m.group("industry").strip(),
                case_type="",                       # 2018 index has no case-type column
                difficulty=int(m.group("ovr")),
                quant=int(m.group("quant")),
                start_page=int(m.group("page")),
            ))

    cases.sort(key=lambda c: c.start_page)
    for a, b in zip(cases, cases[1:]):
        a.end_page = b.start_page - 1
    if cases:
        cases[-1].end_page = 10_000
    return cases


def _clean_title(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip(" .;:-")


_TYPE_MAP = {
    "market entry": "market_entry", "market sizing": "market_sizing",
    "profitability": "profitability", "operations": "operations",
    "growth": "growth", "m&a": "m_and_a", "pricing": "pricing",
    "cost improvement": "cost_reduction", "customer experience": "customer_experience",
    "product launch": "product_launch", "npv": "valuation", "valuation": "valuation",
}


def _normalise_type(raw: str) -> str:
    return _TYPE_MAP.get(raw.strip().lower(), "other")


# ── slicing pages to cases ───────────────────────────────────────────────── #

def attach_pages(cases: list[RawCase], pages: list[Page]) -> list[RawCase]:
    """Bind PDF pages to cases.

    The index prints *slide* numbers, which usually equal PDF page numbers but
    can drift by a page or two. Rather than trust it, anchor on the running
    header: the first page whose header names the case wins.
    """
    by_number = {p.number: p for p in pages}
    for case in cases:
        found = [
            p for p in pages
            if case.start_page - 3 <= p.number <= case.end_page + 3
            and _header_names(p.text, case.title)
        ]
        if found:
            case.pages = [p for p in pages
                          if found[0].number <= p.number <= (found[-1].number)]
        else:
            case.pages = [by_number[n] for n in range(case.start_page, min(case.end_page, len(pages)) + 1)
                          if n in by_number]
    return cases


def _despace(text: str) -> str:
    """'C AT C H  M E' -> 'catchme' for header matching."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _header_names(page_text: str, title: str) -> bool:
    key = _despace(title)[:14]
    if len(key) < 6:
        return False
    for line in page_text.split("\n"):
        if "|" in line and _despace(line).find(key) >= 0:
            return True
    return False


# ── section extraction ───────────────────────────────────────────────────── #

@dataclass
class Section:
    kind: str
    label: str
    body: str


def split_ordered(text: str) -> list[Section]:
    """Cut the case at its section headers, preserving document order.

    Order matters more than grouping: a question's worked answer is whatever
    guidance section comes *after* it, so the builder has to walk the sequence
    rather than look sections up by kind.
    """
    marks: list[tuple[int, str, str]] = []
    for kind, pattern in SECTION_PATTERNS.items():
        for m in pattern.finditer(text):
            label = m.group(1) if m.groups() else ""
            marks.append((m.start(), kind, label))
    marks.sort()

    # Overlapping patterns (a "Question 2 Guidance" line matches both) — keep
    # the more specific one, which is whichever matched at the same offset last.
    deduped: list[tuple[int, str, str]] = []
    for mark in marks:
        if deduped and deduped[-1][0] == mark[0]:
            if mark[1] == "guidance":
                deduped[-1] = mark
            continue
        deduped.append(mark)

    out: list[Section] = []
    for i, (start, kind, label) in enumerate(deduped):
        end = deduped[i + 1][0] if i + 1 < len(deduped) else len(text)
        out.append(Section(kind, label, _strip_noise(text[start:end])))
    return out


def split_sections(text: str) -> dict[str, list[str]]:
    """Grouped view, for callers that don't care about order."""
    out: dict[str, list[str]] = {}
    for section in split_ordered(text):
        out.setdefault(section.kind, []).append(section.body)
    return out


def _strip_noise(text: str) -> str:
    text = NOISE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_header(body: str) -> str:
    """Drop the section's own header line."""
    lines = body.split("\n")
    return "\n".join(lines[1:]).strip() if lines else ""


# ── clarifying questions ─────────────────────────────────────────────────── #

CLARIFY_ITEM = re.compile(
    r"^\s*(\d+)[\.\)]\s*(?P<q>[^\n]+\?)\s*\n(?P<a>.+?)(?=^\s*\d+[\.\)]|\Z)",
    re.M | re.S,
)


def parse_clarifications(section: str) -> list[dict]:
    out = []
    for m in CLARIFY_ITEM.finditer(section):
        question = _tidy(m.group("q"))
        answer = _tidy(m.group("a"))
        if len(answer) < 8:
            continue
        out.append({
            "id": _slug(question)[:28] or f"clar{len(out)+1}",
            "topic": _topic_from(question),
            "match": _keywords(question),
            "response": answer,
        })
    return out


def _topic_from(question: str) -> str:
    """Turn 'What geographical markets do they serve?' into a topic label.

    The topic is all the host model ever sees before the candidate asks, so it
    must describe the subject without containing the answer.
    """
    q = question.strip().rstrip("?")
    # Strip the interrogative *and* any auxiliary that follows it, so
    # "What is Disney hoping to achieve" becomes "Disney hoping to achieve"
    # rather than the half-eaten "is Disney hoping to achieve".
    q = re.sub(r"^(what|which|how much|how many|how|why|where|when|who)\s+", "", q, flags=re.I)
    q = re.sub(r"^(is|are|was|were|do|does|did|can|could|would|should|has|have)\s+", "", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip()
    return q[:90] or "background on the client"


STOP = {
    "the", "a", "an", "is", "are", "was", "were", "do", "does", "did", "of", "to",
    "in", "on", "for", "with", "and", "or", "what", "which", "how", "why", "when",
    "where", "who", "there", "their", "they", "it", "its", "we", "our", "you",
    "your", "any", "have", "has", "had", "be", "been", "this", "that", "these",
    "client", "company", "hoping", "achieve", "please", "provide", "would", "like",
}


def _keywords(question: str) -> list[str]:
    words = re.findall(r"[a-z][a-z\-']{3,}", question.lower())
    seen, out = set(), []
    for w in words:
        if w in STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:8]


# ── framework → rubric buckets ───────────────────────────────────────────── #

# Category headers in the framework slide are short title-case lines followed by
# bullets. That shape is the only thing distinguishing them from body prose.
BUCKET_HEAD = re.compile(r"^(?P<label>[A-Z][A-Za-z /&\-]{3,38})\s*$", re.M)
_META_GUIDANCE = re.compile(
    r"\b(strong|best|excellent|good)?\s*(candidates?|interviewees?)\s+(will|should|may|must)"
    r"|\binterviewers?\b|move (on|forward) to|provide (this|it) (only )?if",
    re.I)
BULLET = re.compile(r"^\s*[•\-\*•‣▪]")


def _is_bucket_head(lines: list[str], i: int) -> bool:
    """A framework category header, distinguished from a wrapped bullet line.

    Text alone can't tell "Streaming Market" (a header) from "Demand for
    streamlined" (the middle of a wrapped bullet) — both are capitalised word
    runs. What separates them is what comes next: a header is always followed
    by a bullet. That lookahead is the whole discriminator.
    """
    line = lines[i]
    if not BUCKET_HEAD.fullmatch(line) or len(line.split()) > 5:
        return False
    if BULLET.match(line) or line.endswith(":"):
        return False
    for nxt in lines[i + 1:]:
        if not nxt:
            continue
        return bool(BULLET.match(nxt))
    return False


def _first_bucket_offset(text: str) -> int:
    lines = text.split("\n")
    stripped = [l.strip() for l in lines]
    offset = 0
    for i, raw in enumerate(lines):
        if stripped[i] and _is_bucket_head(stripped, i):
            return offset
        offset += len(raw) + 1
    return len(text)


def parse_framework(section: str) -> tuple[list[dict], str]:
    """Extract rubric components from the framework guidance slide.

    Returns (components, guidance_prose). Weights are *not* in the book — every
    component is emitted at weight 2 and the first three marked must-have, which
    is a starting point to correct, not a finding.
    """
    body = strip_header(section)
    guidance = ""
    mf = MOVE_FORWARD.search(body)
    if mf:
        # Guidance prose runs from the header to the first bucket, not to the
        # end of the slide.
        tail = body[mf.end():]
        stop = _first_bucket_offset(tail)
        guidance = _tidy(tail[:stop])

    lines = [l.rstrip() for l in body.split("\n")]
    stripped_lines = [l.strip() for l in lines]
    components: list[dict] = []
    current: dict | None = None

    for i, stripped in enumerate(stripped_lines):
        if not stripped:
            continue
        if _is_bucket_head(stripped_lines, i):
            current = {"label": stripped, "detail": []}
            components.append(current)
        elif current is not None:
            current["detail"].append(stripped.lstrip("•-*• "))

    out: list[dict] = []
    for c in components:
        # Detail bullets are a mix of concepts ("fuel, labor, leases") and
        # interviewer meta-guidance ("Strong candidates will recognize...").
        # The meta lines are not things a candidate could SAY, so as matching
        # targets they only mislead — drop them, and keep labels short enough
        # to be a target rather than an essay.
        kept = [d for d in c["detail"]
                if not _META_GUIDANCE.search(d)]
        detail = " ".join(kept)[:150]
        if not detail:
            continue
        out.append({
            "id": _slug(c["label"])[:24] or f"bucket{len(out)+1}",
            "label": f"{c['label']} — {detail}",
            "weight": 2,
            # Position among the components we *kept*, not among those we saw —
            # skipped empty headers must not consume must-have slots.
            "must_have": len(out) < 3,
            "accept": _keywords(c["label"] + " " + detail)[:8],
        })
    return out, guidance


# ── numeric answers ──────────────────────────────────────────────────────── #

MONEY = re.compile(
    r"(?P<neg>-)?\$?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<scale>bn|billion|B\b|mn|million|MM|M\b|K\b|thousand)?",
    re.I,
)
SCALES = {"bn": 1e9, "billion": 1e9, "b": 1e9, "mn": 1e6, "million": 1e6,
          "mm": 1e6, "m": 1e6, "k": 1e3, "thousand": 1e3}

# A line that performs arithmetic. Worked solutions are full of these; the
# candidate's final answer is the value on the last one.
CALC_LINE = re.compile(r"=\s*[\-–]?\s*\$?\s*\d")
# Lines that announce a result rather than a step.
CONCLUSION_LINE = re.compile(
    r"\b(?:total|in total|answer|therefore|so the|overall|final|net|"
    r"annual|per year|breakeven|break-even|payback|conclusion)\b", re.I)
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def _values_in(line: str) -> list[float]:
    out = []
    for m in MONEY.finditer(line):
        raw = m.group("num").replace(",", "")
        if not raw or raw.count(".") > 1:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        scale = (m.group("scale") or "").lower().rstrip(".")
        if scale:
            value *= SCALES.get(scale, 1)
        if m.group("neg"):
            value = -value
        out.append(value)
    return out


def parse_calculation(section: str) -> dict | None:
    """Best-effort numeric answer from a worked solution.

    Strategy: only consider lines that actually compute something (`= <number>`),
    take the value on the right of the last `=`, and prefer a line that also
    reads like a conclusion. Steps from earlier lines become partial credit.

    This is a heuristic over free prose and it will sometimes be wrong, which is
    why every question built from it carries a review flag. A wrong `expected`
    marks correct answers wrong — a silent failure that would quietly destroy
    trust in every other verdict the tool gives.
    """
    body = strip_header(section)
    calc_lines = [l.strip() for l in body.split("\n") if CALC_LINE.search(l)]
    if not calc_lines:
        return None

    def final_value(line: str) -> float | None:
        tail = line.rsplit("=", 1)[-1]
        if YEAR.search(tail) and not re.search(r"[$%]", tail):
            return None
        values = _values_in(tail)
        return values[0] if values else None

    scored: list[tuple[int, int, float, str]] = []
    for i, line in enumerate(calc_lines):
        value = final_value(line)
        if value is None or abs(value) < 2:
            continue
        scored.append((1 if CONCLUSION_LINE.search(line) else 0, i, value, line))

    if not scored:
        return None

    # Prefer a conclusion line; among equals, the last one wins.
    scored.sort(key=lambda t: (t[0], t[1]))
    _, _, value, line = scored[-1]

    steps = []
    for i, l in enumerate(calc_lines):
        v = final_value(l)
        if v is None or abs(v) < 2 or v == value:
            continue
        steps.append({"id": f"step{len(steps)+1}", "label": _tidy(l)[:110], "value": round(v, 4)})
        if len(steps) >= 4:
            break

    return {
        "kind": "numeric",
        "expected": round(value, 4),
        "units": "",
        "tolerance_pct": 5,
        "steps": steps,
        "common_errors": [],
        "_source_line": _tidy(line)[:180],
    }


# ── helpers ──────────────────────────────────────────────────────────────── #

def _tidy(text: str) -> str:
    text = NOISE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = INLINE_HEADER.sub(" ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" |")
    return _strip_spaced_caps(text)


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")
