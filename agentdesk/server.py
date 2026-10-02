"""FastAPI app: serves the dashboard, streams engine events over a websocket, exposes controls.

Controls (kill, flatten, pause, resume, proposal decisions) need the per-run token in an X-AgentDesk-Token
header. The token reaches the page only through the link printed in the terminal (#token=...), never from a
URL the server serves, so another site can't read it. Requests from another site's pages (Origin) or through
a hostname other than this machine's (DNS rebinding) are refused outright.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

WEB = Path(__file__).parent / "web"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def resolve_token(server_cfg) -> str:
    """AGENTDESK_TOKEN env var, else server.token in config, else a fresh random token for this run."""
    return os.environ.get("AGENTDESK_TOKEN") or (server_cfg or {}).get("token") or secrets.token_urlsafe(24)


def _hostname(value: str | None) -> str | None:
    if not value:
        return None
    return urlsplit(value if "://" in value else f"//{value}").hostname


def create_app(engine, bus, token: str | None = None, allowed_hosts=()) -> FastAPI:
    app = FastAPI(title="AgentDesk")
    token = token or resolve_token({})
    hosts = LOCAL_HOSTS | {h for h in allowed_hosts if h and h not in ("0.0.0.0", "::")}

    def host_ok(headers) -> bool:
        return _hostname(headers.get("host")) in hosts

    def origin_ok(headers) -> bool:          # browsers send Origin on cross-site POSTs and on every websocket
        o = headers.get("origin")
        return o is None or _hostname(o) in hosts

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not host_ok(request.headers):
            return JSONResponse({"ok": False, "error": "unknown host"}, 403)
        if request.method not in SAFE_METHODS:
            if not origin_ok(request.headers):
                return JSONResponse({"ok": False, "error": "cross-site request refused"}, 403)
            if not secrets.compare_digest(request.headers.get("x-agentdesk-token", ""), token):
                return JSONResponse({"ok": False, "error": "Controls need the dashboard link printed in the terminal "
                                                           "(it carries this run's control token)."}, 401)
        return await call_next(request)

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html")

    @app.get("/config.js")
    async def config_js():
        return FileResponse(WEB / "config.local.js", media_type="application/javascript")

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/api/state")
    async def state():
        return JSONResponse(json.loads(json.dumps(engine.snapshot(), default=str)))

    @app.get("/api/trades")
    async def trades():
        return engine.journal.trades(limit=200)

    @app.post("/api/kill")
    async def kill():
        await engine.kill()
        return {"ok": True}

    @app.post("/api/flatten")
    async def flatten():
        await engine.flatten()
        return {"ok": True}

    @app.post("/api/pause")
    async def pause():
        engine.pause(True)
        return {"ok": True}

    @app.post("/api/resume")
    async def resume():
        if engine.risk.st.halted:
            return JSONResponse({"ok": False, "error": f"halted: {engine.risk.st.halt_reason}. To clear it, restart with --clear-halt "
                                 "(today's P&L, trade count and loss limit carry over)."}, 409)
        engine.pause(False)
        return {"ok": True}

    @app.get("/api/proposals")
    async def proposals():
        return engine.crew.book.items[-50:] if engine.crew else []

    @app.post("/api/proposals/{pid}/{decision}")
    async def decide(pid: str, decision: str):
        if decision not in ("approve", "reject") or not engine.crew:
            return JSONResponse({"ok": False, "error": "bad request"}, 400)
        item = engine.decide_proposal(pid, decision == "approve")
        if item is None:
            return JSONResponse({"ok": False, "error": "proposal not pending"}, 409)
        return {"ok": True, "item": item}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        if not (host_ok(sock.headers) and origin_ok(sock.headers)):
            await sock.close(code=1008)
            return
        await sock.accept()
        q = bus.subscribe()
        gone = asyncio.create_task(_until_disconnect(sock))    # a quiet bus must not keep a closed socket alive
        try:
            await sock.send_text(json.dumps(engine.snapshot(), default=str))
            while True:
                nxt = asyncio.ensure_future(q.get())
                await asyncio.wait({nxt, gone}, return_when=asyncio.FIRST_COMPLETED)
                if not nxt.done():
                    nxt.cancel()
                    break
                batch = [nxt.result()]
                while not q.empty() and len(batch) < 200:
                    batch.append(q.get_nowait())
                if bus.take_overflow(q):        # this tab fell behind and lost events: resync it from a fresh snapshot
                    while not q.empty():
                        q.get_nowait()
                    await sock.send_text(json.dumps(engine.snapshot(), default=str))
                    continue
                await sock.send_text(json.dumps({"type": "batch", "events": batch}, default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            gone.cancel()
            bus.unsubscribe(q)

    return app


async def _until_disconnect(sock: WebSocket) -> None:
    """Returns when the client (or the shutting-down server) closes the socket. Clients never send data."""
    while True:
        msg = await sock.receive()
        if msg["type"] == "websocket.disconnect":
            return
