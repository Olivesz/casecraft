"""Full-path integration: MCP tools ↔ room HTTP ↔ a fake browser.

This exercises the thing that actually ships — the SSE bridge, the blocking
answer wait, and the two-step grading handoff — rather than calling the session
objects directly. If this passes, a real browser and a real Claude will work.
"""

from __future__ import annotations

import json
import os
import threading
import time

import httpx
import pytest

os.environ.setdefault("CASECRAFT_NO_BROWSER", "1")
os.environ.setdefault("CASECRAFT_DB", "/tmp/casecraft-integration.db")

from fastmcp import Client                    # noqa: E402

from casecraft import server as srv           # noqa: E402


class FakeBrowser(threading.Thread):
    """Stands in for the interview room tab: acks speech, answers when asked."""

    daemon = True

    def __init__(self, base_url: str, answers: list[str]) -> None:
        super().__init__(name="fake-browser")
        self.base = base_url
        self.answers = list(answers)
        self.spoken: list[str] = []
        self.exhibits: list[str] = []
        self.feedback: list[dict] = []
        self._stop = threading.Event()
        self._last_speak_id: str | None = None
        self._answered_for: str | None = None
        self._dialogue_seen = 0

    def run(self) -> None:
        with httpx.Client(timeout=None) as client:
            from casecraft.session import Room
            try:
                client.post(f"{self.base}/hello",
                            json={"build": Room.BUILD, "audio_unlocked": True})
            except Exception:                              # noqa: BLE001
                pass
            with client.stream("GET", f"{self.base}/events") as resp:
                for line in resp.iter_lines():
                    if self._stop.is_set():
                        return
                    if not line.startswith("data: "):
                        continue
                    state = json.loads(line[6:])
                    self._react(state, client)

    def _react(self, state: dict, client: httpx.Client) -> None:
        # Text mode delivers interviewer lines as dialogue rather than speech.
        for line in (state.get("dialogue") or [])[self._dialogue_seen:]:
            if line["who"] == "interviewer":
                self.spoken.append(line["text"])
        self._dialogue_seen = len(state.get("dialogue") or [])

        if state.get("awaiting_start"):
            client.post(f"{self.base}/session/start", json={})

        speak = state.get("speak")
        if speak and speak.get("id") != self._last_speak_id:
            self._last_speak_id = speak["id"]
            self.spoken.append(speak["text"])
            time.sleep(0.01)                       # pretend to talk
            client.post(f"{self.base}/spoken", json={"id": speak["id"]})

        if state.get("exhibit"):
            title = state["exhibit"].get("title")
            if title and (not self.exhibits or self.exhibits[-1] != title):
                self.exhibits.append(title)

        if state.get("feedback"):
            self.feedback.append(state["feedback"])

        timer = state.get("timer") or {}
        key = f"{timer.get('started')}"
        if state.get("listening") and self.answers and self._answered_for != key:
            self._answered_for = key
            time.sleep(0.05)                       # pretend to think
            client.post(f"{self.base}/answer", json={"transcript": self.answers.pop(0)})

    def stop(self) -> None:
        self._stop.set()


ANSWERS = [
    # q1 structure — deliberately misses variable costs, to exercise the probe path
    "I want to look at two things. First, revenue, which is passengers times ticket "
    "price. Second, fixed costs like the aircraft leases and the gate fees.",
    # q2 math — the load-factor mistake
    "Forty routes times six is 240 flights, times 150 seats, times 220 dollars, "
    "times 365. I make it about 2.89 billion.",
]


@pytest.fixture(scope="module")
def room_and_client():
    url = srv._server.start()
    browser = FakeBrowser(url, ANSWERS)
    browser.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not srv._room.connected:
        time.sleep(0.05)
    assert srv._room.connected, "fake browser never connected to the room"
    yield url, browser
    browser.stop()


