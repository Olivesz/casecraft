"""Interview session state — and the bridge between MCP and the browser room.

Two processes-worth of concerns meet here:

  * The **MCP side** (host Claude) drives: speak this, listen now, grade that.
  * The **room side** (the browser tab) does what MCP structurally cannot —
    push audio out of the speakers and pull it in from the mic.

`Room` is the shared mailbox between them. Claude's tools mutate it; the page
subscribes over SSE and reacts. Crucially, the case prompt travels
Room → browser → speakers and *never* back through MCP, which is what keeps it
off the candidate's screen.

Threading: uvicorn serves the room on a background thread while FastMCP owns the
main thread. Every mutation goes through one lock, and waiting for an answer is
an `Event`, not a poll — so a candidate can think for ninety seconds without the
server spinning.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .delivery import analyze
from .library import Case, Library, Question
from .scoring import Scorecard, Verdict, grade

# Phases of a real case, in order. The room renders these; the host model is
# told which one it's in so it behaves like an interviewer rather than a quizzer.
PHASES = ("idle", "prompt", "clarify", "structure", "analysis", "synthesis", "debrief")

# Words per minute for the three delivery speeds. The browser's speech synthesis
# takes a rate multiplier where 1.0 ≈ 170 wpm, so these map to 0.75 / 0.95 / 1.1.
SPEEDS = {"slow": 0.75, "moderate": 0.95, "regular": 1.1}


@dataclass
class Attempt:
    uid: str
    question_type: str
    dimension: str
    tags: list[str]
    difficulty: int
    transcript: str
    seconds: float
    verdict: Verdict | None = None
    delivery: dict = field(default_factory=dict)
    probes_used: int = 0


class Room:
    """Shared state between the MCP tools and the browser tab."""

    # Bumped whenever room.html changes in a way a stale tab would get wrong.
    BUILD = "2026-08-03-loop"

    # Every state change is recorded here. An agent diagnosing this room should
    # never need a human to look at the screen and describe what they see —
    # the log says what was spoken, what was heard, what was acknowledged, and
    # exactly when. That is the difference between debugging and guessing.
    LOG_LIMIT = 400

    # "text"  — the interviewer's lines are printed; the candidate types.
    # "voice" — spoken aloud, microphone captured.
    #
    # Text is the default while the conversation logic is being proven: it
    # removes audio unlock, playback acknowledgement, microphone permission,
    # transcription error and half-duplex timing in one move, so a failure is
    # unambiguously a failure of the interview logic. The voice path is intact
    # behind the flag, and the turn model is identical, so switching over later
    # changes how a line is delivered, not when.
    def __init__(self, mode: str = "text") -> None:
        self.mode = mode
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._spoken = threading.Event()
        self.state: dict[str, Any] = {
            "phase": "idle",
            "status": "Waiting for Claude to start a session…",
            "speak": None,
            "listening": False,
            "transcript": "",
            "exhibit": None,
            "feedback": None,
            "timer": None,
            "progress": None,
            "scorecard": None,
            "speed": "moderate",
            "awaiting_start": False,
            "case_title": None,
            # Who holds the floor: "interviewer" | "candidate" | "waiting".
            # Exactly one party speaks at a time, and the page enforces it —
            # an open mic during playback records the interviewer's own voice.
            "floor": "waiting",
            "mode": mode,
            # The visible conversation, in text mode. Append-only.
            "dialogue": [],
        }
        self._utterances: queue.Queue[dict] = queue.Queue()
        self._listen_started: float = time.monotonic()
        self._speaking_id: str | None = None
        self.page_info: dict[str, Any] = {}
        self._started = threading.Event()
        self._opening_done = threading.Event()
        self._opening: list[str] = []
        self._opening_speed = "moderate"
        self._log: list[dict] = []
        self._t0 = time.monotonic()
        self._closing = threading.Event()
        self._skip = threading.Event()

    def close(self) -> None:
        """Tell open SSE streams to finish so the server can actually shut down."""
        self._closing.set()
        self.update(status="Room closed.")

    @property
    def closing(self) -> bool:
        return self._closing.is_set()

    # ── flight recorder ──────────────────────────────────────────────────── #

    def log(self, event: str, **data: Any) -> None:
        entry = {"t": round(time.monotonic() - self._t0, 3), "event": event, **data}
        with self._lock:
            self._log.append(entry)
            if len(self._log) > self.LOG_LIMIT:
                del self._log[: len(self._log) - self.LOG_LIMIT]

    def events(self, limit: int = 60, since: float | None = None) -> list[dict]:
        with self._lock:
            rows = list(self._log)
        if since is not None:
            rows = [r for r in rows if r["t"] >= since]
        return rows[-limit:]

    def diagnostics(self) -> dict:
        """Everything an agent needs to work out what this room is doing."""
        with self._lock:
            state = dict(self.state)
            subscribers = len(self._subscribers)
        return {
            "build_expected": self.BUILD,
            "page": self.page_info or None,
            "page_problem": self.page_problem(),
            "connected_tabs": subscribers,
            "state": state,
            "started": self._started.is_set(),
            "opening_pending": bool(self._opening) and not self._opening_done.is_set(),
            "queued_utterances": self._utterances.qsize(),
            "speaking_id": self._speaking_id,
            "uptime_sec": round(time.monotonic() - self._t0, 1),
        }

    # ── pub/sub ──────────────────────────────────────────────────────────── #

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subscribers.append(q)
            count = len(self._subscribers)
            q.put(dict(self.state))
        self.log("tab.connected", tabs=count)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
            count = len(self._subscribers)
        self.log("tab.disconnected", tabs=count)

    def update(self, **fields: Any) -> None:
        with self._lock:
            self.state.update(fields)
            snapshot = dict(self.state)
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(snapshot)
            except queue.Full:
                pass

    @property
    def connected(self) -> bool:
        with self._lock:
            return bool(self._subscribers)

    # ── speaking ─────────────────────────────────────────────────────────── #

    def speak(self, text: str, *, speed: str | None = None, wait: bool = True,
              timeout: float = 120.0) -> float:
        """Push text to the room to be spoken. Returns seconds spent speaking.

        The text goes out to the browser and is never returned to MCP — this
        one asymmetry is what lets a case prompt reach the candidate's ears
        without reaching their screen.
        """
        utterance = uuid.uuid4().hex
        with self._lock:
            self._speaking_id = utterance
        self._spoken.clear()
        started = time.monotonic()

        # Close the mic before making any sound. Leaving it open means speech
        # recognition transcribes the interviewer's own voice and files it as
        # the candidate's answer — which silently corrupts every grade. The
        # room is half-duplex on purpose: exactly one party has the floor.
        was_listening = bool(self.state.get("listening"))
        if was_listening:
            self.log("mic.close.for_speech")
        self.update(listening=False, timer=None, floor="interviewer")

        if self.mode == "text":
            # No playback, so no acknowledgement to wait for and no race to
            # lose. The line simply appears.
            self.log("said", words=len(text.split()), text=text[:160])
            self._append_dialogue("interviewer", text)
            self.update(speak=None, status="Interviewer")
            return 0.0
        self.log("speak.start", id=utterance[:8], words=len(text.split()),
                 text=text[:120], tabs=len(self._subscribers))
        self.update(
            speak={"text": text, "rate": SPEEDS.get(speed or self.state["speed"], 0.95),
                   "id": utterance},
            status="Speaking…",
        )
        if wait:
            # Cap the wait by how long the line should actually take. A lost ack
            # (backgrounded tab, closed page, browser quirk) must cost a couple
            # of seconds of slack, never a two-minute silent hang.
            words = len(text.split())
            estimate = words / (2.6 * SPEEDS.get(speed or self.state["speed"], 0.95)) + 6
            acked = self._spoken.wait(min(timeout, estimate))
            self.log("speak.end", id=utterance[:8], acked=acked,
                     seconds=round(time.monotonic() - started, 2),
                     estimate=round(estimate, 1))
        self.update(speak=None)
        # A short guard so the tail of the audio (and any room echo) doesn't
        # land in the next utterance.
        time.sleep(0.4)
        return time.monotonic() - started

    # ── the start gate ───────────────────────────────────────────────────── #
    #
    # Nothing is spoken until the candidate presses Start. Two problems die
    # here at once: they choose when the interview begins, and the click is the
    # user gesture browsers demand before they will play any audio at all.

    def await_start_gate(self, *, case_title: str, opening: list[str] | None = None,
                         speed: str = "moderate") -> None:
        self._started.clear()
        self._opening = list(opening or [])
        self._opening_speed = speed
        self._opening_done.clear()
        self.update(awaiting_start=True, case_title=case_title,
                    status="Ready — press Start when you are.", listening=False, timer=None)

    def mark_started(self) -> None:
        """Start pressed. Play the opening immediately, on our own thread.

        The greeting and the case prompt are fixed content — there is no
        judgement in them, so making them wait on a model round-trip only
        creates a window where pressing Start does nothing. Whoever is driving
        picks up afterwards, at the clarifying questions, where judgement
        actually starts.
        """
        if self._started.is_set():
            return
        self.log("start.pressed")
        self._started.set()
        self.update(awaiting_start=False, status="Starting…")
        if self._opening:
            threading.Thread(target=self._play_opening, daemon=True,
                             name="casecraft-opening").start()
        else:
            self._opening_done.set()

    def _play_opening(self) -> None:
        for line in self._opening:
            self.speak(line, speed=self._opening_speed)
            if self.mode != "text":
                time.sleep(0.35)                 # a beat between lines, as a person would
        self.arm(label="clarify", target_sec=150,
                 status="Your turn — ask a clarifying question or lay out your approach.")
        self.log("opening.done", lines=len(self._opening))
        self._opening_done.set()

    def wait_for_opening(self, max_wait: float = 90.0) -> bool:
        # No opening queued means nothing to wait for. Blocking here on a
        # fresh Event stalled every drill's first question for 90 seconds.
        if not self._opening:
            return True
        return self._opening_done.wait(max_wait)

    def wait_for_start(self, max_wait: float = 45.0) -> bool:
        return self._started.wait(max_wait)

    @property
    def started(self) -> bool:
        return self._started.is_set()

    def page_problem(self) -> str | None:
        """Why the room won't behave, if we can tell from what the page reported."""
        if not self.connected:
            return "No interview room is open."
        info = self.page_info
        if not info:
            return ("The open tab predates this server and never identified itself — "
                    "it is running old code. Reload it.")
        if info.get("build") != self.BUILD:
            return (f"The open tab is running build {info.get('build')!r}, expected "
                    f"{self.BUILD!r}. Reload the tab.")
        if self.mode != "text" and not info.get("audio_unlocked"):
            return "The tab has not been clicked yet, so the browser is blocking audio."
        return None

    def _append_dialogue(self, who: str, text: str) -> None:
        with self._lock:
            line = {"who": who, "text": text, "n": len(self.state["dialogue"]) + 1}
            self.state["dialogue"] = self.state["dialogue"] + [line]
        self.update()

    def mark_spoken(self, utterance_id: str | None = None) -> None:
        """Acknowledge that speech finished.

        Scoped to an utterance id: a late ack for the *previous* line must not
        satisfy the wait on the current one, or a fast tool sequence silently
        cuts the candidate's audio off mid-sentence.
        """
        with self._lock:
            current = self._speaking_id
        if utterance_id and current and utterance_id != current:
            self.log("speak.ack.stale", got=(utterance_id or "")[:8],
                     current=(current or "")[:8])
            return
        self.log("speak.ack", id=(utterance_id or "")[:8])
        self._spoken.set()

    # ── listening ────────────────────────────────────────────────────────── #
    #
    # The candidate talks whenever they want; utterances land in a queue and
    # Claude pops them when it's ready. The old design only opened the mic when
    # Claude called a tool, which meant the candidate literally could not answer
    # "can you hear me?" — there was nothing listening. Conversation needs the
    # microphone to be the candidate's, not the interviewer's.

    def arm(self, *, label: str = "answer", target_sec: int = 120,
            status: str = "Listening — speak whenever you're ready.") -> None:
        """Open the mic. Idempotent — re-arming an open mic must change nothing.

        `listen` re-arms on every call, and callers are told to call it again
        while the candidate is still thinking. Resetting here would restart
        their timer and wipe the transcript they are part-way through speaking.
        """
        if self.state.get("listening"):
            if self.state.get("status") != status:
                self.update(status=status)
            return

        self.log("mic.open", label=label, target_sec=target_sec)
        self._listen_started = time.monotonic()
        self.update(
            floor="candidate",
            listening=True,
            transcript="",
            feedback=None,
            status=status,
            timer={"label": label, "target": target_sec, "started": time.time()},
        )

    def disarm(self, status: str = "Thinking…") -> None:
        self.update(listening=False, status=status, timer=None)

    def submit_transcript(self, text: str) -> None:
        """The candidate finished an utterance — queue it, never drop it.

        Queued rather than assigned to a slot, so speaking before Claude asks
        (or twice in a row) can't lose what was said.
        """
        text = (text or "").strip()
        if not text:
            # A stray Done press or a silence auto-submit that captured nothing.
            # This must be a pure no-op: re-arming here pushes new state, the
            # page reacts by recording again, and submits empty again — a loop
            # that locked the candidate out of answering at all.
            self.log("heard.empty.ignored")
            return
        self.log("heard", chars=len(text), text=text[:160])
        self._append_dialogue("candidate", text)
        self._utterances.put({"text": text, "at": time.time(),
                              "seconds": max(0.1, time.monotonic() - self._listen_started)})
        # Say what is actually true. "Thinking…" while nobody is reading the
        # queue is a lie, and it reads as a hang rather than as a handover.
        self.update(listening=False, transcript=text, timer=None, floor="waiting",
                    status="Answer captured — waiting for the interviewer.")

    def request_skip(self) -> None:
        """The candidate pressed Skip. Unblocks whoever is waiting on an answer."""
        self.log("skip.requested")
        self._skip.set()
        self._utterances.put({"text": "", "skip": True, "at": time.time(), "seconds": 0.0})
        self.update(listening=False, timer=None, floor="waiting",
                    status="Skipped — moving on.")

    def take_skip(self) -> bool:
        was = self._skip.is_set()
        self._skip.clear()
        return was

    def note_picked_up(self) -> None:
        self.update(status="Interviewer is considering your answer…")

    def next_utterance(self, max_wait: float = 50.0) -> dict | None:
        """Pop the next thing the candidate said, or None if they're still going.

        Bounded so a long silence never looks like a hung tool call — the caller
        simply asks again.
        """
        try:
            return self._utterances.get(timeout=max_wait)
        except queue.Empty:
            return None

    def drain(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self._utterances.get_nowait())
            except queue.Empty:
                return out


