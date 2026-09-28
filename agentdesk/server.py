"""FastAPI app: serves the dashboard, streams engine events over a websocket, exposes controls."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

WEB = Path(__file__).parent / "web"


def create_app(engine, bus) -> FastAPI:
    app = FastAPI(title="AgentDesk")

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
            return JSONResponse({"ok": False, "error": f"halted: {engine.risk.st.halt_reason}. Restart to clear."}, 409)
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
        await sock.accept()
        q = bus.subscribe()
        try:
            await sock.send_text(json.dumps(engine.snapshot(), default=str))
            while True:
                msg = await q.get()
                batch = [msg]
                while not q.empty() and len(batch) < 200:
                    batch.append(q.get_nowait())
                await sock.send_text(json.dumps({"type": "batch", "events": batch}, default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bus.unsubscribe(q)

    return app
