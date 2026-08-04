"""A simulated candidate that drives the real room over real HTTP.

This exists so the conversation loop is never hand-tested again. It speaks the
same protocol a browser does — subscribes to SSE, acknowledges speech, presses
"Done" — so a test that passes here exercises the code paths a real session
uses, not a mock of them.

It can also misbehave on purpose, because the failures that actually bit us
were misbehaviour, not logic errors: a tab that never acknowledges speech (the
120-second hang), a candidate who says nothing, and someone talking before
anyone asked them to.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager

import httpx


class SimulatedCandidate(threading.Thread):
    """Stands in for a person with a browser tab open.

    Parameters mirror the ways a real session goes wrong:
      `deaf`  — never acknowledges speech (backgrounded tab, closed page)
      `mute`  — never answers (candidate frozen, mic broken)
      `wont_start` — never presses Start (walked away before beginning)
      `think` — seconds spent "thinking" before each answer
    """

    daemon = True

    def __init__(self, base_url: str, answers: list[str] | None = None, *,
                 deaf: bool = False, mute: bool = False, announce: bool = True,
                 wont_start: bool = False,
                 speak_time: float = 0.02, think: float = 0.05) -> None:
        super().__init__(name="simulated-candidate")
        self.base = base_url.rstrip("/")
        self.answers = list(answers or [])
        self.deaf, self.mute, self.announce = deaf, mute, announce
        self.wont_start = wont_start
        self.started_at: float | None = None
        self.speak_time, self.think = speak_time, think

        self.heard: list[str] = []            # everything spoken to them
        self.said: list[str] = []             # everything they said
        self.exhibits: list[str] = []
        self.feedback: list[dict] = []
        self.statuses: list[str] = []

        self._stop = threading.Event()
        self._ready = threading.Event()
        self._last_speak: str | None = None
        self._armed_at: str | None = None
        self._awaiting_pickup = False
        self._last_status = ""
        self._dialogue_seen = 0
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────── #

    def run(self) -> None:
        with httpx.Client(timeout=30) as client:
            # A real page identifies its build on connect; `announce=False`
            # impersonates a tab left open across a code change.
            if self.announce:
                from casecraft.session import Room
                try:
                    client.post(f"{self.base}/hello",
                                json={"build": Room.BUILD, "audio_unlocked": True})
                except Exception:                          # noqa: BLE001
                    pass
            with client.stream("GET", f"{self.base}/events") as response:
                self._ready.set()
                for line in response.iter_lines():
                    if self._stop.is_set():
                        return
                    if line.startswith("data: "):
                        self._react(json.loads(line[6:]), client)

    def wait_until_connected(self, timeout: float = 10) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        self._stop.set()

    # ── behaviour ────────────────────────────────────────────────────────── #

    def _react(self, state: dict, client: httpx.Client) -> None:
        if state.get("status"):
            self.statuses.append(state["status"])

        # A real candidate presses Start when the gate appears.
        if state.get("awaiting_start") and not self.wont_start:
            self.started_at = time.time()
            client.post(f"{self.base}/session/start", json={})

        # Text mode delivers the interviewer's lines as dialogue; voice mode as
        # a speak event. Either way the candidate "heard" them.
        for line in (state.get("dialogue") or [])[self._dialogue_seen:]:
            if line["who"] == "interviewer":
                self.heard.append(line["text"])
        self._dialogue_seen = len(state.get("dialogue") or [])

        speak = state.get("speak")
        if speak and speak["id"] != self._last_speak:
            self._last_speak = speak["id"]
            self.heard.append(speak["text"])
            if not self.deaf:
                time.sleep(self.speak_time)
                client.post(f"{self.base}/spoken", json={"id": speak["id"]})

        if state.get("exhibit", {}) and state["exhibit"].get("title"):
            title = state["exhibit"]["title"]
            if not self.exhibits or self.exhibits[-1] != title:
                self.exhibits.append(title)

        if state.get("feedback"):
            self.feedback.append(state["feedback"])

        # The interviewer took our last answer. `listen` and `collect_answer`
        # both mark the pickup ("…considering your answer…") before they
        # return, so that status is the one reliable "your answer landed"
        # signal the page ever sees. It must be the TRANSITION into that
        # status, though: unrelated updates re-broadcast the whole state, so a
        # stale "considering" (or a lingering feedback payload) re-reads as a
        # fresh pickup and un-gates the next answer one turn early.
        status = state.get("status") or ""
        if (self._awaiting_pickup and status != self._last_status
                and "considering" in status.lower()):
            self._awaiting_pickup = False
        self._last_status = status

        # The mic opened — answer, unless we're playing a candidate who won't.
        #
        # One answer per arm token, and never while the previous answer sits
        # unclaimed. The server mints a fresh `timer.started` on every
        # defensive re-arm (`say`, `listen` and `collect_answer` all re-arm),
        # not only on real questions — so on a starved runner the SSE consumer
        # catches up on several tokens in a burst, two scripted answers land
        # in the utterance queue together, and the next drain() merges them
        # into one transcript. That eats the script early and starves the last
        # question. A real candidate doesn't speak again until the interviewer
        # has taken what they said; neither does this one.
        timer = state.get("timer") or {}
        token = f"{timer.get('started')}"
        if state.get("listening") and self._armed_at != token and not self._awaiting_pickup:
            self._armed_at = token
            if self.mute:
                return
            with self._lock:
                if not self.answers:
                    return
                reply = self.answers.pop(0)
            time.sleep(self.think)
            client.post(f"{self.base}/answer", json={"transcript": reply})
            self.said.append(reply)
            self._awaiting_pickup = True

    # ── candidate-initiated actions ──────────────────────────────────────── #

    def speak_unprompted(self, text: str) -> None:
        """Talk without waiting to be asked — the thing a queue must not lose."""
        httpx.post(f"{self.base}/listen/start", json={}, timeout=10)
        httpx.post(f"{self.base}/answer", json={"transcript": text}, timeout=10)
        self.said.append(text)

    def queue_answers(self, *answers: str) -> None:
        with self._lock:
            self.answers.extend(answers)

    def wait_for_heard(self, count: int, timeout: float = 5.0) -> bool:
        """Wait until `count` interviewer lines have arrived.

        Text mode completes a whole opening in microseconds, so asserting
        straight after it races the SSE consumer. Voice mode's playback delays
        hid this; the wait makes the tests honest about it either way.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.heard) >= count:
                return True
            time.sleep(0.02)
        return False

    def heard_containing(self, needle: str) -> bool:
        return any(needle.lower() in h.lower() for h in self.heard)


