"""Random-walk fuzz over the tool surface. Invariants, not scripts.

    .venv/bin/python -m tests.fuzz [seed]
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
import time

os.environ.setdefault("CASECRAFT_NO_BROWSER", "1")
os.environ.setdefault("CASECRAFT_DB", "/tmp/casecraft-fuzz.db")

import httpx
from fastmcp import Client

from casecraft import server as srv
from tests.playthrough import Bot

OPS = ["start_case", "start_drill", "ask_case_prompt", "next_question", "jump",
       "collect", "score", "probe", "reveal", "skip", "say_answer", "say_junk",
       "listen", "exhibit", "finish", "status", "repeat"]

ANSWERS = ["about 2.3 billion which means costs are the issue",
           "revenue, fixed costs, and variable costs — three buckets",
           "?", "what's the load factor", "ok", "x" * 5000, "第一 revenue 🎯"]


async def main(seed: int) -> int:
    rng = random.Random(seed)
    url = srv._server.start()
    bot = Bot(url, [])
    bot.start(); bot.wait_until_connected()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5 and not srv._room.connected:
        time.sleep(0.02)

    crashes = 0
    async with Client(srv.mcp) as c:
        async def call(tool, **kw):
            nonlocal crashes
            start = time.monotonic()
            try:
                await c.call_tool(tool, kw)
            except Exception as exc:                       # noqa: BLE001
                text = str(exc)
                # Clean refusals are fine; tracebacks and hangs are not.
                if "Traceback" in text or len(text) > 900:
                    crashes += 1
                    print(f"  UGLY ERROR from {tool}: {text[:200]}")
            elapsed = time.monotonic() - start
            if elapsed > 65:
                crashes += 1
                print(f"  HANG: {tool} took {elapsed:.0f}s")

        for i in range(180):
            op = rng.choice(OPS)
            if op == "start_case":
                await call("start_case", case_id=rng.choice(
                    ["casecraft-orchid-airlines", "casecraft-meridian-coffee", "nope"]))
            elif op == "start_drill":
                await call("start_drill", count=2, types=rng.choice([["math"], None, ["bogus"]]))
            elif op == "ask_case_prompt":
                await call("ask_case_prompt")
            elif op == "next_question":
                await call("next_question", read_aloud=rng.random() < 0.5)
            elif op == "jump":
                await call("next_question", question_id=rng.choice(["q1", "q3", "zz"]))
            elif op == "collect":
                await call("collect_answer", max_wait=5)
            elif op == "score":
                await call("score", covered=rng.sample(
                    ["revenue", "fixed_costs", "bogus", "mece"], k=rng.randint(0, 3)))
            elif op == "probe":
                await call("probe")
            elif op == "reveal":
                await call("reveal_model_answer")
            elif op == "skip":
                httpx.post(f"{url}/session/skip", json={}, timeout=10)
            elif op in ("say_answer", "say_junk"):
                httpx.post(f"{url}/debug/act", json={
                    "action": "say", "text": rng.choice(ANSWERS)}, timeout=10)
            elif op == "listen":
                await call("listen", max_wait=5)
            elif op == "exhibit":
                await call("show_exhibit")
            elif op == "finish":
                await call("finish")
            elif op == "status":
                await call("room_status", events=5)
            elif op == "repeat":
                await call("repeat_question")

            # invariant: the room never claims to listen while the floor is
            # the interviewer's
            st = srv._room.state
            if st["listening"] and st["floor"] == "interviewer":
                crashes += 1
                print(f"  INVARIANT BROKEN at op {i}: listening while interviewer holds floor")

    bot.stop(); srv._server.stop()
    print(f"seed {seed}: {crashes} problems in 180 random ops")
    return 1 if crashes else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 7)))
