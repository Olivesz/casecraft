"""The conversation loop, end to end, with a simulated candidate.

Every test here is a regression for something that actually broke a live
session. The loop is the product — if it stalls, nothing else matters — so all
of it runs under a hard time bound.
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("CASECRAFT_NO_BROWSER", "1")
os.environ.setdefault("CASECRAFT_DB", "/tmp/casecraft-conversation.db")

from fastmcp import Client                              # noqa: E402

from casecraft import server as srv                     # noqa: E402
from casecraft.session import Room                      # noqa: E402
from casecraft.room import RoomServer                   # noqa: E402
from tests.harness import CANDIDATE_SCRIPT, SimulatedCandidate, no_stall   # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def room_pair():
    """A fresh room + server, isolated from the module-level singletons."""
    room = Room()
    server = RoomServer(room)
    url = server.start()
    try:
        yield room, url
    finally:
        server.stop()          # release the port; otherwise the suite exhausts them


@pytest.fixture
def voice_pair():
    """A room in voice mode, for the tests that exercise playback itself."""
    room = Room(mode="voice")
    server = RoomServer(room)
    url = server.start()
    try:
        yield room, url
    finally:
        server.stop()


def _connect(url, answers=None, **kw) -> SimulatedCandidate:
    candidate = SimulatedCandidate(url, answers, **kw)
    candidate.start()
    assert candidate.wait_until_connected(), "candidate never connected"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not candidate.heard and not candidate.statuses:
        time.sleep(0.02)
    return candidate


# ── the loop must never hang ─────────────────────────────────────────────── #

def test_speaking_returns_promptly(voice_pair):
    room, url = voice_pair
    candidate = _connect(url)
    with no_stall(6, "speak"):
        room.speak("Can you hear me?")
    assert candidate.heard_containing("Can you hear me")
    candidate.stop()


def test_a_dead_page_cannot_hang_the_interviewer(voice_pair):
    """The 120-second silent hang, pinned.

    A tab that never acknowledges speech — backgrounded, crashed, closed — used
    to block the interviewer for two minutes per line.
    """
    room, url = voice_pair
    candidate = _connect(url, deaf=True)
    with no_stall(12, "speak against a deaf page"):
        room.speak("This line is never acknowledged by the page.")
    candidate.stop()


def test_no_page_at_all_cannot_hang_the_interviewer(voice_pair):
    room, _url = voice_pair
    with no_stall(12, "speak with nobody listening"):
        room.speak("Nobody is connected to hear this.")


def test_a_silent_candidate_does_not_hang_the_interviewer(room_pair):
    room, url = room_pair
    candidate = _connect(url, mute=True)
    room.arm()
    with no_stall(4, "listen for a silent candidate"):
        assert room.next_utterance(max_wait=1.5) is None
    candidate.stop()


# ── the candidate owns the microphone ────────────────────────────────────── #

def test_candidate_can_answer_a_spoken_question(room_pair):
    """The exact failure from the live run: 'can you hear me?' was unanswerable.

    Speaking with `expect_reply` must open the mic, and the reply must arrive.
    """
    room, url = room_pair
    candidate = _connect(url, ["Yes, I can hear you."])

    with no_stall(8, "ask-and-hear round trip"):
        room.speak("Can you hear me?")
        room.arm()
        heard = room.next_utterance(max_wait=5)

    assert heard is not None, "the candidate had no way to reply"
    assert "hear you" in heard["text"]
    candidate.stop()


def test_speech_before_being_asked_is_not_lost(room_pair):
    """Utterances queue. Talking early must never be dropped on the floor."""
    room, url = room_pair
    candidate = _connect(url)

    candidate.speak_unprompted("Actually, can I ask a clarifying question first?")
    time.sleep(0.3)

    heard = room.next_utterance(max_wait=3)
    assert heard is not None and "clarifying question" in heard["text"]
    candidate.stop()


def test_two_utterances_in_a_row_both_survive(room_pair):
    room, url = room_pair
    candidate = _connect(url)

    candidate.speak_unprompted("First thought.")
    time.sleep(0.15)
    candidate.speak_unprompted("Second thought.")
    time.sleep(0.3)

    first = room.next_utterance(max_wait=3)
    second = room.next_utterance(max_wait=3)
    assert first and second
    assert "First" in first["text"] and "Second" in second["text"]
    candidate.stop()


# ── a whole case, driven by the real tools ───────────────────────────────── #

@pytest.mark.anyio
async def test_full_case_conversation_completes():
    """A complete interview, start to scorecard, with nobody at the keyboard.

    This is the test that replaces manual run-throughs: if it passes, the loop
    a real candidate walks into works.
    """
    url = srv._server.start()
    candidate = SimulatedCandidate(url, [
        CANDIDATE_SCRIPT["greeting"],
        CANDIDATE_SCRIPT["clarify"],
        CANDIDATE_SCRIPT["structure"],
        CANDIDATE_SCRIPT["math_wrong"],
        CANDIDATE_SCRIPT["math_right"],
        CANDIDATE_SCRIPT["exhibit"],
        CANDIDATE_SCRIPT["synthesis"],
    ])
    candidate.start()
    assert candidate.wait_until_connected()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not srv._room.connected:
        time.sleep(0.02)

    try:
        async with Client(srv.mcp) as c:
            with no_stall(90, "the whole interview"):
                started = (await c.call_tool(
                    "start_case", {"case_id": "casecraft-orchid-airlines"})).data
                assert started["room_connected"]

                # greeting → the candidate can actually answer it
                said = (await c.call_tool("say", {"text": "Can you hear me alright?"})).data
                assert said["mic_open"] is True
                reply = (await c.call_tool("listen", {"max_wait": 10})).data
                assert reply["heard"], "greeting went unanswered — the loop is broken"

                # prompt → clarifying question → answered
                await c.call_tool("ask_case_prompt", {})
                clarifying = (await c.call_tool("listen", {"max_wait": 10})).data
                assert clarifying["heard"]
                released = (await c.call_tool(
                    "answer_clarification", {"question": clarifying["transcript"]})).data
                assert released["matched"] is True

                # every remaining question gets asked, answered and graded
                graded = 0
                diagnosed: list[str] = []
                while True:
                    nxt = (await c.call_tool("next_question", {})).data
                    if nxt.get("done"):
                        break
                    answer = (await c.call_tool("collect_answer", {"max_wait": 15})).data
                    if not answer.get("ready"):
                        answer = (await c.call_tool("collect_answer", {"max_wait": 15})).data
                    assert answer.get("ready"), f"no answer for {nxt.get('uid')}"

                    if answer.get("graded"):
                        # Math is graded in-process. The wrong answer must come
                        # back DIAGNOSED, not merely marked wrong.
                        if answer.get("outcome") == "wrong":
                            diagnosed.append(answer.get("note") or "")
                    else:
                        ids = [x["id"] for x in answer["rubric"]["components"]]
                        await c.call_tool("score", {"covered": ids[:2]})
                    graded += 1

                assert graded == 5, f"expected 5 graded questions, got {graded}"
                card = (await c.call_tool("finish", {})).data

        assert card["questions_answered"] == 5
        assert any("load factor" in d for d in diagnosed), \
            f"wrong math was marked but not diagnosed: {diagnosed}"
        assert card["limiting_factor"] in card["dimensions"]
        assert candidate.said, "the candidate never got to speak"
        assert candidate.feedback, "no feedback ever reached the room"
    finally:
        candidate.stop()


# ── the room tells us when it can't work ─────────────────────────────────── #

def test_no_room_is_reported(room_pair):
    room, _url = room_pair
    assert "No interview room" in (room.page_problem() or "")


def test_a_tab_that_never_identified_itself_is_flagged(room_pair):
    """A tab left open across a code change behaves like the old version.

    That cost a whole live run: speech looked like it succeeded while nobody
    could hear it. The server must be able to say so out loud.
    """
    room, url = room_pair
    candidate = _connect(url, announce=False)
    assert "old code" in (room.page_problem() or "")
    candidate.stop()


def test_a_stale_build_is_flagged(room_pair):
    room, url = room_pair
    candidate = _connect(url)
    room.page_info = {"build": "ancient", "audio_unlocked": True}
    problem = room.page_problem() or ""
    assert "ancient" in problem and "Reload" in problem
    candidate.stop()


def test_locked_audio_is_flagged(voice_pair):
    room, url = voice_pair
    candidate = _connect(url)
    room.page_info = {"build": room.BUILD, "audio_unlocked": False}
    assert "blocking audio" in (room.page_problem() or "")
    candidate.stop()


def test_a_healthy_page_reports_no_problem(room_pair):
    room, url = room_pair
    candidate = _connect(url)
    room.page_info = {"build": room.BUILD, "audio_unlocked": True}
    assert room.page_problem() is None
    candidate.stop()


# ── the start gate ───────────────────────────────────────────────────────── #

def test_nothing_is_spoken_before_the_candidate_presses_start(room_pair):
    """The room used to open silent and stay silent — no way in, nothing said."""
    room, url = room_pair
    candidate = _connect(url, wont_start=True)      # candidate hasn't pressed yet
    room.await_start_gate(case_title="Orchid Airlines")
    assert room.state["awaiting_start"] is True
    assert room.started is False
    assert not room.wait_for_start(max_wait=0.3)
    candidate.stop()


def test_pressing_start_opens_the_gate_and_unlocks_audio(room_pair):
    import httpx
    room, url = room_pair
    candidate = _connect(url, wont_start=True)
    room.page_info = {"build": room.BUILD, "audio_unlocked": False}
    room.await_start_gate(case_title="Orchid Airlines")

    httpx.post(f"{url}/session/start", json={}, timeout=10)   # the candidate clicks

    assert room.wait_for_start(max_wait=3)
    assert room.state["awaiting_start"] is False
    assert room.page_info["audio_unlocked"] is True     # the click is the gesture
    assert room.page_problem() is None
    candidate.stop()


def test_a_candidate_who_never_starts_does_not_hang_us(room_pair):
    room, url = room_pair
    candidate = _connect(url, wont_start=True)
    room.await_start_gate(case_title="Orchid Airlines")
    with no_stall(4, "waiting on a Start that never comes"):
        assert room.wait_for_start(max_wait=1.5) is False
    candidate.stop()


# ── the opening plays itself ─────────────────────────────────────────────── #

def test_pressing_start_reads_the_greeting_and_the_case_unprompted(room_pair):
    """The failure: Start was pressed and nothing was ever said.

    The opening must not depend on anyone being blocked in a tool call at that
    exact moment — press Start, hear the interview begin.
    """
    import httpx
    room, url = room_pair
    candidate = _connect(url, wont_start=True)

    room.await_start_gate(
        case_title="Orchid Airlines",
        opening=["Welcome, and thanks for making the time.",
                 "Our client is a regional airline whose profits have fallen.",
                 "Take a moment, then tell me how you'd structure this."],
    )
    assert not candidate.heard, "nothing should be spoken before Start"

    httpx.post(f"{url}/session/start", json={}, timeout=10)

    assert room.wait_for_opening(max_wait=20), "the opening never finished"
    assert candidate.wait_for_heard(3), f"only got {candidate.heard}"
    assert candidate.heard_containing("Welcome")
    assert candidate.heard_containing("regional airline")
    assert candidate.heard_containing("structure this")
    assert room.state["listening"] is True, "the mic must be open once the case is read"
    candidate.stop()


def test_the_opening_is_spoken_in_order(room_pair):
    import httpx
    room, url = room_pair
    candidate = _connect(url, wont_start=True)
    room.await_start_gate(case_title="X", opening=["first line", "second line", "third line"])
    httpx.post(f"{url}/session/start", json={}, timeout=10)
    assert room.wait_for_opening(max_wait=20)
    assert candidate.wait_for_heard(3), f"only got {candidate.heard}"
    assert candidate.heard == ["first line", "second line", "third line"]
    candidate.stop()


def test_start_is_idempotent(room_pair):
    """Double-clicking Start must not read the case twice."""
    import httpx
    room, url = room_pair
    candidate = _connect(url, wont_start=True)
    room.await_start_gate(case_title="X", opening=["only once"])
    httpx.post(f"{url}/session/start", json={}, timeout=10)
    httpx.post(f"{url}/session/start", json={}, timeout=10)
    assert room.wait_for_opening(max_wait=20)
    assert candidate.wait_for_heard(1), f"only got {candidate.heard}"
    time.sleep(0.4)
    assert candidate.heard.count("only once") == 1
    candidate.stop()


# ── agent observability ──────────────────────────────────────────────────── #

def test_the_log_records_what_was_spoken_and_whether_it_landed(voice_pair):
    room, url = voice_pair
    candidate = _connect(url)
    room.speak("Testing the flight recorder.")
    kinds = [e["event"] for e in room.events(limit=50)]
    assert "speak.start" in kinds and "speak.end" in kinds and "speak.ack" in kinds
    end = [e for e in room.events(limit=50) if e["event"] == "speak.end"][-1]
    assert end["acked"] is True
    candidate.stop()


def test_the_log_shows_when_speech_was_never_acknowledged(voice_pair):
    """A deaf page is indistinguishable from a working one without this."""
    room, url = voice_pair
    candidate = _connect(url, deaf=True)
    room.speak("Nobody will acknowledge this.")
    end = [e for e in room.events(limit=50) if e["event"] == "speak.end"][-1]
    assert end["acked"] is False
    candidate.stop()


def test_diagnostics_name_the_reason_the_room_is_broken(room_pair):
    room, _url = room_pair
    diag = room.diagnostics()
    assert diag["connected_tabs"] == 0
    assert "No interview room" in diag["page_problem"]


def test_an_agent_can_drive_the_whole_opening_with_no_browser(room_pair):
    """The point of the agent surface: rehearse the flow with nobody present."""
    import httpx
    room, url = room_pair
    candidate = _connect(url, wont_start=True)
    room.await_start_gate(case_title="X", opening=["Welcome.", "Here is the case."])

    httpx.post(f"{url}/debug/act", json={"action": "press_start"}, timeout=10)
    assert room.wait_for_opening(max_wait=20)

    httpx.post(f"{url}/debug/act", json={"action": "say", "text": "My structure is..."},
               timeout=10)
    heard = room.next_utterance(max_wait=3)
    assert heard and "My structure" in heard["text"]

    diag = httpx.get(f"{url}/debug", timeout=10).json()
    assert diag["started"] is True
    assert any(e["event"] == "heard" for e in diag["events"])
    candidate.stop()


# ── half-duplex: the interviewer must never be transcribed ───────────────── #

def test_speaking_closes_the_microphone(room_pair):
    """The echo bug: the mic stayed open while the interviewer talked.

    Speech recognition then transcribed the interviewer's own words and filed
    them as the candidate's answer, corrupting the grade for that question.
    """
    room, url = room_pair
    candidate = _connect(url)

    room.arm()
    assert room.state["listening"] is True

    room.speak("This is the interviewer talking.")
    assert room.state["listening"] is False, "the mic was open while we spoke"

    kinds = [e["event"] for e in room.events(limit=30)]
    assert "mic.close.for_speech" in kinds
    candidate.stop()


def test_the_mic_reopens_after_the_interviewer_finishes(room_pair):
    room, url = room_pair
    candidate = _connect(url, ["my answer"])

    room.arm()
    room.speak("A question for you.")
    assert room.state["listening"] is False
    room.arm()                                   # interviewer hands the floor back

    heard = room.next_utterance(max_wait=5)
    assert heard and "my answer" in heard["text"]
    candidate.stop()


def test_the_opening_never_leaves_the_mic_open_mid_script(room_pair):
    """Three lines are read back to back; none may be recorded."""
    import httpx
    room, url = room_pair
    candidate = _connect(url, wont_start=True)
    room.await_start_gate(case_title="X", opening=["line one", "line two", "line three"])
    httpx.post(f"{url}/session/start", json={}, timeout=10)
    assert room.wait_for_opening(max_wait=25)
    assert candidate.wait_for_heard(3), f"only got {candidate.heard}"

    events = room.events(limit=80)
    opens = [i for i, e in enumerate(events) if e["event"] == "mic.open"]
    # "said" in text mode, "speak.start" in voice mode — same turn either way.
    speaks = [i for i, e in enumerate(events)
              if e["event"] in ("speak.start", "said")]
    # The only mic.open must come after the last spoken line.
    assert opens and speaks and opens[-1] > speaks[-1]
    assert len(opens) == 1, "the mic opened more than once during the opening"
    assert room.state["listening"] is True
    candidate.stop()


# ── turn ownership ───────────────────────────────────────────────────────── #

def test_the_floor_changes_hands_explicitly(room_pair):
    room, url = room_pair
    candidate = _connect(url)
    assert room.state["floor"] == "waiting"

    room.speak("Interviewer talking.")
    assert room.state["floor"] == "interviewer"

    room.arm()
    assert room.state["floor"] == "candidate"
    candidate.stop()


def test_a_captured_answer_says_it_is_waiting_not_thinking(room_pair):
    """The stall looked like a hang because the room claimed to be thinking.

    Nothing was thinking — the answer was queued and no one had picked it up.
    """
    room, url = room_pair
    candidate = _connect(url, ["here is my structure"])
    room.arm()
    assert room.next_utterance(max_wait=5)

    assert room.state["floor"] == "waiting"
    assert "waiting for the interviewer" in room.state["status"].lower()
    assert "thinking" not in room.state["status"].lower()
    candidate.stop()


def test_picking_up_an_answer_updates_the_room(room_pair):
    room, url = room_pair
    candidate = _connect(url, ["my answer"])
    room.arm()
    room.next_utterance(max_wait=5)
    room.note_picked_up()
    assert "considering" in room.state["status"].lower()
    candidate.stop()


def test_a_question_can_be_bound_without_being_re_read(room_pair):
    """Binding grading to a question you already asked in your own words."""
    from casecraft.library import Library
    from casecraft.session import Session
    room, url = room_pair
    candidate = _connect(url)
    session = Session(room, Library())
    session.load_case(Library().cases["casecraft-orchid-airlines"])
    session.advance()
    before = len(candidate.heard)
    room.arm(label="structure", target_sec=180)      # no ask_current() call
    assert len(candidate.heard) == before, "nothing should have been spoken"
    assert room.state["floor"] == "candidate"
    candidate.stop()


# ── bugs found in the first real run ─────────────────────────────────────── #

def test_an_empty_submission_does_not_consume_the_turn(room_pair):
    """A stray Done press graded the candidate zero on an unanswered question."""
    room, url = room_pair
    candidate = _connect(url)
    room.arm()
    room.submit_transcript("   ")

    assert room.next_utterance(max_wait=1) is None, "an empty answer was queued"
    assert room.state["listening"] is True, "the turn must stay open"
    # A pure no-op: re-arming here pushed new state, the page reacted by
    # submitting again, and the loop locked the candidate out entirely.
    assert any(e["event"] == "heard.empty.ignored" for e in room.events(limit=20))
    assert not any(e["event"] == "mic.open" and e.get("label") == "retry"
                   for e in room.events(limit=30)), "empty submission re-armed the mic"
    candidate.stop()


def test_a_real_answer_after_an_empty_one_still_lands(room_pair):
    room, url = room_pair
    candidate = _connect(url)
    room.arm()
    room.submit_transcript("")
    room.submit_transcript("here is the real answer")
    heard = room.next_utterance(max_wait=3)
    assert heard and heard["text"] == "here is the real answer"
    candidate.stop()


# ── skip, and the exhibit hand-off ───────────────────────────────────────── #

def test_skip_unblocks_a_waiting_interviewer(room_pair):
    """Testing the flow shouldn't require answering every question properly."""
    import httpx
    room, url = room_pair
    candidate = _connect(url)
    room.arm()

    httpx.post(f"{url}/session/skip", json={}, timeout=10)

    utterance = room.next_utterance(max_wait=3)
    assert utterance and utterance.get("skip") is True
    assert room.take_skip() is True
    assert room.state["floor"] == "waiting"
    candidate.stop()


