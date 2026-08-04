"""Delivery analysis — the half of a case interview that isn't the answer.

Candidates prepare for the math. They fail on communication and judgment. Real
MBB feedback forms are four boxes — Structure, Analytics, Judgment,
Communication — and by final round the math is rarely the binding constraint.
What sinks people is hedging, burying the answer, and computing a number without
ever saying what it means.

Everything here is computed from the transcript and the clock. No model call, no
latency, no cost — and it's the feedback candidates never get, because a human
coach can't count your hedges while also running the case.

⚠️ One honest limitation: Whisper and the Web Speech API are both trained to
produce clean text, so they *delete* disfluencies. "Um" and "uh" largely don't
survive transcription. Filler counts are therefore a floor, not a measurement,
and are reported as such. Hedging is unaffected — "I think maybe" is real words
that any transcriber keeps — and pacing comes from the clock, so those two carry
the weight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── lexicons ─────────────────────────────────────────────────────────────── #

# Survives transcription poorly. Reported with a caveat.
_FILLER = re.compile(
    r"\b(um+|uh+|er+|ah+|hmm+|you know|i mean|basically|literally)\b", re.I
)

# Survives transcription intact — this is the signal that matters.
_HEDGE = re.compile(
    r"\b(i think|i guess|i feel like|i believe|maybe|perhaps|probably|possibly"
    r"|sort of|kind of|somewhat|a little bit|i'm not sure|not entirely sure"
    r"|it seems|it might|could be|might be|may be|i would say|arguably"
    r"|if that makes sense|does that make sense|hopefully)\b",
    re.I,
)

# Explicit structure. Interviewers can only follow what you signpost.
_SIGNPOST = re.compile(
    r"\b(first(ly)?|second(ly)?|third(ly)?|fourth|finally|lastly|next"
    r"|to start|let me start|starting with|moving on|turning to|then"
    r"|on the one hand|on the other hand|in summary|to summari[sz]e"
    r"|(two|three|four|five|2|3|4|5) (things|buckets|areas|drivers|factors|questions|parts|moves|steps|options|reasons|levers|priorities|recommendations|points|angles))\b",
    re.I,
)

# Top-down opening: a claim in the first breath, not a windup.
_ANSWER_FIRST = re.compile(
    r"^\W*(my recommendation|i'd recommend|i would recommend|i recommend"
    r"|the answer is|the short answer|my answer|bottom line|in short"
    r"|yes[,.]|no[,.]|the key driver|the main issue|the problem is"
    r"|we should|they should|the client should)",
    re.I,
)

# Windup: throat-clearing before any content.
_PREAMBLE = re.compile(
    r"^\W*(so|okay|ok|well|um+|uh+|alright|right|let me see|let's see"
    r"|that's a good question|good question|interesting)\b[,\s]*",
    re.I,
)

# Quantified reasoning — did they tie the argument to numbers?
_QUANTIFIED = re.compile(r"(\d|\bpercent\b|%|\bmillion\b|\bbillion\b|\$)", re.I)

# "So what" — the move from a number to its implication.
_SO_WHAT = re.compile(
    r"\b(which means|that means|so that (means|tells us|suggests)|implies"
    r"|the implication|therefore|as a result|so the takeaway|what this tells"
    r"|which suggests|the reason this matters|so we should"
    # Consequence phrasing — the implication stated as an outcome rather than
    # signposted with a connective.
    r"|pushes them into|puts them into|tips them into|turns .{0,20}into a loss"
    r"|is not the problem|isn't the problem|that is the driver|that's the driver"
    r"|wipes out|erodes|is unsustainable|cannot sustain|can't sustain)\b",
    re.I,
)


# ── report ───────────────────────────────────────────────────────────────── #

@dataclass
class Delivery:
    words: int
    seconds: float
    wpm: float
    filler_count: int
    hedge_count: int
    hedges: list[str] = field(default_factory=list)
    signpost_count: int = 0
    answer_first: bool = False
    preamble: str | None = None
    quantified: bool = False
    so_what: bool = False
    notes: list[str] = field(default_factory=list)   # coach-voice, specific
    score: float = 0.0                               # 0–1, feeds Communication

    def as_dict(self) -> dict:
        return {
            "words": self.words,
            "seconds": round(self.seconds, 1),
            "wpm": round(self.wpm),
            "hedges": self.hedges,
            "hedge_per_100w": round(self.hedge_count / max(self.words, 1) * 100, 1),
            "filler_count_floor": self.filler_count,
            "signposts": self.signpost_count,
            "answer_first": self.answer_first,
            "quantified": self.quantified,
            "so_what": self.so_what,
            "notes": self.notes,
            "score": round(self.score, 2),
        }


# Notes vary in wording by severity; the debrief tallies habits, so it needs a
# stable category per note or "hedged 5 times" and "some hedging" count as two
# different habits the candidate is doing.
NOTE_CATEGORIES: list[tuple[str, str]] = [
    ("hedg", "Hedging — it reads as low conviction"),
    ("Lead with the answer", "Burying the recommendation instead of leading with it"),
    ("windup", "Opening with a windup instead of content"),
    ("signpost", "Not signposting structure in long answers"),
    ("so what", "Giving a number without saying what it means"),
    ("quantified", "Recommending without quantifying"),
    ("fast", "Speaking too fast"),
    ("slow", "Speaking too slowly"),
    ("target", "Running long against the time target"),
    ("Very short", "Answers too thin to assess"),
    ("wasn't an answer", "Answers too thin to assess"),
]


def categorise(note: str) -> str | None:
    """Canonical habit name for a note, or None if it is praise."""
    if note == CLEAN_DELIVERY:
        return None
    for needle, label in NOTE_CATEGORIES:
        if needle.lower() in note.lower():
            return label
    return note.split(".")[0]


# The one note that is praise rather than a correction. Named so the debrief
# can exclude it: "recurring habits" is the list of things to work on, and
# reporting "clean delivery ×4" there reads as a criticism of a strength.
CLEAN_DELIVERY = "Clean delivery — direct, structured, and appropriately paced."


def _first_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)
    return parts[0] if parts else text.strip()


def analyze(
    transcript: str,
    seconds: float,
    *,
    target_seconds: float | None = None,
    expects_number: bool = False,
    expects_recommendation: bool = False,
    typed: bool = False,
) -> Delivery:
    """Score how an answer was *delivered*, independent of whether it was right.

    `expects_number` / `expects_recommendation` come from the question type —
    a market-sizing answer with no digits in it is a different failure from a
    brainstorm with no digits in it, and the notes should say so.
    """
    text = transcript.strip()
    words = len(text.split())
    seconds = max(seconds, 0.1)
    wpm = words / (seconds / 60)

    hedges = [m.group(0).lower() for m in _HEDGE.finditer(text)]
    opening = _first_sentence(text)
    preamble_m = _PREAMBLE.match(text)

    d = Delivery(
        words=words,
        seconds=seconds,
        wpm=wpm,
        filler_count=len(_FILLER.findall(text)),
        hedge_count=len(hedges),
        hedges=sorted(set(hedges)),
        signpost_count=len(_SIGNPOST.findall(text)),
        answer_first=bool(_ANSWER_FIRST.match(text)),
        preamble=preamble_m.group(0).strip() if preamble_m else None,
        quantified=bool(_QUANTIFIED.search(text)),
        so_what=bool(_SO_WHAT.search(text)),
    )

    penalties = 0.0

    # Nothing said, or nothing *of substance* said, must never score well.
    # Rating "Exceptional" communication for "blah blah blah" discredits the
    # whole scorecard. Word count alone is the wrong test — "It comes out to
    # 2.3 billion" is eight words and a real answer — so filler is caught by
    # lexical diversity instead: repeated words carry no content.
    unique = len({w.lower().strip(".,!?") for w in text.split()})
    diversity = unique / words if words else 0.0
    if words < 5 or (words < 25 and diversity < 0.55):
        d.score = 0.0 if words < 5 else 0.2
        d.notes.append(
            "There wasn't an answer here to assess. Communication is scored on "
            "what you actually said."
        )
        return d

    # ── Hedging: the single most common "low conviction" flag on real forms. ──
    hedge_rate = d.hedge_count / max(words, 1) * 100
    if hedge_rate > 8 and d.hedge_count >= 4:
        penalties += 0.55          # every other clause hedged — well below the bar
        d.notes.append(
            f"You hedged {d.hedge_count} times in {words} words "
            f"({', '.join(d.hedges[:3])}). At that rate it reads as no conviction "
            "at all — as though you don't have a view. Say the thing, then defend it."
        )
    elif hedge_rate > 4 and d.hedge_count >= 3:
        penalties += 0.30
        d.notes.append(
            f"You hedged {d.hedge_count} times ({', '.join(d.hedges[:3])}). "
            "Interviewers read that as low conviction. State it, then defend it — "
            "\"I want to look at costs\", not \"I think maybe we could look at costs\"."
        )
    elif hedge_rate > 2 and d.hedge_count >= 2:
        penalties += 0.12
        d.notes.append(
            f"Some hedging ({', '.join(d.hedges[:2])}). Minor, but it softens an "
            "otherwise firm answer."
        )

    # ── Top-down. Consultants lead with the answer; students lead with the work. ──
    if expects_recommendation and not d.answer_first:
        penalties += 0.30
        d.notes.append(
            "You built up to your recommendation instead of opening with it. "
            "Lead with the answer, then support it — a partner may only hear "
            "your first sentence."
        )
    if d.preamble and len(d.preamble) > 3:
        penalties += 0.05
        d.notes.append(f"Opened with \"{d.preamble}\" — cut the windup, start on content.")

    # ── Signposting. An unlabelled list is heard as rambling. ──
    if words > 90 and d.signpost_count == 0:
        penalties += 0.20
        d.notes.append(
            "No signposting in a long answer. Say how many things you'll cover, "
            "then label them — \"three areas: first… second… third…\". "
            "Interviewers can only follow structure they can hear."
        )

    # ── "So what". A number without an implication is not an insight. ──
    if expects_number and d.quantified and not d.so_what:
        penalties += 0.20
        d.notes.append(
            "You gave me the number but not the implication. Every calculation "
            "needs a \"so what\" — what does this mean for the client's decision?"
        )
    if expects_recommendation and not d.quantified:
        penalties += 0.15
        d.notes.append(
            "Your recommendation wasn't quantified. Cite the figures you derived; "
            "it's what separates a consultant's answer from an opinion."
        )

    # ── Pace. Both directions are real failure modes — when spoken. Typing
    # speed says nothing about delivery, so skip it entirely in text mode
    # rather than penalise a fast typist for "nerves".
    if typed:
        pass
    elif wpm > 200:
        penalties += 0.12
        d.notes.append(f"Speaking fast ({wpm:.0f} wpm) — usually nerves. Slow down; it reads as control.")
    elif wpm < 95 and words > 40:
        penalties += 0.08
        d.notes.append(f"Quite slow ({wpm:.0f} wpm) with long gaps. Some pausing is fine, drifting isn't.")

    # ── Length vs. the interviewer's patience. ──
    if target_seconds and not typed:
        if seconds > target_seconds * 1.8:
            penalties += 0.15
            d.notes.append(
                f"Ran {seconds:.0f}s against a ~{target_seconds:.0f}s target. "
                "In a 30-minute case that's real budget — tighten it."
            )
        elif seconds < target_seconds * 0.35 and words < 40:
            penalties += 0.10
            d.notes.append("Very short. Thin answers read as not knowing where to go next.")

    d.score = max(0.0, 1.0 - penalties)
    if not d.notes:
        d.notes.append(CLEAN_DELIVERY)
    return d
