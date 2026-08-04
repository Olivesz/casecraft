"""Autonomous playthrough — drive the real system and hunt for breakage.

Not a test suite. This runs a whole interview through the real MCP tools and a
real room, then attacks it: out-of-turn speech, double submits, skips mid-answer,
absurd inputs, rapid-fire calls. It reports findings rather than asserting, so
one run surfaces everything instead of stopping at the first failure.

    .venv/bin/python -m tests.playthrough
"""

from __future__ import annotations

import asyncio
import json
import os
import time

os.environ.setdefault("CASECRAFT_NO_BROWSER", "1")
os.environ.setdefault("CASECRAFT_DB", "/tmp/casecraft-playthrough.db")

import httpx                                          # noqa: E402
from fastmcp import Client                            # noqa: E402

from casecraft import server as srv                   # noqa: E402
from tests.harness import SimulatedCandidate          # noqa: E402

FINDINGS: list[tuple[str, str]] = []
CHECKS = {"pass": 0, "fail": 0}


def check(ok: bool, what: str, detail: str = "") -> bool:
    if ok:
        CHECKS["pass"] += 1
    else:
        CHECKS["fail"] += 1
        FINDINGS.append((what, detail))
        print(f"   ✗ {what}\n       {detail}")
    return ok


class Bot(SimulatedCandidate):
    """A candidate that says nothing unless told to — we inject on purpose."""

    def _react(self, state, client):
        if state.get("status"):
            self.statuses.append(state["status"])
        # Text mode delivers the interviewer's lines as dialogue.
        for line in (state.get("dialogue") or [])[self._dialogue_seen:]:
            if line["who"] == "interviewer":
                self.heard.append(line["text"])
        self._dialogue_seen = len(state.get("dialogue") or [])
        speak = state.get("speak")
        if speak and speak["id"] != self._last_speak:
            self._last_speak = speak["id"]
            self.heard.append(speak["text"])
            client.post(f"{self.base}/spoken", json={"id": speak["id"]})
        if state.get("awaiting_start"):
            client.post(f"{self.base}/session/start", json={})
        if state.get("feedback"):
            self.feedback.append(state["feedback"])
        if state.get("exhibit") and state["exhibit"].get("title"):
            t = state["exhibit"]["title"]
            if not self.exhibits or self.exhibits[-1] != t:
                self.exhibits.append(t)


def say(url: str, text: str) -> None:
    httpx.post(f"{url}/debug/act", json={"action": "say", "text": text}, timeout=15)


def debug(url: str) -> dict:
    return httpx.get(f"{url}/debug?limit=200", timeout=15).json()


