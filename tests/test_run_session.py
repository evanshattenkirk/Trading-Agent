"""The run command claims the dashboard port (the review server steps aside) and saves the session for review."""
import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import claims, lifecycle
from agentdesk.__main__ import run_session
from agentdesk.archive import SessionArchive, attach
from agentdesk.bus import Bus
from agentdesk.server import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Engine:
    open, mode = [], "paper"

    def __init__(self, bus):
        self.bus, self.stopped, self.price = bus, False, 765.0

    def snapshot(self):
        return {"type": "snapshot", "ts": 1790694000.0, "day": "2026-09-29", "price": self.price}

    async def run(self):
        self.bus.emit("log", 1790694000.0, msg="engine up")
        await asyncio.Event().wait()

    def stop(self):
        self.stopped = True
        self.price = 766.0                     # what shutdown left behind: the final snapshot must carry it


def cfg(tmp_path, **review):
    return {"review": {"sessions_dir": str(tmp_path / "sessions"), "claims_dir": str(tmp_path / "claims"),
                       "snapshot_every_sec": 0.05, **review}}


def test_attach_only_for_real_sessions(tmp_path):
    bus = Bus()
    assert attach(cfg(tmp_path), "sim", bus) is None and bus.taps == []
    arc = attach(cfg(tmp_path), "paper", bus)
    assert isinstance(arc, SessionArchive) and arc.dir == tmp_path / "sessions" and len(bus.taps) == 1
    assert attach(cfg(tmp_path, modes=["paper", "sim"]), "sim", Bus()) is not None


def test_run_session_saves_snapshot_events_and_final_state(tmp_path):
    async def main():
        port, bus = free_port(), Bus()
        engine = Engine(bus)
        server = lifecycle.Server(uvicorn.Config(create_app(engine, bus), host="127.0.0.1", port=port, log_level="error",
                                                 timeout_graceful_shutdown=1))
        stop = asyncio.Event()
        run = asyncio.create_task(run_session(engine, bus, server, cfg(tmp_path), "paper", "127.0.0.1", port,
                                              stop=stop, hard_exit_sec=None))
        snap = tmp_path / "sessions" / "2026-09-29.snapshot.json"
        for _ in range(100):
            if snap.exists():
                break
            await asyncio.sleep(0.05)
        assert json.loads(snap.read_text())["price"] == 765.0          # periodic save while running
        stop.set()
        await asyncio.wait_for(run, 8)
        return snap
    snap = asyncio.run(main())
    assert json.loads(snap.read_text())["price"] == 766.0              # saved again after shutdown
    events = (tmp_path / "sessions" / "2026-09-29.events.jsonl").read_text().splitlines()
    assert json.loads(events[0])["msg"] == "engine up"


def test_run_session_in_sim_saves_nothing(tmp_path):
    async def main():
        port, bus = free_port(), Bus()
        engine = Engine(bus)
        server = lifecycle.Server(uvicorn.Config(create_app(engine, bus), host="127.0.0.1", port=port, log_level="error",
                                                 timeout_graceful_shutdown=1))
        stop = asyncio.Event()
        run = asyncio.create_task(run_session(engine, bus, server, cfg(tmp_path), "sim", "127.0.0.1", port,
                                              stop=stop, hard_exit_sec=None))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(run, 8)
    asyncio.run(main())
    assert not (tmp_path / "sessions").exists()


def test_run_session_waits_for_the_review_server_to_let_go(tmp_path):
    async def main():
        port, bus = free_port(), Bus()
        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", port))
        holder.listen()                                                   # the review page, about to step aside
        asyncio.get_running_loop().call_later(0.4, holder.close)
        engine = Engine(bus)
        server = lifecycle.Server(uvicorn.Config(create_app(engine, bus), host="127.0.0.1", port=port, log_level="error",
                                                 timeout_graceful_shutdown=1))
        stop = asyncio.Event()
        run = asyncio.create_task(run_session(engine, bus, server, cfg(tmp_path), "paper", "127.0.0.1", port,
                                              stop=stop, hard_exit_sec=None))
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started                                             # bound once the port was free
        stop.set()
        await asyncio.wait_for(run, 8)
    asyncio.run(main())


def test_claim_is_held_for_the_whole_run_and_released(tmp_path):
    from agentdesk.__main__ import dashboard_claim
    with dashboard_claim(cfg(tmp_path)):
        assert claims.claimed(tmp_path / "claims") == [os.getpid()]
    assert claims.claimed(tmp_path / "claims") == []
