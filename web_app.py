"""
data-trace-agent — web UI

A thin FastAPI wrapper around `trace_agent.build_agent()` and
`trace_agent.astream_events()`. Serves a single-page chat UI at `/` and
streams typed events (tool_call / tool_result / ai_text / final) over a
WebSocket at `/ws` so the page can render the conversation alongside a
live tool-call log.

Run:
    export OPENROUTER_API_KEY=...
    python3 web_app.py            # http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from trace_agent import SAMPLE_QUESTIONS, astream_events, build_agent

_HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(_HERE, "static")
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")

log = logging.getLogger("web_app")

# Built once at startup, reused across all WebSocket sessions. Each socket
# keeps its own `history` so conversations don't leak between tabs.
_agent_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    client, agent = await build_agent()
    _agent_state["client"] = client
    _agent_state["agent"] = agent
    print("[web] agent ready, serving on http://127.0.0.1:8000", file=sys.stderr)
    try:
        yield
    finally:
        _agent_state.clear()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(INDEX_HTML)


@app.get("/api/samples")
async def samples():
    return {"questions": list(SAMPLE_QUESTIONS)}


@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    agent = _agent_state.get("agent")
    if agent is None:
        await socket.send_json({"type": "error", "message": "agent not initialized"})
        await socket.close()
        return

    history: list = []
    try:
        while True:
            raw = await socket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"text": raw}
            user_text = (payload.get("text") or "").strip()
            if not user_text:
                continue

            await socket.send_json({"type": "user_echo", "content": user_text})
            try:
                async for ev in astream_events(agent, history, user_text):
                    # `args` may include non-JSON-serializable values; coerce.
                    safe = _jsonable(ev)
                    await socket.send_json(safe)
            except Exception as e:
                log.exception("stream failed")
                await socket.send_json({"type": "error", "message": str(e)})
    except WebSocketDisconnect:
        return


def _jsonable(obj):
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return json.loads(json.dumps(obj, default=str))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "web_app:app",
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEB_PORT", "8000")),
        reload=False,
    )