class Session:
    """One interview: a full case, or a drill run of loose questions."""

    def __init__(self, room: Room, library: Library) -> None:
        self.room = room
        self.library = library
        self.id = uuid.uuid4().hex[:8]
        self.case: Case | None = None
        self.queue: list[Question] = []
        self.index = -1
        self.mode = "drill"
        self.phase = "idle"
        self.speed = "moderate"
        self.attempts: list[Attempt] = []
        self.scorecard = Scorecard()
        self.probes_used = 0
        self.started = time.time()
        self.clarifications_asked: list[str] = []

    # ── setup ────────────────────────────────────────────────────────────── #

    def load_case(self, case: Case) -> None:
        self.mode = "case"
        self.case = case
        self.queue = list(case.questions)
        self.index = -1
        self.phase = "prompt"

    def load_drill(self, questions: list[Question]) -> None:
        self.mode = "drill"
        self.case = None
        self.queue = questions
        self.index = -1
        self.phase = "analysis"

    # ── flow ─────────────────────────────────────────────────────────────── #

    @property
    def current(self) -> Question | None:
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    def jump_to(self, question_id: str) -> Question | None:
        """Candidate-led steering: go to the question they chose, not the next.

        Marks everything before it as passed over rather than pretending the
        case is linear — the queue order stays intact for anything they come
        back to via another jump.
        """
        for i, q in enumerate(self.queue):
            if q.id == question_id or q.uid.endswith("/" + question_id):
                self.index = i
                self.probes_used = 0
                self.phase = {"structure": "structure", "synthesis": "synthesis"}.get(
                    q.type, "analysis")
                self.room.update(
                    progress=f"{i + 1} of {len(self.queue)}", exhibit=None, feedback=None)
                return q
        return None

    def advance(self) -> Question | None:
        self.index += 1
        self.probes_used = 0
        q = self.current
        if q:
            self.phase = {"structure": "structure", "synthesis": "synthesis"}.get(
                q.type, "analysis"
            )
            self.room.update(
                progress=f"{self.index + 1} of {len(self.queue)}",
                exhibit=None,
                feedback=None,
            )
        else:
            self.phase = "debrief"
        return q

    def ask_current(self) -> float:
        """Speak the current question aloud. Returns speaking duration."""
        q = self.current
        if not q:
            return 0.0
        text = f"{q.context} {q.prompt}".strip() if (q.context and self.mode == "drill") else q.prompt
        return self.room.speak(text, speed=self.speed)

    # ── grading ──────────────────────────────────────────────────────────── #

    def score_answer(
        self, transcript: str, seconds: float, covered: set[str] | None = None
    ) -> dict:
        """Grade one answer on both axes: what was said, and how it was said."""
        q = self.current
        if not q:
            raise RuntimeError("no active question")

        verdict = grade(q.rubric, transcript=transcript, covered=covered)

        delivery = analyze(
            transcript,
            seconds,
            target_seconds=q.time_target_sec,
            expects_number=q.type in ("math", "exhibit"),
            expects_recommendation=q.type == "synthesis",
            typed=self.room.mode == "text",
        )

        # Content lands on the question's primary dimension; delivery always
        # lands on communication. That mirrors a real form, where a candidate
        # can nail the analysis and still be dinged for how it came out.
        self.scorecard.add(q.dimension, verdict.score)
        self.scorecard.add("communication", delivery.score)
        if q.type == "structure":
            self.scorecard.add("judgment", verdict.score)

        attempt = Attempt(
            uid=q.uid, question_type=q.type, dimension=q.dimension, tags=q.tags,
            difficulty=q.difficulty, transcript=transcript, seconds=seconds,
            verdict=verdict, delivery=delivery.as_dict(), probes_used=self.probes_used,
        )
        self.attempts.append(attempt)

        payload = {
            "outcome": verdict.outcome,
            "score": verdict.score,
            "note": verdict.note,
            "error_id": verdict.error_id,
            "missed": verdict.missed,
            "delivery": delivery.as_dict(),
            "question_type": q.type,
            "probe_available": self.probes_used < len(q.probes),
        }
        self.room.update(feedback=payload, status="Feedback ready.")
        return payload

    def next_probe(self) -> str | None:
        """The next nudge, weakest first — the way an interviewer escalates."""
        q = self.current
        if not q or self.probes_used >= len(q.probes):
            return None
        probe = q.probes[self.probes_used]
        self.probes_used += 1
        return probe

    def debrief(self) -> dict:
        card = self.scorecard.as_dict()
        card["duration_minutes"] = round((time.time() - self.started) / 60, 1)
        # Unique questions, not attempts: a probe-and-retry on one question was
        # reported as "6 questions answered" in a five-question case.
        card["questions_answered"] = len({a.uid for a in self.attempts})
        card["attempts"] = len(self.attempts)
        card["clarifications_asked"] = len(self.clarifications_asked)

        # Recurring delivery notes are worth more than one-off ones — a habit
        # flagged three times is the thing actually worth practising.
        from .delivery import categorise

        tally: dict[str, int] = {}
        for a in self.attempts:
            seen_here = set()
            for note in a.delivery.get("notes", []):
                key = categorise(note)          # None for praise
                if key is None or key in seen_here:
                    continue                    # count a habit once per answer
                seen_here.add(key)
                tally[key] = tally.get(key, 0) + 1
        card["recurring_habits"] = [
            {"note": k, "times": v} for k, v in sorted(tally.items(), key=lambda kv: -kv[1])
            if v >= 2
        ][:4]

        card["per_question"] = [
            {
                "uid": a.uid, "type": a.question_type, "score": a.verdict.score if a.verdict else 0,
                "outcome": a.verdict.outcome if a.verdict else "unanswered",
                "seconds": round(a.seconds), "probes": a.probes_used,
                "error": a.verdict.error_id if a.verdict else None,
            }
            for a in self.attempts
        ]
        self.room.update(scorecard=card, phase="debrief", status="Case complete.")
        return card