def test_speaking_always_hands_the_floor_back(room_pair):
    """The exhibit bug: an intro was spoken and the mic was never reopened."""
    room, url = room_pair
    candidate = _connect(url)
    room.arm()
    room.speak("I'm showing you a chart of cost per available seat mile.")
    assert room.state["listening"] is False      # closed while speaking
    room.arm()                                   # callers must hand it back
    assert room.state["listening"] is True
    assert room.state["floor"] == "candidate"
    candidate.stop()


# ── invariant: speaking must always hand the floor back ──────────────────── #

def test_every_speaking_tool_reopens_the_microphone():
    """A tool that speaks and doesn't re-arm strands the candidate.

    This exact bug shipped twice — once in `answer_clarification`, once in
    `repeat_question` — because it is invisible unless you happen to exercise
    that path. Enforce the class, not the instance.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "casecraft" / "server.py").read_text()
    stranded = []
    for m in re.finditer(r"@mcp\.tool\n(?:async )?def (\w+)\(.*?\n\n\n", src, re.S):
        name, body = m.group(1), m.group(0)
        speaks = "_room.speak(" in body or "session.ask_current()" in body
        if speaks and "_room.arm(" not in body:
            stranded.append(name)
    assert not stranded, f"these tools speak but never reopen the mic: {stranded}"


# ── the page must never invent turn state locally ────────────────────────── #

def test_the_page_never_hardcodes_a_floor():
    """The talk button unlocked itself while the interviewer was speaking.

    `stopListening()` painted the button with a hardcoded "waiting", so ending
    a turn silently unlocked the mic even when the floor had passed to the
    interviewer — letting the candidate talk over them and be recorded doing it.
    Turn state is server state; the page may only mirror it.
    """
    import re
    from pathlib import Path

    page = (Path(__file__).resolve().parent.parent
            / "casecraft" / "static" / "room.html").read_text()

    # Any paint call must use the live floor, never a literal — except the two
    # places where the value is definitionally correct (we just started
    # listening, so the floor IS the candidate's).
    bad = [c for c in re.findall(r'paintTalkButton\([^)]*\)', page)
           if re.search(r'"(waiting|interviewer)"', c)]
    assert not bad, f"button painted from a hardcoded floor: {bad}"
    assert "currentFloor = floor" in page, "the page must mirror the server's floor"


def test_the_page_disables_the_button_for_the_interviewer_floor():
    from pathlib import Path
    page = (Path(__file__).resolve().parent.parent
            / "casecraft" / "static" / "room.html").read_text()
    assert 'const theirTurn = floor === "interviewer"' in page
    assert "el.talk.disabled = theirTurn" in page
    assert "if(!el.talk.disabled) el.talk.onclick()" in page, \
        "the spacebar shortcut must respect the lock too"


def test_a_new_case_cannot_grade_the_previous_case_s_answer():
    """`_pending` is module state and outlives the Session that filled it.

    Without an explicit clear, an answer captured in one case could be scored
    against a different case's rubric — silently, with a plausible-looking
    verdict. It only surfaced in testing because the new session happened not
    to have an active question yet.
    """
    from casecraft import server as srv

    srv._pending.update(transcript="answer from the previous case", seconds=10.0)
    srv._room.submit_transcript("stale utterance")

    srv._pending.clear()
    srv._room.drain()

    assert "transcript" not in srv._pending
    assert srv._room.next_utterance(max_wait=0.2) is None


def test_starting_a_session_clears_pending_state():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "casecraft" / "server.py").read_text()
    assert src.count("_pending.clear()\n    _room.drain()") == 2, \
        "both start_case and start_drill must clear stale answers"


def test_next_question_cannot_talk_over_the_opening(room_pair):
    """Asking a question mid-opening made two turns compete for one answer."""
    import httpx
    room, url = room_pair
    candidate = _connect(url, wont_start=True)
    room.await_start_gate(case_title="X", opening=["line one", "line two", "line three"])
    httpx.post(f"{url}/session/start", json={}, timeout=10)

    # Whoever asks next must block until the opening has finished speaking.
    assert room.wait_for_opening(max_wait=25)
    assert candidate.wait_for_heard(3), f"only got {candidate.heard}"
    assert candidate.heard == ["line one", "line two", "line three"]
    candidate.stop()


def test_rearming_an_open_mic_does_not_wipe_the_answer(room_pair):
    """`listen` re-arms every call, and callers are told to call it repeatedly.

    Resetting an already-open mic restarted the candidate's timer and cleared
    the transcript they were part-way through — losing what they'd already said.
    """
    room, url = room_pair
    candidate = _connect(url)
    room.arm(label="structure", target_sec=180)
    first_timer = room.state["timer"]
    room.update(transcript="half of an answer so far")

    room.arm(label="structure", target_sec=180)      # the second listen() call

    assert room.state["timer"] == first_timer, "the timer restarted"
    assert room.state["transcript"] == "half of an answer so far", "the answer was wiped"
    assert room.state["listening"] is True
    candidate.stop()


# ── text mode ────────────────────────────────────────────────────────────── #

def test_text_mode_prints_instead_of_speaking(room_pair):
    """Text mode removes playback, so there is no acknowledgement to wait for."""
    room, url = room_pair
    candidate = _connect(url, deaf=True)          # would hang the voice path
    assert room.mode == "text"

    with no_stall(3, "speaking in text mode"):
        elapsed = room.speak("Our client is a regional airline.")
    assert elapsed == 0.0

    lines = room.state["dialogue"]
    assert lines and lines[-1] == {"who": "interviewer",
                                   "text": "Our client is a regional airline.", "n": 1}
    candidate.stop()


def test_text_mode_records_both_sides_in_order(room_pair):
    room, url = room_pair
    candidate = _connect(url)
    room.speak("What factors would you consider?")
    room.arm()
    room.submit_transcript("Revenue and costs.")
    room.speak("Good. Anything else?")

    said = [(d["who"], d["text"]) for d in room.state["dialogue"]]
    assert said == [
        ("interviewer", "What factors would you consider?"),
        ("candidate", "Revenue and costs."),
        ("interviewer", "Good. Anything else?"),
    ]
    candidate.stop()


def test_text_mode_needs_no_audio_unlock(room_pair):
    """The click-to-unlock requirement is a voice-path concern only."""
    room, url = room_pair
    candidate = _connect(url)
    room.page_info = {"build": room.BUILD, "audio_unlocked": False}
    assert room.page_problem() is None
    room.mode = "voice"
    assert "blocking audio" in (room.page_problem() or "")
    candidate.stop()


def test_text_mode_opening_plays_without_a_browser(room_pair):
    import httpx
    room, url = room_pair
    candidate = _connect(url, wont_start=True)
    room.await_start_gate(case_title="X", opening=["Welcome.", "Here is the case.", "Your turn."])
    httpx.post(f"{url}/session/start", json={}, timeout=10)

    with no_stall(10, "text-mode opening"):
        assert room.wait_for_opening(max_wait=8)
    assert [d["text"] for d in room.state["dialogue"]] == \
        ["Welcome.", "Here is the case.", "Your turn."]
    assert room.state["listening"] is True
    candidate.stop()


def test_the_opening_is_one_continuous_briefing(room_pair):
    """Three chunks read as three turns and invited interruption mid-setup.

    No interviewer pauses after "hello" to see whether you'd like to speak.
    """
    import asyncio
    from fastmcp import Client
    from casecraft import server as srv

    # Connect first: otherwise start_case waits 45s for a browser to appear.
    url = srv._server.start()
    candidate = _connect(url)

    async def go():
        async with Client(srv.mcp) as c:
            return (await c.call_tool(
                "start_case", {"case_id": "casecraft-orchid-airlines"})).data

    try:
        asyncio.run(go())
    finally:
        candidate.stop()
    assert len(srv._room._opening) == 1, \
        f"the brief must be one turn, got {len(srv._room._opening)}"
    text = srv._room._opening[0]
    assert "Good to meet you" in text and "Orchid Airlines" in text
    assert "tell me how you'd structure" not in text, \
        "the prompt already asks the question; don't ask again"


# ── real-interview mechanics ─────────────────────────────────────────────── #

def test_a_data_request_is_not_an_answer():
    """"What's the load factor?" must be a conversation, not a graded zero."""
    from casecraft.server import _looks_like_data_request as req

    assert req("What's the load factor?")
    assert req("Sorry, can you repeat the numbers?")
    assert req("do we know their fixed costs")
    assert not req("I make it about 2.89 billion a year.")       # an answer
    assert not req("Is it 2.3 billion?")                          # proposed answer
    assert not req("What factors matter? Three things: revenue, fixed costs, "
                   "and variable costs, starting with the revenue build "
                   "because that is where the volume story lives.")   # long = answer


def test_a_multi_message_answer_is_one_answer(room_pair):
    """Each Enter used to submit a complete answer; the rest of a multi-part
    answer was then graded against the NEXT question."""
    room, url = room_pair
    candidate = _connect(url)
    room.arm()
    room.submit_transcript("First, the revenue build.")
    room.submit_transcript("Second, fixed versus variable costs.")

    first = room.next_utterance(max_wait=3)
    rest = room.drain()
    joined = "\n".join([first["text"]] + [r["text"] for r in rest])
    assert "revenue build" in joined and "variable costs" in joined
    candidate.stop()


def test_scorecard_counts_questions_not_attempts():
    """A probe-and-retry reported "6 questions answered" in a 5-question case."""
    from casecraft.library import Library
    from casecraft.session import Room, Session

    session = Session(Room(), Library())
    session.load_case(Library().cases["casecraft-orchid-airlines"])
    session.advance()
    session.score_answer("about 2.89 billion", 20)     # wrong attempt
    session.score_answer("about 2.3 billion", 20)      # retry, same question
    card = session.debrief()
    assert card["questions_answered"] == 1
    assert card["attempts"] == 2
