"""Soak test — run EVERY case and drill to completion, and abuse the API.

The single-path playthrough passing proves almost nothing: it exercised one
bundled case with well-formed data and a cooperative call order. Most of the
library is imported from PDFs, where fields are missing, rubrics are `open`
rather than `buckets`, exhibits have no data, and probe lists are empty. And
nothing had ever called the tools in the wrong order.

This runs all of it and reports every failure instead of stopping at the first.

    .venv/bin/python -m tests.soak
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback

os.environ.setdefault("CASECRAFT_NO_BROWSER", "1")
os.environ.setdefault("CASECRAFT_DB", "/tmp/casecraft-soak.db")

import httpx                                          # noqa: E402
from fastmcp import Client                            # noqa: E402

from casecraft import server as srv                   # noqa: E402
from casecraft.library import Library                 # noqa: E402
from tests.playthrough import Bot, check, CHECKS, FINDINGS, debug, say   # noqa: E402


async def drive_case(c, url, bot, case_id: str) -> None:
    """Take one case from start to scorecard, answering everything."""
    started = (await c.call_tool("start_case", {"case_id": case_id})).data
    n = started["question_count"]

    blob = json.dumps(started)
    check("clarification_topics" not in blob or all(
        r.get("response", "@@") not in blob
        for r in Library().cases[case_id].clarifications),
        f"[{case_id}] briefing leaks no clarification answers")

    await c.call_tool("ask_case_prompt", {})

    asked = 0
    for _ in range(n + 2):
        nxt = (await c.call_tool("next_question", {})).data
        if nxt.get("done"):
            break
        asked += 1
        check("prompt" not in nxt, f"[{case_id}] question text never returned",
              json.dumps(nxt)[:120])

        say(url, "Revenue is passengers times price. Costs split into fixed like "
                 "leases and variable like fuel. It comes out to about 2.3 billion, "
                 "which means this is a cost problem, not a revenue one.")
        ans = (await c.call_tool("collect_answer", {"max_wait": 15})).data
        if not ans.get("ready"):
            ans = (await c.call_tool("collect_answer", {"max_wait": 15})).data
        check(ans.get("ready"), f"[{case_id}] {nxt.get('uid')} answer collected",
              json.dumps(ans)[:140])

        if ans.get("ready") and not ans.get("graded") and not ans.get("skipped"):
            rubric = ans.get("rubric") or {}
            ids = [x["id"] for x in rubric.get("components", [])]
            check(rubric, f"[{case_id}] {nxt.get('uid')} ungraded answer has a rubric",
                  json.dumps(ans)[:140])
            if ids:
                scored = (await c.call_tool("score", {"covered": ids[:2]})).data
                check("outcome" in scored, f"[{case_id}] score returned a verdict",
                      json.dumps(scored)[:140])

        # probe must not explode even when the case has no probes
        (await c.call_tool("probe", {})).data

    check(asked >= 1, f"[{case_id}] at least one question was asked")
    card = (await c.call_tool("finish", {})).data
    check("dimensions" in card, f"[{case_id}] scorecard produced", json.dumps(card)[:140])
    check(card.get("limiting_factor") in (card.get("dimensions") or {}) or not card.get("dimensions"),
          f"[{case_id}] limiting factor is real", str(card.get("limiting_factor")))


async def abuse(c, url) -> None:
    """Call things in the wrong order and with bad inputs. Nothing may hang or 500."""
    print("\n── calling tools out of order ────────────────────────")

    async def must_error(tool, kw, what):
        try:
            await c.call_tool(tool, kw)
            check(False, what, "call succeeded when it should have been refused")
        except Exception as exc:                       # noqa: BLE001
            msg = str(exc)
            check("Traceback" not in msg and len(msg) < 800, what,
                  f"error was not clean: {msg[:200]}")

    async def must_survive(tool, kw, what):
        try:
            await c.call_tool(tool, kw)
            check(True, what)
        except Exception as exc:                       # noqa: BLE001
            check("no active question" in str(exc).lower()
                  or "grade the answer" in str(exc).lower()
                  or "no exhibit" in str(exc).lower()
                  or "awaiting" in str(exc).lower(),
                  what, f"unexpected error: {str(exc)[:200]}")

    await must_survive("score", {"covered": ["bogus_id"]}, "score with no pending answer is clean")
    await must_survive("probe", {}, "probe without a question is clean")
    await must_survive("reveal_model_answer", {}, "reveal before grading is clean")
    await must_survive("show_exhibit", {"exhibit_id": "nope"}, "bad exhibit id is clean")
    await must_error("start_case", {"case_id": "does-not-exist"}, "unknown case id refused")
    await must_error("start_drill", {"types": ["nonsense"]}, "impossible drill refused")

    print("\n── restarting mid-case ───────────────────────────────")
    await c.call_tool("start_case", {"case_id": "casecraft-orchid-airlines"})
    await c.call_tool("ask_case_prompt", {})
    await c.call_tool("next_question", {})
    restarted = (await c.call_tool("start_case", {"case_id": "casecraft-meridian-coffee"})).data
    check(restarted.get("awaiting_start") is True, "restarting mid-case re-arms the gate")
    d = debug(url)
    check(d["queued_utterances"] == 0, "restart clears stale utterances",
          f"queued={d['queued_utterances']}")

    print("\n── finishing twice ───────────────────────────────────")
    await c.call_tool("finish", {})
    twice = (await c.call_tool("finish", {})).data
    check("dimensions" in twice, "finishing twice is harmless")

    print("\n── scorecard with nothing answered ───────────────────")
    await c.call_tool("start_case", {"case_id": "casecraft-orchid-airlines"})
    empty = (await c.call_tool("finish", {})).data
    check(empty.get("questions_answered") == 0, "empty scorecard reports zero",
          json.dumps(empty)[:140])


async def drills(c, url) -> None:
    print("\n── drill mode ────────────────────────────────────────")
    for kinds in (["math"], ["structure", "synthesis"], None):
        started = (await c.call_tool(
            "start_drill", {"types": kinds, "count": 3} if kinds
            else {"count": 3})).data
        label = kinds or "any"
        check("questions" in started, f"drill {label} started", json.dumps(started)[:120])
        blob = json.dumps(started)
        check("prompt" not in blob, f"drill {label} leaks no question text", blob[:140])

        for _ in range(3):
            nxt = (await c.call_tool("next_question", {})).data
            if nxt.get("done"):
                break
            say(url, "About 2.3 billion, which means costs are the problem.")
            ans = (await c.call_tool("collect_answer", {"max_wait": 15})).data
            if not ans.get("ready"):
                ans = (await c.call_tool("collect_answer", {"max_wait": 15})).data
            check(ans.get("ready"), f"drill {label} answer collected",
                  json.dumps(ans)[:140])
            if ans.get("ready") and not ans.get("graded"):
                ids = [x["id"] for x in (ans.get("rubric") or {}).get("components", [])]
                if ids:
                    await c.call_tool("score", {"covered": ids[:1]})
        (await c.call_tool("finish", {})).data


async def run() -> None:
    url = srv._server.start()
    bot = Bot(url, [])
    bot.start()
    bot.wait_until_connected()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not srv._room.connected:
        time.sleep(0.02)

    lib = Library()
    ids = list(lib.cases)
    print(f"\n══ soaking {len(ids)} cases ══")

    async with Client(srv.mcp) as c:
        for i, case_id in enumerate(ids, 1):
            t0 = time.monotonic()
            try:
                await drive_case(c, url, bot, case_id)
                print(f"  [{i:>2}/{len(ids)}] {case_id[:44]:<46} "
                      f"{time.monotonic()-t0:5.1f}s")
            except Exception:                          # noqa: BLE001
                check(False, f"[{case_id}] crashed mid-case",
                      traceback.format_exc().splitlines()[-1])
                print(f"  [{i:>2}/{len(ids)}] {case_id[:44]:<46} CRASH")

        await drills(c, url)
        await abuse(c, url)

    bot.stop()
    srv._server.stop()


def main() -> int:
    asyncio.run(run())
    print("\n" + "═" * 66)
    print(f"  {CHECKS['pass']} passed · {CHECKS['fail']} failed")
    if FINDINGS:
        seen = set()
        print("\n  FINDINGS")
        for what, detail in FINDINGS:
            key = what.split("]")[-1].strip()
            if key in seen:
                continue
            seen.add(key)
            print(f"   • {what}")
            if detail:
                print(f"     {detail[:170]}")
        print(f"\n  ({len(FINDINGS)} total, {len(seen)} distinct)")
    else:
        print("  no findings")
    print("═" * 66)
    return 1 if FINDINGS else 0


if __name__ == "__main__":
    raise SystemExit(main())
