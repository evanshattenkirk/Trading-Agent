"""Alpaca SPY feed: history up to now on IEX (the 15-minute embargo is SIP-only), and a websocket that checks the
subscribe reply, treats {"T": "error"} as a drop, backs off, and warns when regular-hours prints stop (M11, L11)."""
import asyncio
import copy
import json
import logging
import sys
import time as _time
from datetime import date, datetime, time, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.feeds import alpaca
from agentdesk.feeds.base import Heartbeat

CFG = load_config()
MON = date(2026, 9, 28)


def feed(kind="iex"):
    cfg = copy.deepcopy(CFG)
    cfg["data"]["alpaca"]["feed"] = kind
    return alpaca.AlpacaFeed(cfg)


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")


# --------------------------------------------------------------------------- history end
def capture_fetch(monkeypatch, fail_first: int | None = None):
    calls = []

    async def fetch(symbol, start, end, kind):
        calls.append((start, end, kind))
        if fail_first and len(calls) == 1:
            req = httpx.Request("GET", "https://data.alpaca.markets/v2/stocks/SPY/bars")
            raise httpx.HTTPStatusError("403", request=req, response=httpx.Response(fail_first, request=req,
                                        text='{"message":"subscription does not permit querying recent SIP data"}'))
        return []
    monkeypatch.setattr(alpaca, "fetch_bars_1m", fetch)
    return calls


def test_iex_history_runs_up_to_now(monkeypatch):
    calls = capture_fetch(monkeypatch)
    f = feed("iex")
    before = datetime.now(timezone.utc)
    asyncio.run(f.history_1m(5))
    assert calls[0][2] == "iex" and (calls[0][1] - before).total_seconds() > -1          # no 16-minute lag
    assert f.history_end == pytest.approx(calls[0][1].timestamp())


def test_sip_history_keeps_the_free_plan_lag(monkeypatch):
    calls = capture_fetch(monkeypatch)
    f = feed("sip")
    asyncio.run(f.history_1m(5))
    lag = (datetime.now(timezone.utc) - calls[0][1]).total_seconds()
    assert 15 * 60 < lag < 17 * 60 and f.history_end == pytest.approx(calls[0][1].timestamp())


def test_an_iex_history_refusal_falls_back_to_the_lagged_end(monkeypatch, caplog):
    calls = capture_fetch(monkeypatch, fail_first=403)
    f = feed("iex")
    asyncio.run(f.history_1m(5))
    assert len(calls) == 2 and (calls[0][1] - calls[1][1]).total_seconds() == pytest.approx(16 * 60, abs=1)
    assert f.history_end == pytest.approx(calls[1][1].timestamp())
    assert "403" in caplog.text
