"""The interview room — a local page that does what MCP structurally cannot.

MCP has no access to the host's microphone or speakers, and every tool result is
rendered into the chat transcript. Both facts point at the same answer: put the
audio in a browser tab on the candidate's own machine, and let MCP carry only
control signals.

The division of labour:

    Claude ──MCP──► Room state ──SSE──► browser ──► 🔊 speech synthesis
                         ▲                    └───► 🎤 recognition
                         └────────POST /answer──────┘

The case prompt travels left-to-right only. It is spoken and then discarded; it
never returns through MCP, so it never lands on screen. Everything the candidate
*says* travels back, because that's their own words and showing them is useful.

Speech runs on the browser's built-in Web Speech API — zero install, zero model
download, and `speechSynthesis` takes a rate multiplier, so the three interview
speeds are exact rather than approximate. Recognition can be swapped for local
Whisper (see /transcribe) when it's available, for privacy and accuracy.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .session import Room

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 7654


def build_app(room: Room) -> FastAPI:
    app = FastAPI(title="casecraft room", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        # Never cached: the page is read from disk per request, so an edit (or
        # an update) must reach the tab on reload rather than being served from
        # a stale copy.
        return HTMLResponse(
            (STATIC / "room.html").read_text(),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )

    @app.get("/events")
    def events(request: Request) -> StreamingResponse:
        """SSE: every room mutation, plus a keepalive so proxies don't reap us."""
        def stream():
            q = room.subscribe()
            try:
                while not room.closing:
                    try:
                        state = q.get(timeout=1)
                        yield f"data: {json.dumps(state)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"     # also proves the tab is alive
            finally:
                room.unsubscribe(q)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/answer")
    async def answer(request: Request) -> JSONResponse:
        body = await request.json()
        text = (body.get("transcript") or "").strip()
        room.submit_transcript(text)
        return JSONResponse({"ok": True, "chars": len(text)})

    @app.post("/spoken")
    async def spoken(request: Request) -> JSONResponse:
        """The page reports that speech synthesis finished, naming the utterance."""
        try:
            body = await request.json()
        except Exception:                                   # noqa: BLE001
            body = {}
        room.mark_spoken((body or {}).get("id"))
        return JSONResponse({"ok": True})

    @app.post("/listen/start")
    def listen_start() -> JSONResponse:
        """The candidate pressed talk. The mic is theirs, not the interviewer's."""
        room.arm(status="Listening — press Done when you've finished.")
        return JSONResponse({"ok": True})

    @app.post("/session/start")
    def session_start() -> JSONResponse:
        """The candidate pressed Start. Also the gesture that unlocks audio."""
        room.page_info = {**room.page_info, "audio_unlocked": True}
        room.mark_started()
        return JSONResponse({"ok": True})

    @app.post("/session/skip")
    def session_skip() -> JSONResponse:
        """Skip this question — for testing the flow without answering everything."""
        room.request_skip()
        return JSONResponse({"ok": True})

    @app.post("/speed")
    async def speed(request: Request) -> JSONResponse:
        body = await request.json()
        room.update(speed=body.get("speed", "moderate"))
        return JSONResponse({"ok": True})

    @app.post("/mode")
    async def set_mode(request: Request) -> JSONResponse:
        body = await request.json()
        mode = (body or {}).get("mode", "text")
        room.mode = "voice" if mode == "voice" else "text"
        room.update(mode=room.mode)
        room.log("mode.changed", mode=room.mode)
        return JSONResponse({"ok": True, "mode": room.mode})

    @app.get("/config")
    def config() -> JSONResponse:
        """Tells the page which speech engine to use. Local wins when present."""
        from . import stt

        return JSONResponse({
            "local_stt": stt.available(),
            "model": stt.MODEL_NAME if stt.available() else None,
        })

    @app.post("/transcribe")
    async def transcribe(request: Request) -> JSONResponse:
        """On-device transcription of one complete utterance.

        The page posts the raw MediaRecorder blob here instead of using browser
        recognition, so audio never leaves the machine.
        """
        from . import stt

        if not stt.available():
            return JSONResponse({"ok": False, "error": "local stt unavailable"}, status_code=503)

        audio = await request.body()
        if not audio:
            return JSONResponse({"ok": False, "error": "empty audio"}, status_code=400)

        # MediaRecorder's container varies by browser (webm on Chrome, mp4 on
        # Safari). PyAV probes content, but giving it the right extension keeps
        # it from guessing wrong on ambiguous headers.
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip()
        suffix = {
            "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".mp4",
            "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
            "audio/aiff": ".aiff", "audio/x-aiff": ".aiff",
        }.get(ctype, ".webm")

        try:
            text = await run_in_threadpool(stt.transcribe, audio, suffix)
        except Exception as exc:                            # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "transcript": text})

    # Partials are re-transcriptions of the whole utterance so far, so cost
    # grows with length. Past this the live pass falls behind real time and is
    # worth less than the latency it costs — the final pass still gets it all.
    PARTIAL_LIMIT_BYTES = 3_000_000

    @app.post("/transcribe/partial")
    async def transcribe_partial(request: Request) -> JSONResponse:
        """Live transcription of the audio captured so far this turn."""
        from . import stt

        if not stt.available():
            return JSONResponse({"ok": False, "error": "local stt unavailable"}, status_code=503)
        audio = await request.body()
        if not audio:
            return JSONResponse({"ok": True, "transcript": ""})
        if len(audio) > PARTIAL_LIMIT_BYTES:
            return JSONResponse({"ok": True, "transcript": None, "too_long": True})
        try:
            text = await run_in_threadpool(stt.transcribe, audio, ".webm", partial=True)
        except Exception as exc:                            # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "transcript": text})

    @app.post("/hello")
    async def hello(request: Request) -> JSONResponse:
        """The page identifies its build and whether audio is unlocked."""
        try:
            body = await request.json()
        except Exception:                                   # noqa: BLE001
            body = {}
        room.page_info = {"build": (body or {}).get("build"),
                          "audio_unlocked": bool((body or {}).get("audio_unlocked"))}
        return JSONResponse({"ok": True, "expected_build": room.BUILD})

    @app.get("/debug")
    def debug(limit: int = 60) -> JSONResponse:
        """Everything about this room, for an agent diagnosing it.

        Deliberately unauthenticated and read-only on 127.0.0.1: the point is
        that any agent can inspect the room without a human describing what
        they see on screen.
        """
        return JSONResponse({**room.diagnostics(), "events": room.events(limit=limit)})

    @app.post("/debug/act")
    async def debug_act(request: Request) -> JSONResponse:
        """Drive the room as if we were the candidate — for autonomous testing.

        `press_start`, `say` (submit an utterance), `open_mic`, `hello`.
        Everything the browser can do, an agent can do.
        """
        body = await request.json()
        action = (body or {}).get("action")

        if action == "press_start":
            room.page_info = {**room.page_info, "audio_unlocked": True}
            room.mark_started()
        elif action == "say":
            room.submit_transcript((body or {}).get("text", ""))
        elif action == "open_mic":
            room.arm()
        elif action == "hello":
            room.page_info = {"build": (body or {}).get("build") or room.BUILD,
                              "audio_unlocked": True}
        elif action == "ack_speech":
            room.mark_spoken((body or {}).get("id"))
        elif action == "set_state":
            # Drive the page through arbitrary states so the UI state machine
            # can be tested without a human clicking through every branch.
            room.update(**((body or {}).get("state") or {}))
        else:
            return JSONResponse({"ok": False, "error": f"unknown action {action!r}"},
                                status_code=400)
        room.log("agent.act", action=action)
        return JSONResponse({"ok": True, "action": action, **room.diagnostics()})

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True, "connected": room.connected})

    return app


