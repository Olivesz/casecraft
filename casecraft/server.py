"""casecraft MCP server — the tools that let Claude run a case interview.

Design rule, enforced by every return type below: **the candidate must never be
able to read the question.** Prompts are spoken by the room and never returned
through MCP. Rubrics are released only after an answer is committed. Model
answers only after grading. If a tool could leak the case, it doesn't return it.

Tool descriptions here are not documentation — they are the host model's
operating instructions, and they land in its context. They're written to make
Claude behave like an interviewer (withhold, probe, pace) rather than a quiz
bot.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from . import progress as progress_store
from .library import ALL_TYPES, Library
from .room import RoomServer
from .session import Room, Session

INSTRUCTIONS = """
casecraft runs live, spoken consulting case interviews. You are the interviewer.

The case is delivered to the candidate inside the interview room — spoken in
voice mode, printed in text mode — and their answers come back the same way.
You never see the case text and must never guess at it. Everything you know
comes back through these tools. Never promise audio ("you'll hear me read...")
— the room decides delivery, not you.

How to run one well:
* Open with `start_case` or `start_drill`, then `ask_case_prompt`. Use `say`
  for anything in your own voice — greetings, probes, transitions, feedback.
* Give data only when asked. `answer_clarification` is the only way to release
  a fact, and the candidate has to earn it by asking.
* After each answer, `collect_answer` then `score`. Probe before revealing —
  `probe` escalates hints the way a real interviewer does.
* Interviewer-led cases (McKinsey style): you drive, question by question.
  Candidate-led (Bain/BCG style): ask what they want to look at, then steer.
* Stay in character while the case is running. Debrief warmly and specifically
  once `finish` returns the scorecard.