@pytest.mark.anyio
async def test_full_case_flow(room_and_client):
    url, browser = room_and_client

    async with Client(srv.mcp) as c:
        # ── start ────────────────────────────────────────────────────────
        started = (await c.call_tool("start_case",
                                     {"case_id": "casecraft-orchid-airlines"})).data
        assert started["room_connected"] is True
        assert started["format"] == "interviewer_led"
        assert started["question_count"] == 5

        # The briefing tells Claude the topics, never the answers.
        topics = json.dumps(started["clarification_topics"])
        assert "40 routes" not in topics
        assert "continental US" not in topics

        # ── prompt is spoken, never returned ─────────────────────────────
        # The opening (greeting + case prompt) plays itself when the candidate
        # presses Start; this call just waits for it to finish.
        res = (await c.call_tool("ask_case_prompt", {})).data
        assert res["played"] is True
        assert "Orchid" not in json.dumps(res), "the prompt must never come back through MCP"
        assert any("Orchid Airlines" in s for s in browser.spoken), \
            "case prompt never reached the room"

        # ── clarification: gated, and spoken not returned ────────────────
        clar = (await c.call_tool("answer_clarification",
                                  {"question": "Where do they operate?"})).data
        assert clar["matched"] is True
        assert "continental US" not in json.dumps(clar)
        assert any("continental US" in s for s in browser.spoken)

        miss = (await c.call_tool("answer_clarification",
                                  {"question": "What is the CEO's favourite colour?"})).data
        assert miss["matched"] is False
        assert "available_topics" in miss

        # ── q1: structure, incomplete answer → PARTIAL → probe ───────────
        q1 = (await c.call_tool("next_question", {})).data
        assert q1["type"] == "structure"

        ans = (await c.call_tool("collect_answer", {"max_wait": 15})).data
        assert ans["ready"] is True
        assert ans["graded"] is False              # needs the model to match
        assert "revenue" in {c0["id"] for c0 in ans["rubric"]["components"]}
        # rubric labels only — no weights leak
        assert all(set(c0) == {"id", "label"} for c0 in ans["rubric"]["components"])

        graded = (await c.call_tool("score",
                                    {"covered": ["revenue", "fixed_costs"]})).data
        assert graded["outcome"] == "partial"
        assert "variable_costs" in graded["missed"]

        p = (await c.call_tool("probe", {})).data
        assert "probe" in p

        # ── q2: math, wrong answer → named diagnosis, instantly ──────────
        q2 = (await c.call_tool("next_question", {})).data
        assert q2["type"] == "math"

        m = (await c.call_tool("collect_answer", {"max_wait": 15})).data
        assert m["graded"] is True                 # numeric never waits on a model
        assert m["outcome"] == "wrong"
        assert "load factor" in m["note"]
        assert m["error_id"]

        # ── the room saw the feedback ────────────────────────────────────
        assert browser.feedback, "feedback never reached the room"

        # ── scorecard ────────────────────────────────────────────────────
        card = (await c.call_tool("finish", {})).data
        assert set(card["dimensions"]) >= {"structure", "analytics", "communication"}
        assert card["limiting_factor"] in card["dimensions"]
        assert card["questions_answered"] == 2


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── optional on-device speech-to-text ───────────────────────────────────── #

def test_config_reports_engine(room_and_client):
    url, _ = room_and_client
    cfg = httpx.get(f"{url}/config", timeout=10).json()
    assert "local_stt" in cfg
    if cfg["local_stt"]:
        assert cfg["model"]


def test_local_transcription_over_http(room_and_client):
    """Real audio → real Whisper → text the grader can score."""
    from casecraft import stt

    if not stt.available():
        pytest.skip("faster-whisper not installed")

    import subprocess
    import tempfile

    url, _ = room_and_client
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as fh:
        path = fh.name
    subprocess.run(
        ["say", "-o", path,
         "Two hundred forty flights per day at one hundred twenty passengers each, "
         "so roughly two point three billion dollars a year."],
        check=True,
    )
    audio = open(path, "rb").read()
    os.unlink(path)

    r = httpx.post(f"{url}/transcribe", content=audio,
                   headers={"Content-Type": "audio/aiff"}, timeout=180)
    assert r.status_code == 200, r.text
    text = r.json()["transcript"]
    assert text

    from casecraft.scoring import extract_numbers
    numbers = extract_numbers(text)
    assert any(abs(n - 2.3e9) < 1e8 for n in numbers), f"got {numbers} from {text!r}"


def test_transcribe_rejects_empty_audio(room_and_client):
    url, _ = room_and_client
    r = httpx.post(f"{url}/transcribe", content=b"", timeout=30)
    assert r.status_code in (400, 503)