def _free_port(preferred: int = DEFAULT_PORT) -> int:
    for port in range(preferred, preferred + 200):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free port in range")


class RoomServer:
    """uvicorn on a daemon thread, so FastMCP keeps the main thread."""

    def __init__(self, room: Room) -> None:
        self.room = room
        self.port: int | None = None
        self._thread: threading.Thread | None = None
        self._server = None

    @property
    def url(self) -> str | None:
        return f"http://127.0.0.1:{self.port}" if self.port else None

    def start(self) -> str:
        if self._thread and self._thread.is_alive():
            return self.url                                   # type: ignore[return-value]

        import uvicorn

        self.port = _free_port()
        config = uvicorn.Config(
            build_app(self.room), host="127.0.0.1", port=self.port,
            log_level="error", access_log=False,
        )
        server = uvicorn.Server(config)
        self._server = server
        self._thread = threading.Thread(target=server.run, daemon=True, name="casecraft-room")
        self._thread.start()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return self.url                           # type: ignore[return-value]
            time.sleep(0.05)
        raise RuntimeError("room server failed to start")

    def stop(self) -> None:
        """Shut the room down and release its port.

        Without this every restart leaks a listener; a long session walks up the
        port range until it runs out.
        """
        self.room.close()          # unblock the SSE generators first
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._server = None
        self.port = None

    def open_browser(self) -> None:
        import os
        import webbrowser

        if os.environ.get("CASECRAFT_NO_BROWSER"):
            return
        if self.url:
            webbrowser.open(self.url)

    def wait_for_page(self, timeout: float = 45.0) -> bool:
        """Block until a tab actually connects — a session with no ears is useless."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.room.connected:
                return True
            time.sleep(0.1)
        return False