@contextmanager
def no_stall(seconds: float, what: str = "operation"):
    """Fail if the block takes longer than `seconds`.

    Every hang this project has had looked identical from the outside: a tool
    call that never returned. Wrapping the loop in a hard bound turns that from
    a live-session surprise into a red test.
    """
    started = time.monotonic()
    yield
    elapsed = time.monotonic() - started
    assert elapsed <= seconds, f"{what} stalled: took {elapsed:.1f}s, limit {seconds}s"


CANDIDATE_SCRIPT = {
    "greeting": "Yes, I can hear you fine. I'm ready when you are.",
    "clarify": "Before I start — where does the client operate, and what's their goal?",
    "structure": (
        "I want to look at three things. First, revenue, which is passengers flown "
        "times ticket price. Second, fixed costs like aircraft leases and gate fees. "
        "Third, variable costs, mainly fuel and per-passenger service."
    ),
    "math_right": (
        "Forty routes times six flights is 240 flights a day. 150 seats at 80 percent "
        "is 120 passengers, so 28,800 passengers a day. Times 220 dollars is about "
        "6.34 million a day, so roughly 2.3 billion a year."
    ),
    "math_wrong": "I make it about 2.89 billion a year.",
    "exhibit": (
        "First, fuel is the biggest mover, up from 3.1 to 5.9. Second, labor rose from "
        "4.2 to 5.4. Third, the lease line is flat at 2.8. In total CASM went from 12 "
        "to 16.2, about 35 percent, which means this is a cost problem, not a revenue one."
    ),
    "synthesis": (
        "My recommendation is to attack costs, not price. Revenue is 2.3 billion and "
        "growing, but cost per seat mile is up 35 percent, driven by fuel up 90 percent. "
        "Hedge fuel, rationalize the worst routes, and grow ancillary revenue. The risk "
        "is ceding share on routes we exit, so I'd test exits selectively."
    ),
}
