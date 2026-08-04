import asyncio, os, time
os.environ.setdefault("CASECRAFT_NO_BROWSER","1"); os.environ.setdefault("CASECRAFT_DB","/tmp/cc-repro.db")
from fastmcp import Client
from casecraft import server as srv
from tests.harness import CANDIDATE_SCRIPT, SimulatedCandidate

async def main():
    url = srv._server.start()
    cand = SimulatedCandidate(url, [CANDIDATE_SCRIPT["clarify"], CANDIDATE_SCRIPT["structure"],
                                    CANDIDATE_SCRIPT["math_wrong"]])
    cand.start(); cand.wait_until_connected()
    t=time.monotonic()
    while time.monotonic()-t<5 and not srv._room.connected: time.sleep(0.02)
    async with Client(srv.mcp) as c:
        await c.call_tool("start_case", {"case_id":"casecraft-orchid-airlines"})
        await c.call_tool("ask_case_prompt", {})
        r=await c.call_tool("listen", {"max_wait":10}); print("listen ->", r.data.get("heard"), str(r.data.get("transcript"))[:40])
        q1=await c.call_tool("next_question", {}); print("q1 ->", q1.data.get("uid"))
        a1=await c.call_tool("collect_answer", {"max_wait":10}); print("a1 ->", a1.data.get("ready"), str(a1.data.get("transcript"))[:40])
        if a1.data.get("ready") and not a1.data.get("graded"):
            await c.call_tool("score", {"covered":["revenue"]})
        q2=await c.call_tool("next_question", {}); print("q2 ->", q2.data.get("uid"), q2.data.get("type"))
        a2=await c.call_tool("collect_answer", {"max_wait":10}); print("a2 ->", a2.data.get("ready"), a2.data.get("graded"))
    print("\ncandidate said:", len(cand.said), "answers left:", len(cand.answers))
    print("\n--- room events ---")
    for e in srv._room.events(limit=40):
        t0=e.pop("t"); ev=e.pop("event"); print(f"  {t0:7.2f} {ev:<22} {str(e)[:70]}")
    cand.stop(); srv._server.stop()
asyncio.run(main())
