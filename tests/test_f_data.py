"""Book F data: Robinhood equity parsing, throttling and caching, and the sim session (handoff section 4.2)."""
from __future__ import annotations

import asyncio
import time as _time
from datetime import date, time

import pytest

from f_fakes import CFG
from agentdesk.books import f_stocks_in_play as F

DAY = date(2026, 10, 1)


def run(c):
    return asyncio.run(c)


class RH:
    def __init__(self, fail_once=False):
        self.calls, self.fail_once = [], fail_once

    async def call(self, tool, args):
        self.calls.append((tool, dict(args)))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("blip")
        syms = args.get("symbols", [])
        if tool == "get_equity_historicals":
            if args["interval"] == "day":
                return {"results": [{"symbol": s, "bars": [
                    {"begins_at": "2026-09-29T00:00:00Z", "open_price": "10", "high_price": "11", "low_price": "9",
                     "close_price": "10.5", "volume": 100},
                    {"begins_at": "2026-09-30T00:00:00Z", "open_price": "10", "high_price": "11", "low_price": "9",
                     "close_price": "10.5", "volume": 100, "interpolated": True}]} for s in syms if s != "GONE"]}
            return {"results": [{"symbol": s, "bars": [
                {"begins_at": "2026-10-01T13:30:00Z", "open_price": "1", "high_price": "2", "low_price": "0.5",
                 "close_price": "1.5", "volume": 7},
                {"begins_at": "2026-10-01T13:35:00Z", "open_price": "1", "high_price": "2", "low_price": "0.5",
                 "close_price": "1.5", "volume": 99}]} for s in syms if s != "GONE"]}
        if tool == "get_equity_quotes":
            return {"results": [{"symbol": s, "bid_price": "10.00", "ask_price": "10.02", "last_trade_price": "10.01"}
                                for s in syms if s != "GONE"]}
        if tool == "get_equity_tradability":
            return {"results": [{"symbol": s, "tradable": s != "HALT"} for s in syms]}
        raise AssertionError(tool)


def data(tmp_path, rh=None):
    from agentdesk.feeds.f_data import RobinhoodEquityData
    return RobinhoodEquityData(rh or RH(), {**CFG, "cache_dir": str(tmp_path), "max_calls_per_s": 1000})


def test_minute_bars_are_in_et_minutes_and_windowed(tmp_path):
    d = data(tmp_path)
    got = run(d.minute_bars(["NVDA", "GONE"], DAY, 570, 575))
    assert got == {"NVDA": [{"t": 570, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 7.0}]}    # GONE dropped, not guessed


def test_at_most_ten_symbols_per_call(tmp_path):
    rh = RH()
    d = data(tmp_path, rh)
    run(d.minute_bars([f"S{i}" for i in range(23)], DAY, 570, 575))
    assert [len(a["symbols"]) for _, a in rh.calls] == [10, 10, 3]


def test_a_failed_call_is_retried_once(tmp_path):
    rh = RH(fail_once=True)
    got = run(data(tmp_path, rh).quotes(["NVDA"]))
    assert got["NVDA"].bid == 10.0 and got["NVDA"].last == 10.01 and len(rh.calls) == 2


def test_daily_bars_skip_interpolated_and_are_cached_for_the_day(tmp_path):
    rh = RH()
    d = data(tmp_path, rh)
    got = run(d.daily_bars(["NVDA"], DAY))
    assert [b["d"] for b in got["NVDA"]] == [date(2026, 9, 29)]
    n = len(rh.calls)
    run(d.daily_bars(["NVDA"], DAY))
    assert len(rh.calls) == n


def test_or_volumes_come_from_1_minute_bars_and_are_cached(tmp_path):
    rh = RH()
    d = data(tmp_path, rh)
    got = run(d.or_volumes(["NVDA"], [date(2026, 10, 1)]))
    assert got[date(2026, 10, 1)] == {"NVDA": 7.0}
    n = len(rh.calls)
    run(d.or_volumes(["NVDA"], [date(2026, 10, 1)]))
    assert len(rh.calls) == n


def test_untradable_names_are_dropped(tmp_path):
    assert run(data(tmp_path).tradable(["NVDA", "HALT"])) == {"NVDA"}


def test_throttle_caps_calls_per_second():
    from agentdesk.feeds.f_data import Throttle
    th = Throttle(per_s=5)

    async def burst():
        t0 = _time.monotonic()
        for _ in range(11):
            await th.wait()
        return _time.monotonic() - t0
    assert run(burst()) >= 1.9                       # 11 calls at 5/s need two full waits


class Clock:
    def __init__(self, ts):
        self.t = ts

    def now(self):
        return self.t


def test_sim_session_is_deterministic_and_only_shows_completed_bars():
    from agentdesk.feeds.f_data import SimEquityData
    c = Clock(F.at_et(DAY, time(9, 36, 30)))
    a, b = SimEquityData(c), SimEquityData(c)
    ba = run(a.minute_bars(["NVDA"], DAY, 570, 960))["NVDA"]
    assert ba == run(b.minute_bars(["NVDA"], DAY, 570, 960))["NVDA"]
    assert [x["t"] for x in ba] == list(range(570, 576))          # 09:36 bar still forming
    q = run(a.quotes(["NVDA"]))["NVDA"]
    assert q.bid < q.ask and q.ts == c.t


def test_sim_universe_passes_the_section_3_filters_and_has_in_play_names():
    from agentdesk.feeds.f_data import SimEquityData
    c = Clock(F.at_et(DAY, time(9, 35, 5)))
    s = SimEquityData(c)
    sp = run(s.sp500())
    daily = run(s.daily_bars(sp + list(F.AI_LIST), DAY))
    uni = F.universe(daily, set(sp), CFG)
    assert len(uni) >= 30
    dates = sorted({b["d"] for bs in daily.values() for b in bs})[-14:]
    vols = run(s.or_volumes(sorted(uni), dates))
    bars = run(s.minute_bars(sorted(uni), DAY, 570, 575))
    rows = [F.scan_row(x, uni[x], bars[x], [vols[d][x] for d in dates]) for x in uni]
    res = F.rank_candidates([r for r in rows if r], CFG)
    assert res.picks or res.shorts
