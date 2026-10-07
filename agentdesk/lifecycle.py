"""Run the dashboard server and the engine together, and shut both down cleanly on Ctrl-C or SIGTERM.

Shutdown sells what is open first (Evan, 2026-09-28: "sell them, dont leave them open"): book A and every paper
book are flattened through engine.flatten("shutdown") while the engine, quotes and broker are still running, with
`flatten_timeout` to finish. Then it stops the engine loop, closes the dashboard (open websockets included), cancels the background pollers (book hooks, L2,
crew) so none is mid-call when its session goes, closes the broker/data sessions with a timeout each, and exits.
Whatever is still open after the timeout is logged. A
second Ctrl-C or SIGTERM, or shutdown taking longer than `hard_exit_sec`, exits the process immediately.
If the engine task dies with an error, the same shutdown runs and serve() raises EngineCrashed, so `run` exits with
status 1 instead of leaving a dashboard up with nothing trading; the after-hours review page then takes the port.
Book E's paper positions are kept overnight on purpose (saved in e_positions; spec E-Q1).
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


class EngineCrashed(RuntimeError):
    """The engine task stopped with an error; raised by serve() after the normal shutdown ran."""


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
                close_timeout: float = 3.0, hard_exit_sec: float | None = 25.0,
                flatten_timeout: float = 8.0) -> threading.Timer | None:
    """Serve until `stop` is set (by SIGINT/SIGTERM when handlers can be installed), then shut down.
    Returns the armed hard-exit timer so the caller can cancel it once asyncio.run() has returned."""
    loop = asyncio.get_running_loop()
    stop = stop or asyncio.Event()
    installed = []

    def on_signal(sig) -> None:
        if stop.is_set():
            _hard_exit(130, f"second {signal.Signals(sig).name}")
        log.warning("%s received: selling open positions, then shutting down (again to exit now)", signal.Signals(sig).name)
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
    timer, crash = None, None
    try:
        await asyncio.wait({srv, waiter, eng}, return_when=asyncio.FIRST_COMPLETED)
        crash = _crash(eng)
        if crash is None and eng.done() and not (srv.done() or waiter.done()):
            await asyncio.wait({srv, waiter}, return_when=asyncio.FIRST_COMPLETED)   # a finished day keeps the page
    finally:
        waiter.cancel()
        if hard_exit_sec:
            timer = threading.Timer(hard_exit_sec, _hard_exit, (1, f"shutdown took longer than {hard_exit_sec:g}s"))
            timer.daemon = True
            timer.start()
        await shutdown(engine, server, srv, eng, closers, grace, close_timeout, before, flatten_timeout)
        for sig in installed:
            loop.remove_signal_handler(sig)
    if crash is not None:
        if timer:
            timer.cancel()
        raise EngineCrashed(f"engine stopped with an error: {crash!r}") from crash
    return timer


def _crash(t: asyncio.Task) -> BaseException | None:
    return t.exception() if t.done() and not t.cancelled() else None


def _report_engine_exit(t: asyncio.Task) -> None:
    if not t.cancelled() and t.exception() is not None:
        log.error("engine stopped with an error", exc_info=t.exception())


async def shutdown(engine, server, srv: asyncio.Task, eng: asyncio.Task, closers, grace: float,
                   close_timeout: float, keep=frozenset(), flatten_timeout: float = 8.0) -> None:
    await _sell_open_positions(engine, flatten_timeout)
    engine.stop()
    server.should_exit = True
    eng.cancel()
    await asyncio.wait({srv, eng}, timeout=grace)
    if not srv.done():                     # connections or handlers still open: stop waiting for them
        server.force_exit = True
        await asyncio.wait({srv}, timeout=1.0)
        srv.cancel()
    # background pollers the engine started (book hooks, _guard tasks, L2 book, crew) stop BEFORE the sessions they
    # call close under them (a call cut off by the close was the ClosedResourceError traceback of 2026-10-05). The
    # sessions' own tasks are left to their close().
    await _cancel_rest(set(keep) | _owned(closers), close_timeout, spare_transports=True)
    for close in closers:
        try:
            await asyncio.wait_for(close(), close_timeout)
        except asyncio.TimeoutError:
            log.warning("%s did not finish within %gs; skipped", _name(close), close_timeout)
        except Exception as ex:
            log.warning("%s failed: %r", _name(close), ex)
    await _cancel_rest(keep, 1.0)          # whatever the closers left behind
    log.info("shutdown complete")


TRANSPORT_MODULES = ("mcp.", "anyio.", "httpx", "httpcore")     # MCP/HTTP clients' own task-group tasks


def _transport_task(t: asyncio.Task) -> bool:
    """A task inside an MCP/HTTP client's own task group (anyio names those by module.function): the session's
    owner winds it down when the session closes, so shutdown must not cancel it from outside first."""
    frame = getattr(t.get_coro(), "cr_frame", None)
    mod = frame.f_globals.get("__name__", "") if frame is not None else ""
    return mod.startswith(TRANSPORT_MODULES) or t.get_name().startswith(TRANSPORT_MODULES)


def _owned(closers) -> set:
    out = set()
    for close in closers:
        owned = getattr(getattr(close, "__self__", None), "owned_tasks", None)
        if owned is not None:
            out.update(owned())
    return out


async def _cancel_rest(keep, timeout: float, spare_transports: bool = False) -> None:
    rest = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and t not in keep and not t.done()
            and not (spare_transports and _transport_task(t))]
    for t in rest:
        t.cancel()
    if rest:
        await asyncio.wait(rest, timeout=timeout)


def _held(engine) -> list[str]:
    a = [f"{p.qty}x {p.contract.label}" for p, _ in getattr(engine, "open", None) or []]
    books = getattr(engine, "books", None)
    return a + [getattr(p, "label", str(p)) for p in (books.positions() if books else [])
                if not getattr(p, "overnight", False)]


async def _sell_open_positions(engine, timeout: float) -> None:
    held = _held(engine)
    if not held:
        return
    log.warning("Shutdown: selling %d open position(s): %s", len(held), ", ".join(held))
    try:
        await asyncio.wait_for(engine.flatten("shutdown"), timeout)
    except asyncio.TimeoutError:
        log.error("Shutdown flatten did not finish within %gs", timeout)
    except Exception:
        log.exception("Shutdown flatten failed")
    left = _held(engine)
    if left:
        where = "Close them in the Robinhood app" if getattr(getattr(engine, "broker", None), "live", False) \
            else "paper, not journaled"
        log.error("Shutting down with positions still open: %s. %s.", ", ".join(left), where)


def _name(fn) -> str:
    owner = getattr(fn, "__self__", None)
    return f"{type(owner).__name__}.{fn.__name__}" if owner is not None else getattr(fn, "__name__", "close")
