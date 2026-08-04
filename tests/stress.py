"""Round 2: concurrency, state leaks, and resource exhaustion."""
import asyncio, os, time, gc
os.environ.setdefault("CASECRAFT_NO_BROWSER", "1")
os.environ.setdefault("CASECRAFT_DB", "/tmp/cc-abuse2.db")
import httpx
from fastmcp import Client
from casecraft import server as srv
from casecraft.room import RoomServer
from casecraft.session import Room
from tests.playthrough import Bot, check, CHECKS, FINDINGS, say, debug

async def main():
    url = srv._server.start()
    bot = Bot(url, []); bot.start(); bot.wait_until_connected()
    t=time.monotonic()
    while time.monotonic()-t < 5 and not srv._room.connected: time.sleep(0.02)

    async with Client(srv.mcp) as c:
        print("\n-- concurrent collect_answer --")
        await c.call_tool("start_case", {"case_id":"casecraft-orchid-airlines"})
        await c.call_tool("ask_case_prompt", {})
        await c.call_tool("next_question", {})
        say(url, "answer one"); say(url, "answer two")
        a, b = await asyncio.gather(
            c.call_tool("collect_answer", {"max_wait":6}),
            c.call_tool("collect_answer", {"max_wait":6}))
        ready = [r.data for r in (a, b) if r.data.get("ready")]
        # Coalescing: everything said since the question was asked is ONE
        # answer, so exactly one collector wins and gets both parts.
        check(len(ready) == 1
              and "answer one" in ready[0]["transcript"]
              and "answer two" in ready[0]["transcript"],
              "concurrent collects coalesce into one whole answer",
              str([r.data.get("transcript") for r in (a, b)]))

        print("-- score twice --")
        say(url, "revenue and fixed costs and variable costs")
        await c.call_tool("collect_answer", {"max_wait":10})
        await c.call_tool("score", {"covered":["revenue"]})
        try:
            await c.call_tool("score", {"covered":["revenue"]})
            check(False, "double score refused", "second score succeeded")
        except Exception as e:
            check("collect_answer first" in str(e), "double score refused cleanly", str(e)[:120])

        print("-- pending answer does not survive a new case --")
        say(url, "orphan answer")
        await c.call_tool("collect_answer", {"max_wait":10})
        await c.call_tool("start_case", {"case_id":"casecraft-meridian-coffee"})
        try:
            r = await c.call_tool("score", {"covered":["market"]})
            check(False, "stale pending answer leaked into the new case", str(r.data)[:140])
        except Exception as e:
            check(True, "stale pending answer rejected")

        print("-- rapid fire --")
        t0=time.monotonic()
        for _ in range(30):
            await c.call_tool("room_status", {"events":5})
        check(time.monotonic()-t0 < 20, "30 status calls stay fast",
              f"{time.monotonic()-t0:.1f}s")

        print("-- log is bounded --")
        for i in range(600):
            srv._room.log("noise", i=i)
        check(len(srv._room.events(limit=10_000)) <= Room.LOG_LIMIT,
              "event log is bounded", len(srv._room.events(limit=10_000)))

    bot.stop(); srv._server.stop()

    print("-- rooms start and stop without leaking ports --")
    ports=set()
    for _ in range(12):
        r=Room(); s=RoomServer(r); s.start(); ports.add(s.port); s.stop()
    check(len(ports) <= 3, "ports are released and reused", f"used {sorted(ports)}")
    gc.collect()

asyncio.run(main())
print(f"\n{CHECKS['pass']} passed · {CHECKS['fail']} failed")
for w,d in FINDINGS: print(f"  • {w}\n    {d[:160]}")
