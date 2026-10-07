"""Shutdown hooks (review 2026-10-06): the review snapshot is saved right after the shutdown flatten, before the
sessions close (L17), and a Robinhood browser sign-in shows on the dashboard as well as in the log (M1)."""
import asyncio
import json
import logging
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import lifecycle
from agentdesk.__main__ import run_session
from agentdesk.bus import Bus
from agentdesk.server import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Engine:
    mode = "paper"

    def __init__(self, bus, closers=()):
        self.bus, self.price, self.closers, self.calls = bus, 765.0, list(closers), []
        self.open = [(SimpleNamespace(qty=1, contract=SimpleNamespace(label="SPY 765C")), None)]
        self.books = None
        self.l2_rh = SimpleNamespace(on_sign_in=None)

    def snapshot(self):
        return {"type": "snapshot", "ts": 1790694000.0, "day": "2026-09-29", "price": self.price}

    async def run(self):
        await asyncio.Event().wait()

    async def flatten(self, reason="manual flatten"):
        self.calls.append("flatten")
        self.open, self.price = [], 770.0            # what the shutdown flatten sold

    def stop(self):
        self.calls.append("stop")
        self.price = 766.0


def serve(engine, bus, cfg, **kw):
    async def main():
        port = free_port()
        server = lifecycle.Server(uvicorn.Config(create_app(engine, bus), host="127.0.0.1", port=port,
                                                 log_level="error", timeout_graceful_shutdown=1))
        stop = asyncio.Event()
        run = asyncio.create_task(run_session(engine, bus, server, cfg, "paper", "127.0.0.1", port, stop=stop,
                                              hard_exit_sec=None, grace=1, close_timeout=0.5, **kw))
        await asyncio.sleep(0.3)
        if "on_running" in cfg:
            cfg["on_running"]()
        stop.set()
        await asyncio.wait_for(run, 10)
    asyncio.run(main())


def cfg(tmp_path):
    return {"review": {"sessions_dir": str(tmp_path / "sessions"), "claims_dir": str(tmp_path / "claims"),
                       "snapshot_every_sec": 600}}


def test_snapshot_is_saved_after_the_flatten_and_before_the_sessions_close(tmp_path):
    seen = []
    snap = tmp_path / "sessions" / "2026-09-29.snapshot.json"

    async def close_robinhood():
        seen.append(json.loads(snap.read_text())["price"] if snap.exists() else None)
    bus = Bus()
    engine = Engine(bus, [close_robinhood])
    serve(engine, bus, cfg(tmp_path))
    assert engine.calls[0] == "flatten"
    assert seen == [770.0]                           # what was sold is on the review page even if the closers hang
    assert json.loads(snap.read_text())["price"] == 766.0          # and the final save still runs


def test_after_flatten_hook_failure_is_logged_and_shutdown_goes_on(caplog):
    order = []

    async def closer():
        order.append("close")

    def boom():
        order.append("after_flatten")
        raise OSError("disk full")
    engine = Engine(Bus(), [])

    async def main():
        server = SimpleNamespace(should_exit=False, force_exit=False)
        srv, eng = asyncio.create_task(asyncio.sleep(0)), asyncio.create_task(asyncio.sleep(0))
        await lifecycle.shutdown(engine, server, srv, eng, [closer], 0.5, 0.5, frozenset(asyncio.all_tasks()),
                                 after_flatten=boom)
    with caplog.at_level(logging.WARNING, logger="agentdesk.lifecycle"):
        asyncio.run(main())
    assert engine.calls[0] == "flatten" and order == ["after_flatten", "close"]
    assert "disk full" in caplog.text


def test_a_robinhood_sign_in_prompt_reaches_the_dashboard(tmp_path):
    bus = Bus()
    got = []
    bus.taps.append(got.append)
    engine = Engine(bus)
    c = cfg(tmp_path)
    c["on_running"] = lambda: engine.l2_rh.on_sign_in("Robinhood needs a sign-in: open the URL printed above")
    serve(engine, bus, c)
    logs = [m for m in got if m.get("type") == "log" and "needs a sign-in" in m.get("msg", "")]
    assert len(logs) == 1 and logs[0]["level"] == "warn"
