"""Ctrl-C / SIGTERM shut the engine and dashboard down within seconds, even with a dashboard tab open on a quiet
bus, a Robinhood MCP session to close, or a close() that hangs."""
import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import uvicorn
import websockets
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentdesk import lifecycle
from agentdesk.brokers.robinhood import RobinhoodMCP
from agentdesk.bus import Bus
from agentdesk.config import load_config
from agentdesk.server import create_app

CFG = load_config()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class QuietEngine:
    """Nothing on the bus (pre-market paper): the websocket handler would wait forever for the next event."""
    open, mode = [], "paper"

    def __init__(self):
        self.stopped = False

    def snapshot(self):
        return {"type": "snapshot"}

    async def run(self):
        await asyncio.Event().wait()

    def stop(self):
        self.stopped = True


async def wait_listening(port: int, timeout: float = 5.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            _, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError("server never came up")


async def hold_socket(port: int, got_snapshot: asyncio.Event) -> None:
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
            await ws.recv()
            got_snapshot.set()
            while True:
                await ws.recv()
    except websockets.ConnectionClosed:
        pass


def test_open_dashboard_socket_does_not_block_uvicorn_shutdown():
    """The websocket handler notices the close instead of waiting on a quiet bus (uvicorn waits for it)."""
    async def main():
        port = free_port()
        engine, bus = QuietEngine(), Bus()
        server = lifecycle.Server(uvicorn.Config(create_app(engine, bus), host="127.0.0.1", port=port, log_level="error"))
        srv = asyncio.create_task(server.serve())         # no force_exit and no graceful timeout: plain should_exit
        await wait_listening(port)
        got = asyncio.Event()
        client = asyncio.create_task(hold_socket(port, got))
        await asyncio.wait_for(got.wait(), 5)
        t0 = time.monotonic()
        server.should_exit = True
        await asyncio.wait_for(srv, 5)
        client.cancel()
        return time.monotonic() - t0
    assert asyncio.run(main()) < 3


def test_serve_stops_engine_closes_sessions_and_skips_a_hanging_close():
    closed = []

    async def ok_close():
        closed.append("feed")

    async def hangs():
        closed.append("hang-start")
        await asyncio.Event().wait()

    async def main():
        port = free_port()
        engine, bus = QuietEngine(), Bus()
        server = lifecycle.Server(uvicorn.Config(create_app(engine, bus), host="127.0.0.1", port=port,
                                                 log_level="error", timeout_graceful_shutdown=1))
        stop = asyncio.Event()
        run = asyncio.create_task(lifecycle.serve(engine, server, [hangs, ok_close], stop=stop, grace=1,
                                                  close_timeout=0.5, hard_exit_sec=None))
        await wait_listening(port)
        got = asyncio.Event()
        asyncio.create_task(hold_socket(port, got))
        await asyncio.wait_for(got.wait(), 5)
        t0 = time.monotonic()
        stop.set()
        await asyncio.wait_for(run, 6)
        return engine, time.monotonic() - t0
    engine, took = asyncio.run(main())
    assert engine.stopped
    assert closed == ["hang-start", "feed"]
    assert took < 4


class HoldingEngine(QuietEngine):
    """An engine with book A and paper-book positions open at shutdown."""

    def __init__(self, flatten_takes=0.0):
        super().__init__()
        self.calls, self.takes = [], flatten_takes
        self.open = [(type("P", (), {"qty": 2, "contract": type("C", (), {"label": "SPY 765C"})()})(), None)]
        self.books = type("H", (), {"positions": lambda s: ["G1"]})()

    async def flatten(self, reason="manual flatten"):
        self.calls.append(("flatten", reason, self.stopped))
        await asyncio.sleep(self.takes)
        self.open = []

    def stop(self):
        self.calls.append(("stop",))
        super().stop()


def serve_until_stopped(engine, **kw):
    async def main():
        port = free_port()
        server = lifecycle.Server(uvicorn.Config(create_app(engine, Bus()), host="127.0.0.1", port=port,
                                                 log_level="error", timeout_graceful_shutdown=1))
        stop = asyncio.Event()
        run = asyncio.create_task(lifecycle.serve(engine, server, [], stop=stop, grace=1, close_timeout=0.5,
                                                  hard_exit_sec=None, **kw))
        await wait_listening(port)
        t0 = time.monotonic()
        stop.set()
        await asyncio.wait_for(run, 10)
        return time.monotonic() - t0
    return asyncio.run(main())


def test_shutdown_sells_open_positions_before_stopping_the_engine():
    engine = HoldingEngine()
    serve_until_stopped(engine)
    assert engine.calls[0] == ("flatten", "shutdown", False)       # while the engine, quotes and broker still run
    assert engine.calls[1] == ("stop",) and not engine.open


def test_shutdown_sells_paper_book_positions_even_when_book_a_is_flat():
    engine = HoldingEngine()
    engine.open = []
    serve_until_stopped(engine)
    assert engine.calls[0][:2] == ("flatten", "shutdown")


def test_shutdown_with_nothing_open_sends_nothing():
    engine = HoldingEngine()
    engine.open, engine.books = [], None
    serve_until_stopped(engine)
    assert engine.calls == [("stop",)]


def test_a_slow_flatten_is_cut_off_and_what_is_left_is_logged(caplog):
    engine = HoldingEngine(flatten_takes=30)
    with caplog.at_level("ERROR", logger="agentdesk.lifecycle"):
        took = serve_until_stopped(engine, flatten_timeout=0.5)
    assert took < 5 and engine.stopped
    assert "SPY 765C" in caplog.text and "still open" in caplog.text


# --------------------------------------------------------------------------- Robinhood MCP session
class TaskBound:
    """Mimics anyio: exiting the context from a different task than the one that entered it is an error."""

    def __init__(self, log, name, value):
        self.log, self.name, self.value = log, name, value

    async def __aenter__(self):
        self.task = asyncio.current_task()
        self.log.append(f"enter {self.name}")
        return self.value

    async def __aexit__(self, *exc):
        if asyncio.current_task() is not self.task:
            raise RuntimeError(f"{self.name}: exited in a different task than it was entered in")
        self.log.append(f"exit {self.name}")
        return False


class FakeSession:
    def __init__(self, log):
        self.log = log

    async def initialize(self):
        pass

    async def list_tools(self):
        class T:
            name, inputSchema = "get_accounts", {}
        return type("L", (), {"tools": [T()]})()


def test_robinhood_session_is_closed_by_the_task_that_opened_it():
    log: list[str] = []
    rh = RobinhoodMCP(CFG)

    async def main():
        async def opener():            # the engine task opens the session...
            ready = asyncio.get_running_loop().create_future()
            rh._closing = asyncio.Event()
            rh._owner = asyncio.create_task(rh._own_session(
                lambda: TaskBound(log, "transport", ("r", "w", None)),
                lambda r, w: TaskBound(log, "session", FakeSession(log)), ready))
            await ready
        await asyncio.create_task(opener())
        assert rh.tools == {"get_accounts": {}}
        await rh.close(timeout=2)     # ...and shutdown closes it from another task
        return rh._owner
    owner = asyncio.run(main())
    assert owner.done() and not owner.cancelled() and owner.exception() is None
    assert log == ["enter transport", "enter session", "exit session", "exit transport"]


def test_robinhood_connect_failure_reaches_start():
    rh = RobinhoodMCP(CFG)

    class Boom:
        async def __aenter__(self):
            raise ConnectionError("no route")

        async def __aexit__(self, *a):
            return False

    async def main():
        ready = asyncio.get_running_loop().create_future()
        rh._closing = asyncio.Event()
        rh._owner = asyncio.create_task(rh._own_session(lambda: Boom(), None, ready))
        with pytest.raises(ConnectionError):
            await ready
        await rh.close()
    asyncio.run(main())


# --------------------------------------------------------------------------- the real CLI
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_cli_run_exits_on_signal_with_dashboard_open(tmp_path, sig):
    port = free_port()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["server"]["port"] = port
    (tmp_path / "cfg.yaml").write_text(yaml.safe_dump(cfg))
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["HOME"] = str(tmp_path)                         # keep ~/.agentdesk out of the real home
    p = subprocess.Popen([sys.executable, "-m", "agentdesk", "--config", str(tmp_path / "cfg.yaml"), "run",
                          "--mode", "sim", "--speed", "1", "--no-browser"],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        async def connect_then_signal():
            await wait_listening(port, 20)
            got = asyncio.Event()
            client = asyncio.create_task(hold_socket(port, got))
            await asyncio.wait_for(got.wait(), 10)
            p.send_signal(sig)
            t0 = time.monotonic()
            while p.poll() is None and time.monotonic() - t0 < 10:
                await asyncio.sleep(0.05)
            client.cancel()
            return time.monotonic() - t0
        took = asyncio.run(connect_then_signal())
        assert p.poll() is not None, "still running 10s after the signal"
        out = p.stdout.read()
        assert p.returncode == 0, out[-2000:]
        assert "Traceback" not in out, out[-2000:]
        assert took < 8
    finally:
        if p.poll() is None:
            p.kill()
