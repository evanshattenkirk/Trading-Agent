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


# --------------------------------------------------------------------------- websocket
class Closed(Exception):
    """Stands in for websockets.ConnectionClosedError."""


class FakeWS:
    def __init__(self, script):
        self.script, self.sent = list(script), []

    async def recv(self):
        if not self.script:
            raise Closed("no more messages")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    async def send(self, s):
        self.sent.append(json.loads(s))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.script:
            raise StopAsyncIteration          # the server closed the socket cleanly
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)


class Connects:
    """websockets.connect stand-in: hands out the scripted sockets in order, then stops the loop."""

    def __init__(self, *sockets):
        self.sockets, self.kw, self.used = list(sockets), [], []

    def __call__(self, url, **kw):
        self.kw.append(kw)
        if not self.sockets:
            raise asyncio.CancelledError
        ws = self.sockets.pop(0)
        self.used.append(ws)
        outer = self

        class Ctx:
            async def __aenter__(self):
                return ws

            async def __aexit__(self, *a):
                return False
        return Ctx()


HELLO = [{"T": "success", "msg": "connected"}]
AUTH_OK = [{"T": "success", "msg": "authenticated"}]
SUB_OK = [{"T": "subscription", "trades": ["SPY"], "quotes": [], "bars": []}]


def trade(px, ts="2026-09-28T14:00:00.5Z"):
    return [{"T": "t", "S": "SPY", "p": px, "s": 100, "t": ts, "c": ["@"]}]


def run_ws(monkeypatch, *sockets):
    import websockets
    conn = Connects(*sockets)
    monkeypatch.setattr(websockets, "connect", conn)
    sleeps = []
    real = asyncio.sleep

    async def no_wait(s, *a, **k):
        sleeps.append(s)
        await real(0)
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    f = feed("iex")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(f._ws())
    got = []
    while not f.q.empty():
        got.append(f.q.get_nowait())
    return f, conn, sleeps, got


def test_a_subscribe_error_is_a_drop_and_the_feed_reconnects(monkeypatch, caplog):
    bad = FakeWS([HELLO, AUTH_OK, [{"T": "error", "code": 405, "msg": "symbol limit exceeded"}], trade(660.0)])
    good = FakeWS([HELLO, AUTH_OK, SUB_OK, trade(660.0), trade(660.1)])
    f, conn, sleeps, got = run_ws(monkeypatch, bad, good)
    assert len(conn.kw) == 3                                   # bad, good, then the stop
    assert [t.px for t in got] == [660.0, 660.1]               # nothing from the socket that refused the subscription
    assert bad.sent[-1] == {"action": "subscribe", "trades": ["SPY"]}
    assert "405" in caplog.text and "symbol limit exceeded" in caplog.text


def test_an_error_message_mid_stream_drops_the_socket(monkeypatch, caplog):
    ws = FakeWS([HELLO, AUTH_OK, SUB_OK, trade(660.0), [{"T": "error", "code": 406, "msg": "connection limit exceeded"}],
                 trade(999.0)])
    f, conn, sleeps, got = run_ws(monkeypatch, ws)
    assert [t.px for t in got] == [660.0] and ws.script == [trade(999.0)]     # stopped reading at the error
    assert "406" in caplog.text and "connection limit exceeded" in caplog.text
    assert len(conn.kw) == 2


def test_backoff_grows_until_a_connection_delivers_prints(monkeypatch):
    err = [{"T": "error", "code": 406, "msg": "connection limit exceeded"}]
    f, conn, sleeps, got = run_ws(monkeypatch,
                                  FakeWS([HELLO, AUTH_OK, SUB_OK, err]),
                                  FakeWS([HELLO, AUTH_OK, SUB_OK, err]),
                                  FakeWS([HELLO, AUTH_OK, SUB_OK, trade(660.0)]),     # healthy, then closed
                                  FakeWS([HELLO, AUTH_OK, SUB_OK, err]))
    assert sleeps[:4] == [1, 2, 1, 2]       # a clean close after prints resets it; an error right after subscribing doesn't


def test_the_socket_uses_a_30s_pong_timeout(monkeypatch):
    f, conn, sleeps, got = run_ws(monkeypatch, FakeWS([HELLO, AUTH_OK, SUB_OK]))
    assert conn.kw[0]["ping_interval"] == 15 and conn.kw[0]["ping_timeout"] == 30


def test_a_prints_stall_in_regular_hours_warns_once(caplog):
    f = feed("iex")
    t0 = at_ct(MON, time(10, 0))
    f.last_print = t0
    with caplog.at_level(logging.INFO, logger="agentdesk.alpaca"):
        f._check_stall(t0 + 30)
        assert "no prints" not in caplog.text
        f._check_stall(t0 + 61)
        f._check_stall(t0 + 90)
        assert caplog.text.count("no prints") == 1
        asyncio.run(f._handle(json.dumps(trade(660.0, "2026-09-28T15:01:35Z"))))
        assert "resumed" in caplog.text
        caplog.clear()
        f._check_stall(at_ct(MON, time(15, 30)))                                  # after the close: quiet is normal
        f._check_stall(at_ct(MON, time(8, 30, 30)))                               # right after the open: counts from 08:30
        assert "no prints" not in caplog.text
        f.last_print = at_ct(date(2026, 11, 27), time(11, 0))
        f._check_stall(at_ct(date(2026, 11, 27), time(13, 0)))                    # half-day afternoon
        assert "no prints" not in caplog.text


def test_the_heartbeat_checks_for_a_stall(monkeypatch):
    f = feed("iex")
    seen = []
    monkeypatch.setattr(f, "_check_stall", seen.append)

    async def go():
        t = asyncio.create_task(f._beat())
        await asyncio.sleep(0.01)
        t.cancel()
    asyncio.run(go())
    assert seen and isinstance(f.q.get_nowait(), Heartbeat)
