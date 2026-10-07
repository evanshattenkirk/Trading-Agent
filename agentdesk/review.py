"""After-hours dashboard: `python -m agentdesk review` (launchd com.agentdesk.review keeps it running).

Serves the dashboard page on the engine's host and port with the last saved paper session (agentdesk/archive.py),
read-only: GET only, no control endpoints, no websocket, and no broker, data or Claude calls. It listens only while
no live process has claimed the port (agentdesk/claims.py), so the engine takes http://127.0.0.1:8765 back at
08:10 CT and the review page returns when the engine stops.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import claims
from .archive import load_day
from .server import LOCAL_HOSTS, SAFE_METHODS, WEB, _hostname

log = logging.getLogger("agentdesk.review")
NOTHING = {"ok": False, "error": "No saved session yet. The engine saves one from its next paper run."}


def create_review_app(sessions_dir: Path | str, allowed_hosts=()) -> FastAPI:
    app = FastAPI(title="AgentDesk review", docs_url=None, redoc_url=None, openapi_url=None)
    hosts = LOCAL_HOSTS | {h for h in allowed_hosts if h and h not in ("0.0.0.0", "::")}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if _hostname(request.headers.get("host")) not in hosts:
            return JSONResponse({"ok": False, "error": "unknown host"}, 403)
        if request.method not in SAFE_METHODS:
            return JSONResponse({"ok": False, "error": "Review mode is read-only; the engine is not running."}, 405)
        return await call_next(request)

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html")

    @app.get("/config.js")
    async def config_js():
        return Response("window.AGENTDESK = { source: 'review' };\n", media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/api/review")
    async def review(day: str | None = None):
        data = load_day(sessions_dir, day)
        return JSONResponse(data, headers={"Cache-Control": "no-store"}) if data else JSONResponse(NOTHING, 404)

    @app.get("/api/state")
    async def state(day: str | None = None):
        data = load_day(sessions_dir, day, max_events=0)
        return JSONResponse(data["snapshot"]) if data else JSONResponse(NOTHING, 404)

    return app


class ReviewSupervisor:
    """Serves `app` on host:port whenever no live process claims the port; checks every `poll` seconds."""

    def __init__(self, app, host: str, port: int, claims_dir: Path | str, poll: float = 2.0):
        self.app, self.host, self.port, self.claims_dir, self.poll = app, host, port, Path(claims_dir), poll
        self.server: uvicorn.Server | None = None
        self.task: asyncio.Task | None = None
        self._busy_logged = False
        self._claim_logged = False

    @property
    def serving(self) -> bool:
        return self.task is not None and not self.task.done()

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                await self.step()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), self.poll)
        finally:
            await self._stop_serving()

    async def step(self) -> None:
        held = claims.claimed(self.claims_dir)
        if held:
            if self.serving:
                log.info("dashboard port claimed by pid %s; review page off", ", ".join(map(str, held)))
                await self._stop_serving()
            elif not self._claim_logged:                # e.g. at startup: say why the page isn't up
                log.info("review page off: pid %s holds a claim in %s", ", ".join(map(str, held)), self.claims_dir)
            self._claim_logged = True
            return
        self._claim_logged = False
        if self.serving:
            return
        if self.task is not None:                      # it stopped on its own (bind error); start over
            self.task = self.server = None
        if claims.port_in_use(self.host, self.port):
            if not self._busy_logged:
                log.warning("port %s is in use by another process; waiting", self.port)
                self._busy_logged = True
            return
        self._busy_logged = False
        from .lifecycle import Server
        self.server = Server(uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning",
                                            timeout_graceful_shutdown=1))
        self.task = asyncio.create_task(self._serve(self.server), name="review-dashboard")
        log.info("review page on http://%s:%s", self.host, self.port)

    @staticmethod
    async def _serve(server) -> None:
        try:
            await server.serve()
        except SystemExit:                             # uvicorn exits on a bind error; keep the supervisor alive
            log.warning("review server could not bind; will retry")

    async def _stop_serving(self) -> None:
        if self.server is not None and self.serving:
            self.server.should_exit = True
            done, _ = await asyncio.wait({self.task}, timeout=3)
            if not done:
                self.server.force_exit = True
                await asyncio.wait({self.task}, timeout=2)
                self.task.cancel()
        self.task = self.server = None


async def serve_forever(app, host: str, port: int, claims_dir: Path | str, poll: float = 2.0) -> None:
    """Runs the supervisor until SIGINT/SIGTERM."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
    await ReviewSupervisor(app, host, port, claims_dir, poll).run(stop)