Be a real interviewer: courteous, unhurried, and hard to impress.
"""

mcp = FastMCP("casecraft", instructions=INSTRUCTIONS, version="0.1.0")

_library = Library()
_room = Room()
_server = RoomServer(_room)
_session: Session | None = None
_pending: dict[str, Any] = {}     # transcript awaiting a `score` call


# Interrogative openers for spotting a data request. Kept deliberately dumb and
# deterministic — this must never misfire on a real answer, so the prefix path
# additionally requires the utterance to contain no numbers at all.
_INTERROGATIVES = {
    "what", "what's", "whats", "how", "where", "when", "who", "why", "which",
    "can", "could", "do", "does", "did", "is", "are", "should", "would", "will",
}


def _looks_like_data_request(text: str) -> bool:
    """A short question with no numbers is a request, not an answer.

    In a real case the candidate ASKS for inputs — "what's the load factor?" —
    and the interviewer supplies them. Before this, that exchange was graded as
    a wrong answer and logged to the weakness model as a failed attempt.
    """
    from casecraft.scoring import extract_numbers

    t = text.strip()
    words = t.split()
    if not words or len(words) > 28 or extract_numbers(t):
        return False
    if t.endswith("?"):
        return True
    return len(words) <= 14 and words[0].lower().strip(",.") in _INTERROGATIVES


def _require_session() -> Session:
    if _session is None:
        raise ValueError("No session. Call start_case or start_drill first.")
    return _session


def _ensure_room() -> str:
    """Boot the room and open a tab. Idempotent — safe to call every time."""
    url = _server.start()
    if not _room.connected:
        _server.open_browser()
        _server.wait_for_page(timeout=45)
    return url


# ───────────────────────────── discovery ───────────────────────────── #

@mcp.tool
def catalog() -> dict:
    """List every case and drill category available, with counts.

    Safe to show the candidate — it contains titles and counts, never content.
    Use it to offer choices ("I have a profitability case in airlines and a
    market-entry case in consumer health — which would you like?").
    """
    return _library.catalog()


@mcp.tool
def progress(detail: bool = False) -> dict:
    """The candidate's history: weakest areas, repeated mistakes, recent attempts.

    Use it to recommend what to practise, and to open a session with something
    specific ("last time you dropped the load factor twice — let's do capacity
    math"). `detail` includes per-attempt rows.
    """
    report = progress_store.report()
    if not detail:
        report.pop("recent", None)
    return report


# ───────────────────────────── starting ────────────────────────────── #

@mcp.tool
def start_case(
    case_id: Annotated[str | None, Field(description="Specific case id from catalog()")] = None,
    case_type: Annotated[str | None, Field(description="e.g. profitability, market_entry")] = None,
    speed: Annotated[Literal["slow", "moderate", "regular"], Field(
        description="Delivery pace. 'slow' for learning, 'regular' for real interview pressure."
    )] = "moderate",
) -> dict:
    """Begin a full case interview. Opens the interview room in the browser.

    Returns a briefing: format, industry, difficulty, how many questions, and
    the *topics* of clarifying information available — never the facts
    themselves. Tell the candidate the room is open and confirm they can hear
    you before starting.
    """
    global _session

    pool = list(_library.cases.values())
    if case_id:
        if case_id not in _library.cases:
            raise ValueError(f"No case {case_id!r}. Use catalog() to see options.")
        case = _library.cases[case_id]
    else:
        if case_type:
            pool = [c for c in pool if c.meta.get("case_type") == case_type]
        if not pool:
            raise ValueError(f"No cases match case_type={case_type!r}.")
        case = pool[0]

    # A transcript captured in the previous session must never be graded
    # against this one's rubric. `_pending` is module state and outlives the
    # Session that produced it.
    _pending.clear()
    _room.drain()
    url = _ensure_room()
    _session = Session(_room, _library)
    _session.speed = speed
    _session.load_case(case)
    room_problem = _room.page_problem()
    _room.update(speed=speed, phase="prompt", scorecard=None, feedback=None,
                 progress=f"0 of {len(case.questions)}")
    _room.await_start_gate(
        case_title=case.title,
        speed=speed,
        # ONE continuous briefing, not three messages.
        #
        # Chunking it read as three separate turns with a pause after each,
        # which invited the candidate to interrupt the setup — no interviewer
        # pauses after "hello" to see if you want to say something. And the
        # case prompt already ends by asking the question ("...how would you
        # approach this?"), so a trailing "now tell me your structure" was both
        # redundant and unlike anything a real interviewer says.
        opening=[
            f"Good to meet you, and thanks for making the time. This is a "
            f"{case.meta.get('expected_minutes', 30)}-minute "
            f"{(case.meta.get('case_type') or 'business').replace('_', ' ')} case. "
            "I'll give you the situation, and then I'd like you to walk me through "
            "your thinking. Here it is.\n\n"
            + case.prompt
        ],
    )
    return {
        "room_url": url,
        "room_connected": _room.connected,
        "session_id": _session.id,
        "awaiting_start": True,
        "next": "The room is showing a Start button. Tell the candidate to press it "
                "when ready, then call ask_case_prompt — it waits for the press. "
                "Do not speak before then; the browser blocks audio until they click.",
        **case.briefing(),
    }


@mcp.tool
def start_drill(
    types: Annotated[list[str] | None, Field(
        description=f"Question types to drill, any of {list(ALL_TYPES)}. Omit for all."
    )] = None,
    min_difficulty: Annotated[int, Field(ge=1, le=5)] = 1,
    max_difficulty: Annotated[int, Field(ge=1, le=5)] = 5,
    tags: Annotated[list[str] | None, Field(description="Skill tags, e.g. breakeven, market_sizing")] = None,
    count: Annotated[int, Field(ge=1, le=20)] = 5,
    target_weaknesses: Annotated[bool, Field(
        description="Bias selection toward areas the candidate has scored badly on."
    )] = True,
    speed: Literal["slow", "moderate", "regular"] = "moderate",
) -> dict:
    """Begin a drill: loose questions pulled across cases, no full-case context.

    This is the "only hard math" mode. Each question carries its own standalone
    framing so it makes sense out of case order. With `target_weaknesses`, the
    sampler leans toward what they keep getting wrong without excluding the rest.
    """
    global _session

    questions = _library.drill(
        types=types, min_difficulty=min_difficulty, max_difficulty=max_difficulty,
        tags=tags, limit=count,
        weakness=progress_store.weakness() if target_weaknesses else None,
    )
    if not questions:
        raise ValueError("No questions match those filters. Try catalog() to see what exists.")

    # A transcript captured in the previous session must never be graded
    # against this one's rubric. `_pending` is module state and outlives the
    # Session that produced it.
    _pending.clear()
    _room.drain()
    url = _ensure_room()
    _session = Session(_room, _library)
    _session.speed = speed
    _session.load_drill(questions)
    _room.update(
        speed=speed, phase="analysis", scorecard=None, feedback=None,
        progress=f"0 of {len(questions)}",
    )
    kinds = sorted({q.type for q in questions})
    _room.await_start_gate(
        case_title=f"Drill — {len(questions)} questions",
        speed=speed,
        opening=[
            f"This is a drill: {len(questions)} quick questions, "
            f"{' and '.join(kinds)}. No case context — I'll fire them at you "
            "one at a time, and we move fast. Press on when you're ready and "
            "I'll give you the first one."
        ],
    )
    return {
        "room_url": url,
        "room_connected": _room.connected,
        "session_id": _session.id,
        "mode": "drill",
        "awaiting_start": True,
        "next": "The room shows a Start button; the intro plays when they press it. "
                "Then call next_question for the first drill question.",
        "questions": [q.public() for q in questions],
    }


# ───────────────────────────── speaking ────────────────────────────── #

@mcp.tool
def say(
    text: Annotated[str, Field(description="Your own words, delivered to the candidate in the room.")],
    expect_reply: Annotated[bool, Field(
        description="Open the mic afterwards. True for anything the candidate should answer."
    )] = True,
) -> dict:
    """Speak in your own voice — greetings, probes, transitions, feedback.

    Keep it conversational and brief — in voice mode it is read aloud, and in
    text mode it appears as a chat line. Either way: no lists, no markdown, no
    long sentences.

    With `expect_reply` (the default) the microphone opens as soon as you stop
    talking, so the candidate can just answer. Follow with `listen`.
    """
    problem = _room.page_problem()
    seconds = _room.speak(text, wait=True)
    if problem:
        # Speaking into a void looks identical to speaking successfully. Say so.
        return {"spoken": False, "seconds": round(seconds, 1), "mic_open": False,
                "problem": problem,
                "next": "Tell the candidate to fix this before continuing — do not "
                        "carry on as if they heard you."}
    if expect_reply:
        _room.arm(label="reply", target_sec=90,
                  status="Your turn — speak, then press Done.")
    return {"spoken": True, "seconds": round(seconds, 1), "mic_open": expect_reply}


@mcp.tool
def listen(
    max_wait: Annotated[int, Field(ge=5, le=55, description="Seconds to wait this call.")] = 45,
) -> dict:
    """Hear whatever the candidate says next — not tied to any question.

    This is the conversational channel: clarifying questions, "can you repeat
    that", "I'm ready", thinking out loud. If it returns `heard: false` they're
    still talking or thinking — just call it again. Never fill the silence.
    """
    _room.arm(label="reply", target_sec=90, status="Your turn — speak, then press Done.")
    utterance = _room.next_utterance(max_wait=max_wait)
    if utterance is None:
        return {"heard": False, "next": "Still thinking — call listen again."}
    parts = [utterance] + _room.drain()          # multi-message replies arrive whole
    _room.note_picked_up()
    return {"heard": True,
            "transcript": "\n".join(p["text"] for p in parts if p.get("text")),
            "seconds": round(sum(p.get("seconds", 0.0) for p in parts), 1)}


@mcp.tool
def ask_case_prompt() -> dict:
    """Read the case prompt aloud to open the interview.

    Returns no content — the prompt reaches the candidate inside the room only,
    never through this tool result (which would land in the chat transcript). After this, expect
    clarifying questions before they start structuring.
    """
    session = _require_session()
    if not session.case:
        raise ValueError("Drills have no case prompt — the drill intro plays on "
                         "Start. Call next_question.")
    if not _room.started:
        if not _room.wait_for_start(max_wait=45):
            return {"played": False, "awaiting_start": True,
                    "next": "The candidate hasn't pressed Start yet. Call "
                            "ask_case_prompt again to keep waiting."}

    # The opening plays itself the moment Start is pressed. This just waits for
    # it to finish so the interviewer doesn't talk over the case prompt.
    _room.wait_for_opening(max_wait=90)
    _room.update(phase="clarify")
    return {"played": True, "mic_open": True,
            "next": "The greeting and the case prompt have been delivered and the mic "
                    "is open. Call listen() — expect a clarifying question or their "
                    "opening structure. Do not re-read the prompt."}


@mcp.tool
def next_question(
    read_aloud: Annotated[bool, Field(
        description="False when you've already put the question in your own words — "
                    "it binds the grading without repeating yourself."
    )] = True,
    question_id: Annotated[str | None, Field(
        description="Jump to a specific question id (e.g. 'q3') instead of the next in "
                    "order. Use in candidate-led cases when the candidate chooses the "
                    "branch — go where they steered, don't railroad them."
    )] = None,
) -> dict:
    """Advance to the next question and read it aloud.

    Returns metadata only — type, difficulty, time target — never the text.
    Use the type to calibrate: a `structure` question deserves "take a minute
    if you'd like"; a `math` question deserves silence while they work.
    """
    session = _require_session()
    # If the opening is still playing, wait for it. Otherwise the question is
    # spoken over the case prompt and both arm the mic, so the candidate's first
    # answer lands against whichever turn happened to win the race.
    _room.wait_for_opening(max_wait=90)

    q = session.jump_to(question_id) if question_id else session.advance()
    if question_id and q is None:
        raise ValueError(f"No question {question_id!r} in this case. "
                         f"Valid ids: {[x.id for x in session.queue]}")
    if q is None:
        return {"done": True, "next": "Call finish() for the scorecard."}

    # Order matters: put the exhibit up and introduce it BEFORE asking what they
    # make of it. Asking first and arming the mic meant the room recorded silence
    # while the intro was still playing, submitted an empty answer, and then left
    # the candidate with no open mic at all.
    if q.exhibit_id and session.case:
        exhibit = session.case.exhibits.get(q.exhibit_id)
        if exhibit:
            _room.update(exhibit={
                "title": exhibit.get("title"),
                "data": exhibit.get("data"),
                "note": exhibit.get("note"),
                "text": exhibit.get("text"),
                "image_url": exhibit.get("image_url"),
            })
            intro = exhibit.get("read_aloud_intro")
            if intro and read_aloud:
                _room.speak(intro, speed=session.speed)

    seconds = session.ask_current() if read_aloud else 0.0
    # The mic always opens last, after every line has finished playing.
    _room.arm(label=q.type, target_sec=q.time_target_sec,
              status="Your turn — speak, then press Done.")
    return {"spoken": read_aloud, "seconds": round(seconds, 1), "mic_open": True,
            **q.public()}


@mcp.tool
def repeat_question() -> dict:
    """Deliver the current question again. Candidates are allowed to ask."""
    session = _require_session()
    q = session.current
    seconds = session.ask_current()
    _room.arm(label=q.type if q else "reply",
              target_sec=q.time_target_sec if q else 120,
              status="Your turn — speak, then press Done.")
    return {"spoken": True, "seconds": round(seconds, 1), "mic_open": True}


@mcp.tool
def answer_clarification(
    question: Annotated[str, Field(description="The candidate's clarifying question, verbatim.")],
) -> dict:
    """Release one withheld fact, if the candidate asked for it.

    This is the *only* way case data reaches them, and it's deliberate: good
    candidates ask, weak ones assume. If nothing matches, you get `matched:
    false` — say you don't have that information and let them proceed. Don't
    invent facts; you genuinely don't have the case.
    """
    session = _require_session()
    if not session.case:
        return {"matched": False, "reason": "drill mode has no case context"}

    hit = session.case.match_clarification(question)
    if not hit:
        return {
            "matched": False,
            "available_topics": [
                {"id": c["id"], "topic": c["topic"]} for c in session.case.clarifications
            ],
            "next": "If one of these topics is clearly what they meant, call "
                    "release_clarification with its id. Otherwise tell them you "
                    "don't have that data.",
        }

    session.clarifications_asked.append(hit["id"])
    _room.speak(hit["response"], speed=session.speed)
    # Speaking closes the mic; hand the floor straight back. Without this the
    # candidate hears the answer and then has no way to respond to it.
    _room.arm(label="reply", target_sec=120,
              status="Your turn — speak, then press Done.")
    return {"matched": True, "topic": hit["topic"], "spoken": True, "mic_open": True}


@mcp.tool
def release_clarification(
    topic_id: Annotated[str, Field(description="Clarification id from available_topics.")],
) -> dict:
    """Release a specific withheld fact by id, when keyword matching missed it.

    Use only when the candidate's question clearly maps to that topic. You are
    matching intent, not deciding generosity — if they didn't ask for it, don't
    release it.
    """
    session = _require_session()
    if not session.case:
        raise ValueError("no case loaded")
    for c in session.case.clarifications:
        if c["id"] == topic_id:
            session.clarifications_asked.append(topic_id)
            _room.speak(c["response"], speed=session.speed)
            _room.arm(label="reply", target_sec=120,
                      status="Your turn — speak, then press Done.")
            return {"matched": True, "topic": c["topic"], "spoken": True, "mic_open": True}
    raise ValueError(f"no clarification {topic_id!r}")


# ───────────────────────────── listening ───────────────────────────── #

@mcp.tool
def collect_answer(
    max_wait: Annotated[int, Field(ge=5, le=55, description="Seconds to wait this call.")] = 50,
    acknowledge: Annotated[str | None, Field(
        description="A short line spoken the instant their answer lands — 'Got it, "
                    "let me think about that.' Removes the dead air while you reason."
    )] = None,
) -> dict:
    """Listen for the candidate's spoken answer, then grade what can be graded.

    Call it right after asking a question. If it returns `ready: false`, they're
    still thinking — call it again. Don't fill the silence; interviewers let
    candidates work.

    Math answers come back fully graded (deterministic, instant). Framework and
    synthesis answers come back with the rubric's component labels and the
    committed transcript — read them, decide which components the candidate
    actually covered, and pass those ids to `score`.
    """
    session = _require_session()
    q = session.current
    if q is None:
        raise ValueError("No active question.")

    if not _room.state.get("listening"):
        _room.arm(label=q.type, target_sec=q.time_target_sec,
                  status="Your turn — speak, then press Done.")

    utterance = _room.next_utterance(max_wait=max_wait)
    if utterance is None:
        return {"ready": False, "next": "Still working — call collect_answer again."}

    # An answer is EVERYTHING said since the question was asked. People send
    # multi-part answers — several messages in text mode, several bursts in
    # voice — and grading only the first part scored half an answer while the
    # rest leaked into the next question.
    parts = [utterance] + _room.drain()
    if any(part.get("skip") for part in parts):
        _room.take_skip()
        return {"ready": True, "skipped": True, "graded": False,
                "next": "The candidate skipped. Move to next_question without grading."}
    transcript = "\n".join(part["text"] for part in parts if part.get("text"))
    elapsed = sum(part.get("seconds", 0.0) for part in parts)

    if _looks_like_data_request(transcript):
        _room.arm(label=q.type, target_sec=q.time_target_sec,
                  status="Your turn — speak, then press Done.")
        return {"ready": True, "graded": False, "clarification_request": True,
                "transcript": transcript,
                "next": "They're asking for information, not answering. Answer it from "
                        "the question's own data or the case clarifications (or say you "
                        "don't have it), then call collect_answer again. Not graded, "
                        "not logged."}

    if acknowledge:
        _room.speak(acknowledge, speed=session.speed)   # fills the thinking gap
    _room.note_picked_up()
    _pending.update(transcript=transcript, seconds=elapsed)

    if q.rubric.get("kind") == "numeric":
        result = session.score_answer(transcript, elapsed)
        progress_store.log(session.id, session.attempts[-1])
        _pending.clear()
        return {"ready": True, "transcript": transcript, "graded": True, **result}

    return {
        "ready": True,
        "transcript": transcript,
        "seconds": round(elapsed),
        "graded": False,
        "rubric": q.rubric_for_matching(),
        "next": "Decide which component ids the transcript covers, then call score(covered=[...]). "
                "Be strict: credit a component only if they actually raised it, not if it's implied.",
    }


@mcp.tool
def score(
    covered: Annotated[list[str], Field(
        description="Component ids from the rubric that the candidate genuinely covered."
    )],
) -> dict:
    """Record which rubric components the answer covered, and get the verdict.

    You supply the semantic matching; the pass/probe policy lives server-side so
    it's identical for everyone. A PARTIAL verdict with a named gap is your cue
    to `probe` rather than move on.
    """
    session = _require_session()
    if "transcript" not in _pending:
        raise ValueError("No answer awaiting scoring — call collect_answer first.")

    result = session.score_answer(_pending["transcript"], _pending["seconds"], covered=set(covered))
    progress_store.log(session.id, session.attempts[-1])
    _pending.clear()
    return result


@mcp.tool
def probe() -> dict:
    """Get the next hint for the current question, escalating weakest-first.

    Real interviewers nudge before they explain. Speak the probe with `say`,
    then `collect_answer` again. When probes run out, you've done what an
    interviewer would — give the answer and move on.
    """
    session = _require_session()
    text = session.next_probe()
    if text is None:
        return {"exhausted": True,
                "next": "Reveal the answer with reveal_model_answer, then next_question."}
    return {"probe": text, "next": "Speak this with say(), then collect_answer() again."}


@mcp.tool
def reveal_model_answer() -> dict:
    """The casebook's own answer for the current question.

    Only after grading. Use it to explain what a strong answer sounds like —
    paraphrase it conversationally rather than reading it out verbatim.
    """
    session = _require_session()
    q = session.current
    if not q:
        raise ValueError("No active question.")
    if not session.attempts or session.attempts[-1].uid != q.uid:
        raise ValueError("Grade the answer before revealing the model answer.")
    return {"model_answer": q.model_answer, "type": q.type}


@mcp.tool
def show_exhibit(
    exhibit_id: Annotated[str | None, Field(description="Exhibit id; omit for the current question's.")] = None,
) -> dict:
    """Display a chart or table in the room.

    Exhibits are the one thing the candidate is meant to see — a real
    interviewer slides paper across the table. Speak the intro line, then give
    them a moment before asking what they make of it.
    """
    session = _require_session()
    if not session.case:
        raise ValueError("drills have no exhibits")
    ex_id = exhibit_id or (session.current.exhibit_id if session.current else None)
    exhibit = session.case.exhibits.get(ex_id or "")
    if not exhibit:
        raise ValueError(f"no exhibit {ex_id!r}")

    _room.update(exhibit={
        "title": exhibit.get("title"), "data": exhibit.get("data"),
        "note": exhibit.get("note"),
                "text": exhibit.get("text"), "image_url": exhibit.get("image_url"),
    })
    intro = exhibit.get("read_aloud_intro")
    if intro:
        _room.speak(intro, speed=session.speed)
    # Speaking closes the mic; hand the floor straight back or they're stranded.
    _room.arm(label="exhibit", target_sec=150,
              status="Your turn — speak, then press Done.")
    return {"shown": True, "title": exhibit.get("title"), "mic_open": True}


# ─────────────────────────── introspection ─────────────────────────── #

@mcp.tool
def room_status(
    events: Annotated[int, Field(ge=0, le=200, description="Recent log lines to include.")] = 25,
) -> dict:
    """Inspect the interview room: state, page health, and a timestamped log.

    Use this the moment anything looks wrong — silence, a stall, an answer that
    never arrived. It reports what was spoken, whether the page acknowledged it,
    whether the mic is open and whether a tab is even connected, so you can
    diagnose without asking the candidate what they see on screen.

    `page_problem` is the single most useful field: it names the reason the room
    won't work (no tab, stale build, audio still locked) or is null when healthy.
    """
    return {**_room.diagnostics(), "events": _room.events(limit=events),
            "room_url": _server.url}


@mcp.tool
def room_act(
    action: Annotated[Literal["press_start", "say", "open_mic", "ack_speech"], Field(
        description="press_start = click Start; say = submit an utterance as the candidate; "
                    "open_mic = arm the microphone; ack_speech = acknowledge playback."
    )],
    text: Annotated[str | None, Field(description="For action='say', what the candidate said.")] = None,
) -> dict:
    """Drive the room as if you were the candidate. For testing, not for cheating.

    This lets you rehearse or diagnose the whole flow with nobody at the
    keyboard — press Start, submit an answer, confirm the loop advances. During
    a real interview, don't answer on the candidate's behalf.
    """
    if action == "press_start":
        _room.page_info = {**_room.page_info, "audio_unlocked": True}
        _room.mark_started()
    elif action == "say":
        _room.submit_transcript(text or "")
    elif action == "open_mic":
        _room.arm()
    elif action == "ack_speech":
        _room.mark_spoken(None)
    _room.log("agent.act", action=action)
    return {"ok": True, "action": action, **_room.diagnostics()}


# ───────────────────────────── closing ─────────────────────────────── #

@mcp.tool
def finish() -> dict:
    """End the session and return the scorecard.

    Four dimensions, rated 1–5, where 3 is the bar. Note `limiting_factor` —
    real interviewers decide on the weakest box, not the average, so that's what
    the candidate should work on. `recurring_habits` are patterns seen more than
    once; those matter more than any single answer.
    """
    session = _require_session()
    return session.debrief()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
