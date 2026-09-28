"""Run the dashboard server and the engine together, and shut both down cleanly on Ctrl-C or SIGTERM.

Shutdown never flattens or sends orders. It stops the engine loop, closes the dashboard (open websockets
included), closes the broker/data sessions with a timeout each, and exits. A second Ctrl-C or SIGTERM, or
shutdown taking longer than `hard_exit_sec`, exits the process immediately.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import threading

import uvicorn

log = logging.getLogger("agentdesk.lifecycle")
SIGNALS = (signal.SIGINT, signal.SIGTERM)


class Server(uvicorn.Server):
    """uvicorn without its own signal handling (it swallows SIGTERM while a websocket is open)."""

    @contextlib.contextmanager
    def capture_signals(self):              # uvicorn >= 0.29
        yield

    def install_signal_handlers(self) -> None:     # older uvicorn
        pass


def _hard_exit(code: int, why: str) -> None:
    log.error("%s; exiting now", why)
    logging.shutdown()
    os._exit(code)


async def serve(engine, server, closers=(), stop: asyncio.Event | None = None, grace: float = 3.0,
                close_timeout: float = 3.0, hard_exit_sec: float | None = 15.0) -> threading.Timer | None:
    """Serve until `stop` is set (by SIGINT/SIGTERM when handlers can be installed), then shut down.
    Returns the armed hard-exit timer so the caller can cancel it once asyncio.run() has returned."""
    loop = asyncio.get_running_loop()
    stop = stop or asyncio.Event()
    installed = []

    def on_signal(sig) -> None:
        if stop.is_set():
            _hard_exit(130, f"second {signal.Signals(sig).name}")
        log.warning("%s received: shutting down (no orders are sent)", signal.Signals(sig).name)
        stop.set()

    if threading.current_thread() is threading.main_thread():
        for sig in SIGNALS:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, on_signal, sig)
                installed.append(sig)

    before = asyncio.all_tasks()
    srv = asyncio.create_task(server.serve(), name="dashboard")
    eng = asyncio.create_task(engine.run(), name="engine")
    eng.add_done_callback(_report_engine_exit)
    waiter = asyncio.create_task(stop.wait())
    timer = None
    try:
        await asyncio.wait({srv, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
        if hard_exit_sec:
            timer = threading.Timer(hard_exit_sec, _hard_exit, (1, f"shutdown took longer than {hard_exit_sec:g}s"))
            timer.daemon = True
            timer.start()
        await shutdown(engine, server, srv, eng, closers, grace, close_timeout, before)
        for sig in installed:
            loop.remove_signal_handler(sig)
    return timer


def _report_engine_exit(t: asyncio.Task) -> None:
    if not t.cancelled() and t.exception() is not None:
        log.error("engine stopped with an error", exc_info=t.exception())


async def shutdown(engine, server, srv: asyncio.Task, eng: asyncio.Task, closers, grace: float,
                   close_timeout: float, keep=frozenset()) -> None:
    _warn_open_positions(engine)
    engine.stop()
    server.should_exit = True
    eng.cancel()
    await asyncio.wait({srv, eng}, timeout=grace)
    if not srv.done():                     # connections or handlers still open: stop waiting for them
        server.force_exit = True
        await asyncio.wait({srv}, timeout=1.0)
        srv.cancel()
    for close in closers:
        try:
            await asyncio.wait_for(close(), close_timeout)
        except asyncio.TimeoutError:
            log.warning("%s did not finish within %gs; skipped", _name(close), close_timeout)
        except Exception as ex:
            log.warning("%s failed: %r", _name(close), ex)
    # background pollers (L2 book, quote recorder, crew) the engine started
    rest = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and t not in keep and not t.done()]
    for t in rest:
        t.cancel()
    if rest:
        await asyncio.wait(rest, timeout=close_timeout)
    log.info("shutdown complete")


def _warn_open_positions(engine) -> None:
    held = [p for p, _ in getattr(engine, "open", [])]
    if not held:
        return
    names = ", ".join(f"{p.qty}x {p.contract.label}" for p in held)
    if getattr(engine.broker, "live", False):
        log.error("Shutting down with LIVE positions open and no engine stop: %s. Manage them in the Robinhood app.", names)
    else:
        log.warning("Shutting down with open %s positions (not journaled, nothing sent): %s", engine.mode, names)


def _name(fn) -> str:
    owner = getattr(fn, "__self__", None)
    return f"{type(owner).__name__}.{fn.__name__}" if owner is not None else getattr(fn, "__name__", "close")
