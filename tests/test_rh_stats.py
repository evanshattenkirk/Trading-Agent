"""The engine's Robinhood client counts its calls for the Manager panel (2026-10-27): when the last good call was, and
rate limits, timeouts, refused sign-ins, other errors and reconnects over the last 15 minutes and the day. Counting
only: nothing here changes how a call is made, retried or halted on."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.brokers.base import RateLimited
from agentdesk.brokers.robinhood import RobinhoodMCP

from test_rh_session import Session, connected


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    real = asyncio.sleep

    async def no_wait(_s, *a, **k):
        await real(0)
    monkeypatch.setattr(asyncio, "sleep", no_wait)


class Limited(Session):
    async def call_tool(self, tool, args):
        return SimpleNamespace(isError=True, structuredContent=None,
                               content=[SimpleNamespace(text="RATE_LIMITED: too many requests")])


class Broken(Session):
    async def call_tool(self, tool, args):
        return SimpleNamespace(isError=True, structuredContent=None, content=[SimpleNamespace(text="bad args")])


def test_a_fresh_client_has_no_calls():
    s = connected("ok").stats()
    assert s["last_ok"] is None and s["last_err"] is None
    assert s["window"]["ok"] == 0 and s["day"]["reconnects"] == 0


def test_good_calls_count_and_stamp_the_last_good_call():
    rh = connected("ok")

    async def go():
        await rh._open()
        for _ in range(3):
            await rh.call("get_accounts", {})
        await rh.close()
    asyncio.run(go())
    s = rh.stats()
    assert s["window"]["ok"] == 3 and s["day"]["ok"] == 3
    assert s["last_ok"] is not None


def test_rate_limits_count():
    rh = connected("ok")
    rh._connect = (rh._connect[0], lambda r, w: Limited("ok"))

    async def go():
        await rh._open()
        with pytest.raises(RateLimited):
            await rh.call("get_option_quotes", {})
        await rh.close()
    asyncio.run(go())
    s = rh.stats()
    assert s["window"]["rate_limited"] == 1 and s["window"]["ok"] == 0
    assert s["last_err"]["kind"] == "rate_limited"


def test_timeouts_and_reconnects_count():
    rh = connected("hang", "hang", "ok")
    rh.call_timeout = 0.05

    async def go():
        await rh._open()
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await rh.call("get_option_quotes", {})
        await rh.call("get_option_quotes", {})
        await rh.close()
    asyncio.run(go())
    s = rh.stats()
    assert s["window"]["timeout"] == 2 and s["window"]["ok"] == 1
    assert s["day"]["reconnects"] >= 1          # a call that times out twice (the read retry) reconnects


def test_other_errors_count():
    rh = connected("ok")
    rh._connect = (rh._connect[0], lambda r, w: Broken("ok"))

    async def go():
        await rh._open()
        with pytest.raises(RuntimeError):
            await rh.call("get_accounts", {})
        await rh.close()
    asyncio.run(go())
    assert rh.stats()["window"]["error"] == 1


def test_a_refused_sign_in_counts_as_auth(tmp_path):
    rh = connected("ok")
    rh.cfg = {**rh.cfg, "token_dir": str(tmp_path)}
    (tmp_path / "rh_oauth.json").write_text("{}")
    rh._mark_signed_out()
    try:
        with pytest.raises(Exception):
            asyncio.run(rh.call("get_accounts", {}))
        assert rh.stats()["window"]["auth"] == 1
    finally:
        RobinhoodMCP._refused.pop(str(tmp_path / "rh_oauth.json"), None)


def test_calls_older_than_fifteen_minutes_leave_the_window_but_stay_in_the_day(monkeypatch):
    rh = connected("ok")
    t = [1_800_000_000.0]
    monkeypatch.setattr("agentdesk.brokers.robinhood.time.time", lambda: t[0])

    async def go():
        await rh._open()
        await rh.call("get_accounts", {})
        await rh.close()
    asyncio.run(go())
    t[0] += 16 * 60
    s = rh.stats()
    assert s["window"]["ok"] == 0 and s["day"]["ok"] == 1


def test_the_day_counts_reset_on_a_new_session_day(monkeypatch):
    rh = connected("ok")
    t = [1_800_000_000.0]
    monkeypatch.setattr("agentdesk.brokers.robinhood.time.time", lambda: t[0])

    async def go():
        await rh._open()
        await rh.call("get_accounts", {})
        await rh.close()
    asyncio.run(go())
    t[0] += 86400
    assert rh.stats()["day"]["ok"] == 0
