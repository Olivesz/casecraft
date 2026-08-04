"""Three full flows via real MCP tools; print every dialogue line for reading.

    .venv/bin/python -m tests.dogfood
"""
from __future__ import annotations

import asyncio
import os
import textwrap
import time

os.environ.setdefault("CASECRAFT_NO_BROWSER", "1")
os.environ.setdefault("CASECRAFT_DB", "/tmp/casecraft-dogfood.db")

import httpx
from fastmcp import Client

from casecraft import server as srv
from tests.playthrough import Bot, check, CHECKS, FINDINGS, say


def dump_dialogue(title: str) -> None:
    print(f"\n════ {title} — the conversation as the candidate saw it ════")
    for line in srv._room.state["dialogue"]:
        who = "INT" if line["who"] == "interviewer" else "YOU"
        print(textwrap.fill(line["text"], 96, initial_indent=f"  [{who}] ",
                            subsequent_indent="        "))


async def main() -> int:
    url = srv._server.start()
    bot = Bot(url, [])
    bot.start(); bot.wait_until_connected()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5 and not srv._room.connected:
        time.sleep(0.02)

    async with Client(srv.mcp) as c:
        # ── flow 1: Meridian, candidate-led, with a jump ─────────────────
        await c.call_tool("start_case", {"case_id": "casecraft-meridian-coffee"})
        await c.call_tool("ask_case_prompt", {})
        say(url, "I'd actually like to start with the store economics before "
                 "sizing anything.")
        heard = (await c.call_tool("listen", {"max_wait": 10})).data
        check(heard["heard"], "candidate steers the case")
        jumped = (await c.call_tool("next_question", {"question_id": "q3"})).data
        check(jumped.get("uid", "").endswith("/q3"), "candidate-led jump honoured",
              str(jumped)[:120])
        say(url, "Each customer contributes 6 dollars at 70 percent, so 4.20. "
                 "600,000 divided by 4.20 is about 143,000 a year, which is "
                 "roughly 390 customers a day.")
        a = (await c.call_tool("collect_answer", {"max_wait": 10})).data
        check(a.get("graded") and a.get("outcome") == "correct",
              "jumped question grades correctly", str(a)[:140])
        back = (await c.call_tool("next_question", {"question_id": "q2"})).data
        check(back.get("uid", "").endswith("/q2"), "jump back also works")
        httpx.post(f"{url}/session/skip", json={}, timeout=10)
        await c.call_tool("collect_answer", {"max_wait": 10})
        card = (await c.call_tool("finish", {})).data
        check(card["questions_answered"] >= 1, "candidate-led case closes")
        dump_dialogue("MERIDIAN (candidate-led)")

        # ── flow 2: imported Kegging — exhibit data + data request ───────
        srv._room.state["dialogue"] = []
        await c.call_tool("start_case", {"case_id": "darden24-kegging_costs"})
        await c.call_tool("ask_case_prompt", {})
        nxt = (await c.call_tool("next_question", {})).data
        if nxt.get("has_exhibit"):
            pass  # exhibit auto-shown
        else:
            await c.call_tool("show_exhibit", {"exhibit_id": "ex1"})
        ex = srv._room.state.get("exhibit") or {}
        check(bool(ex.get("text")) and "$50" in (ex.get("text") or ""),
              "exhibit hands over the actual data", str(ex)[:160])
        say(url, "Before I calculate — do we know how many kegs they need?")
        req = (await c.call_tool("collect_answer", {"max_wait": 10})).data
        check(req.get("clarification_request") is True,
              "data request mid-imported-question is conversational", str(req)[:140])
        say(url, "Using the exhibit: leasing is 10 dollars a keg-year over ten years, "
                 "buying is 50 dollars with 5 percent replacement, so buying saves "
                 "roughly 10 million overall, which means purchase is the better deal.")
        ans = (await c.call_tool("collect_answer", {"max_wait": 10})).data
        check(ans.get("ready"), "imported math answer collected", str(ans)[:120])
        if not ans.get("graded") and ans.get("rubric"):
            ids = [x["id"] for x in ans["rubric"]["components"]][:3]
            await c.call_tool("score", {"covered": ids})
        await c.call_tool("finish", {})
        dump_dialogue("KEGGING (imported)")

        # ── flow 3: math drill with a follow-up ──────────────────────────
        srv._room.state["dialogue"] = []
        started = (await c.call_tool("start_drill",
                                     {"types": ["math"], "count": 3,
                                      "tags": ["drill"]})).data
        check(started.get("awaiting_start") is True, "drill uses the start gate")
        for _ in range(3):
            nq = (await c.call_tool("next_question", {})).data
            if nq.get("done"):
                break
            say(url, "About 2,500 dollars, and to improve it I'd raise price or "
                     "cut the fixed costs.")
            got = (await c.call_tool("collect_answer", {"max_wait": 10})).data
            check(got.get("ready"), "drill answer collected", str(got)[:100])
            if not got.get("graded") and got.get("rubric"):
                await c.call_tool("score", {"covered": []})
        await c.call_tool("finish", {})
        dump_dialogue("MATH DRILL")

        follow_qs = [d for d in srv._room.state["dialogue"]
                     if d["who"] == "interviewer" and "follow-up" in d["text"].lower()]
        check(bool(follow_qs), "playbook follow-ups are actually asked",
              "none appeared in the drill dialogue")

    bot.stop(); srv._server.stop()
    print(f"\n{CHECKS['pass']} passed · {CHECKS['fail']} failed")
    for w, d in FINDINGS:
        print(f"  • {w}\n    {d[:150]}")
    return 1 if FINDINGS else 0


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
