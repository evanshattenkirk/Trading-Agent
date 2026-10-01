"""Robinhood MCP calls time out instead of hanging the engine, and a dropped session reconnects
(Evan, 2026-10-01, sweep item 8)."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.brokers.robinhood import RobinhoodMCP
from agentdesk.config import load_config

CFG = load_config()


class Transport:
    async def __aenter__(self):
        return ("r", "w", None)

    async def __aexit__(self, *a):
        return False


class Session:
    """behaviour: 'ok', 'hang' or 'dead' (the connection is gone: every call raises)."""
    made: list["Session"] = []

    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, 0
        Session.made.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def initialize(self):
        pass

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name="get_accounts", inputSchema={}),
                                      SimpleNamespace(name="get_option_quotes", inputSchema={})])

    async def call_tool(self, tool, args):
        self.calls += 1
        if self.behaviour == "hang":
            await asyncio.Event().wait()
        if self.behaviour == "dead":
            raise anyio.ClosedResourceError()
        return SimpleNamespace(isError=False, structuredContent={"data": {"ok": len(Session.made)}}, content=[])


def connected(*behaviours):
    """An RobinhoodMCP whose successive connections get sessions with these behaviours."""
    Session.made = []
    rh = RobinhoodMCP({**CFG, "robinhood": {**CFG["robinhood"], "call_budget": None}})
    rh.RECONNECT_GAP_S = 0
    seq = list(behaviours)
    rh._connect = (lambda: Transport(), lambda r, w: Session(seq.pop(0) if len(seq) > 1 else seq[0]))
    return rh


def test_a_hanging_call_times_out_and_frees_the_session():
    rh = connected("hang")
    rh.call_timeout = 0.05

    async def go():
        await rh._open()
        with pytest.raises(TimeoutError):
            await rh.call("get_option_quotes", {})
        assert not rh._lock.locked()
        await rh.close()
    asyncio.run(go())


def test_a_dead_session_reconnects_and_the_read_is_retried():
    rh = connected("dead", "ok")

    async def go():
        await rh._open()
        got = await rh.call("get_option_quotes", {})
        await rh.close()
        return got
    assert asyncio.run(go()) == {"ok": 2}
    assert [s.behaviour for s in Session.made] == ["dead", "ok"]


def test_a_session_that_ended_reconnects_on_the_next_call():
    rh = connected("ok")

    async def go():
        await rh._open()
        rh._closing.set()                     # the connection drops: the owner task ends
        await asyncio.wait({rh._owner}, timeout=1)
        got = await rh.call("get_accounts", {})
        await rh.close()
        return got
    assert asyncio.run(go()) == {"ok": 2}


def test_repeated_timeouts_reconnect():
    rh = connected("hang", "hang", "ok")
    rh.call_timeout = 0.05

    async def go():
        await rh._open()
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await rh.call("get_option_quotes", {})
        got = await rh.call("get_option_quotes", {})
        await rh.close()
        return got
    assert asyncio.run(go())["ok"] >= 2


def test_a_closed_client_does_not_reconnect():
    rh = connected("ok")

    async def go():
        await rh._open()
        await rh.close()
        with pytest.raises(Exception):
            await rh.call("get_accounts", {})
    asyncio.run(go())
    assert len(Session.made) == 1