async def run() -> None:
    url = srv._server.start()
    bot = Bot(url, [])
    bot.start()
    bot.wait_until_connected()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not srv._room.connected:
        time.sleep(0.02)

    async with Client(srv.mcp) as c:
        async def call(tool, **kw):
            t0 = time.monotonic()
            result = (await c.call_tool(tool, kw)).data
            elapsed = time.monotonic() - t0
            check(elapsed < 60, f"{tool} returned in reasonable time",
                  f"took {elapsed:.1f}s")
            return result

        print("\n── 1. start and opening ──────────────────────────────")
        started = await call("start_case", case_id="casecraft-orchid-airlines")
        blob = json.dumps(started)
        check("Orchid Airlines, a regional carrier" not in blob,
              "case prompt never returned through MCP", blob[:200])
        check(started.get("awaiting_start") is True, "start gate armed")

        opened = await call("ask_case_prompt")
        check(opened.get("played") is True, "opening played", str(opened))
        bot.wait_for_heard(1)
        # The greeting and the case are ONE turn now — a real interviewer does
        # not pause after hello to see whether you'd like to interject.
        check(len(bot.heard) == 1, "the opening is a single turn",
              f"{len(bot.heard)} turns: {[h[:40] for h in bot.heard]}")
        brief = bot.heard[0] if bot.heard else ""
        check("Good to meet you" in brief, "greeting delivered", brief[:80])
        check("Orchid Airlines" in brief, "case prompt in the same turn", brief[:80])
        check("tell me how you'd structure" not in brief,
              "no redundant second ask — the prompt already asks", brief[-90:])
        d = debug(url)
        check(d["state"]["listening"] is True, "mic open after the opening",
              str(d["state"]))
        check(d["state"]["floor"] == "candidate", "floor handed to candidate",
              d["state"]["floor"])

        print("\n── 2. clarifications ─────────────────────────────────")
        say(url, "Where do they operate?")
        heard = await call("listen", max_wait=10)
        check(heard.get("heard") is True, "clarifying question received", str(heard))
        clar = await call("answer_clarification", question="Where do they operate?")
        check(clar.get("matched") is True, "clarification matched")
        check("continental US" not in json.dumps(clar),
              "clarification answer not returned through MCP", json.dumps(clar)[:160])
        check(clar.get("mic_open") is True, "mic reopened after clarification",
              str(clar))

        miss = await call("answer_clarification", question="What is their CEO's name?")
        check(miss.get("matched") is False, "unknown clarification refused")

        print("\n── 3. structure, probe, regrade ──────────────────────")
        q1 = await call("next_question")
        check(q1["type"] == "structure", "q1 is the structure question")
        d = debug(url)
        check(d["state"]["listening"] is True, "mic open after the question")

        say(url, "I'd look at revenue, which is passengers times price, and then "
                 "costs split into fixed like leases and variable like fuel.")
        a1 = await call("collect_answer", max_wait=15,
                        acknowledge="Okay, let me think about that.")
        check(a1.get("ready") is True, "structure answer collected")
        check(a1.get("graded") is False, "structure needs model matching")
        s1 = await call("score", covered=["revenue", "fixed_costs", "variable_costs"])
        check(s1["outcome"] == "correct", "complete structure scores correct", str(s1))

        probe = await call("probe")
        check("probe" in probe, "probe available after grading")

        print("\n── 4. math: data request, right, wrong, diagnosed ────")
        q2 = await call("next_question")
        check(q2["type"] == "math", "q2 is math")

        # A real candidate asks for inputs before computing. That must be a
        # conversation, never a graded (and logged) wrong answer.
        say(url, "Sorry — what was the load factor again?")
        req = await call("collect_answer", max_wait=10)
        check(req.get("clarification_request") is True,
              "a data request is treated as a request, not an answer", str(req)[:140])
        check(req.get("graded") is not True, "data request is not graded")
        d = debug(url)
        check(d["state"]["listening"] is True, "their turn stays open after asking")

        say(url, "I make it about 2.89 billion a year.")
        m = await call("collect_answer", max_wait=15)
        check(m.get("graded") is True, "math graded without a model call")
        check(m.get("outcome") == "wrong", "wrong answer marked wrong")
        check("load factor" in (m.get("note") or ""),
              "wrong math is DIAGNOSED not just marked", str(m.get("note")))
        check(m.get("error_id"), "error_id recorded for weakness tracking")

        print("\n── 5. skip ───────────────────────────────────────────")
        q3 = await call("next_question")
        httpx.post(f"{url}/session/skip", json={}, timeout=10)
        sk = await call("collect_answer", max_wait=10)
        check(sk.get("skipped") is True, "skip surfaces as skipped", str(sk))
        check(sk.get("graded") is False, "skip is not graded")

        print("\n── 6. exhibit ordering ───────────────────────────────")
        before = len(bot.heard)
        q4 = await call("next_question")
        check(q4["type"] == "exhibit", "q4 is the exhibit question")
        bot.wait_for_heard(before + 2)
        spoken = bot.heard[before:]
        check(len(spoken) >= 2, "exhibit intro spoken before the question",
              f"spoken={spoken}")
        if len(spoken) >= 2:
            check("showing you" in spoken[0].lower() or "chart" in spoken[0].lower(),
                  "exhibit intro comes FIRST", f"first line was {spoken[0][:60]!r}")
        check(bot.exhibits, "exhibit displayed in the room", str(bot.exhibits))
        d = debug(url)
        check(d["state"]["listening"] is True,
              "mic open after the exhibit sequence", str(d["state"]))

        say(url, "Fuel is the biggest mover, up from 3.1 to 5.9. Labor rose too. "
                 "Leases are flat, so total CASM is up about 35 percent, which "
                 "means this is a cost problem.")
        a4 = await call("collect_answer", max_wait=15)
        check(a4.get("ready") is True, "exhibit answer collected")
        await call("score", covered=["fuel_spike", "labor_rise", "lease_flat"])

        print("\n── 7. adversarial ────────────────────────────────────")
        # empty submission
        say(url, "   ")
        d = debug(url)
        check(d["queued_utterances"] == 0, "empty submission not queued",
              f"queued={d['queued_utterances']}")

        # talking out of turn (before being asked)
        say(url, "unprompted thought one")
        say(url, "unprompted thought two")
        d = debug(url)
        check(d["queued_utterances"] == 2, "out-of-turn speech is queued, not lost",
              f"queued={d['queued_utterances']}")

        q5 = await call("next_question")
        popped = await call("collect_answer", max_wait=10)
        joined = popped.get("transcript") or ""
        check("thought one" in joined and "thought two" in joined,
              "a multi-message answer arrives as ONE answer", joined[:80])

        # absurd input
        say(url, "x" * 20000)
        big = await call("collect_answer", max_wait=10)
        check(big.get("ready") is True, "very long answer does not crash")

        say(url, "café ☕ naïve — 数字 2.3 billion 🎯")
        uni = await call("collect_answer", max_wait=10)
        check(uni.get("ready") is True, "unicode does not crash", str(uni)[:120])

        print("\n── 8. scorecard ──────────────────────────────────────")
        card = await call("finish")
        check(set(card["dimensions"]) >= {"structure", "analytics"},
              "scorecard covers the dimensions attempted")
        check(card["limiting_factor"] in card["dimensions"],
              "limiting factor is a real dimension")
        check(card["questions_answered"] > 0, "attempts counted")

        print("\n── 9. room invariants over the whole run ─────────────")
        events = debug(url)["events"]
        opens = [e for e in events if e["event"] == "mic.open"]
        speaks = [e for e in events if e["event"] == "speak.start"]
        closes = [e for e in events if e["event"] == "mic.close.for_speech"]
        print(f"   spoke {len(speaks)}× · mic opened {len(opens)}× · "
              f"closed-for-speech {len(closes)}×")
        unacked = [e for e in events
                   if e["event"] == "speak.end" and not e.get("acked")]
        check(not unacked, "every spoken line was acknowledged",
              f"{len(unacked)} unacked")

        # No speak.start may occur while the mic is open.
        listening = False
        overlaps = 0
        for e in events:
            if e["event"] == "mic.open":
                listening = True
            elif e["event"] in ("mic.close.for_speech", "heard", "skip.requested"):
                listening = False
            elif e["event"] == "speak.start" and listening:
                overlaps += 1
        check(overlaps == 0, "never spoke while the mic was open",
              f"{overlaps} overlaps")

    bot.stop()
    srv._server.stop()


def main() -> int:
    asyncio.run(run())
    print("\n" + "═" * 62)
    print(f"  {CHECKS['pass']} passed · {CHECKS['fail']} failed")
    if FINDINGS:
        print("\n  FINDINGS")
        for what, detail in FINDINGS:
            print(f"   • {what}")
            if detail:
                print(f"     {detail[:150]}")
    else:
        print("  no findings")
    print("═" * 62)
    return 1 if FINDINGS else 0


if __name__ == "__main__":
    raise SystemExit(main())
