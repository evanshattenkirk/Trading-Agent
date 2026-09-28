import asyncio
from datetime import date, timedelta

import pytest

from books_fakes import DAY, ct_ts
from agentdesk.bars import Bar
from agentdesk.books.base import MarketContext
from agentdesk.books.iron_condor import IronCondor, expected_move
from agentdesk.books.vol import RobinhoodVix, SimVix
from agentdesk.config import load_config

C = load_config()["books"]["D_iron_condor"]


def ctx(now, spot=765.0, vwap=765.2, vix=15.0, day=DAY):
    return MarketContext(now=now, day=day, spot=spot, vwap=vwap, vix_prev=vix)


def opening_range(s, day, rng, close=765.0):
    """30 one-minute bars 08:30-08:59 CT whose high-low range is rng dollars, last close = close."""
    for i in range(30):
        t = ct_ts(8, 30 + i, day=day)
        s.on_bar("1m", Bar("1m", t, close, close + (rng if i == 10 else 0), close - (0 if i != 20 else 0), close, 100, 1, t + 60), None)


def warm(s, n=14, rng=4.0):
    d, days = DAY, []
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    for d in reversed(days):
        opening_range(s, d, rng)


def test_expected_move_matches_backtest_formula():
    assert expected_move(765.0, 15.0, ct_ts(9, 0)) == pytest.approx(5.6705, abs=1e-3)


def test_strikes_at_09_quiet_day():
    s = IronCondor(C)
    warm(s)
    opening_range(s, DAY, 2.0)                                   # 2/765 < 4/765 median: quiet
    it = s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0)))
    assert [(l.right, l.strike, l.side) for l in it.legs] == [("call", 771, "sell"), ("put", 759, "sell"),
                                                              ("call", 773, "buy"), ("put", 757, "buy")]
    assert it.width == 2 and it.credit


def test_quiet_filter_uses_only_the_first_30_minutes():
    s = IronCondor(C)
    warm(s)
    opening_range(s, DAY, 6.0)                                   # wider than the median: not quiet
    assert "not quiet" in s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0))).reason
    t = ct_ts(9, 30)                                             # later bars must not change anything
    s.on_bar("1m", Bar("1m", t, 765, 766, 764, 765, 100, 1, t + 60), None)
    assert s.days[DAY][0] - s.days[DAY][1] == pytest.approx(6.0)


def test_far_from_vwap_skips():
    s = IronCondor(C)
    warm(s)
    opening_range(s, DAY, 2.0)
    assert "VWAP" in s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0), vwap=763.0)).reason


def test_needs_14_days_and_vix():
    s = IronCondor(C)
    warm(s, n=5)
    opening_range(s, DAY, 2.0)
    assert "14 prior days" in s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0))).reason
    s2 = IronCondor(C)
    warm(s2)
    opening_range(s2, DAY, 2.0)
    assert "VIX" in s2.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0), vix=None)).reason


def test_waits_for_the_0859_bar_then_skips_without_it():            # review focus 2
    s = IronCondor(C)
    warm(s)
    assert s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0))) is None           # no bars yet today: wait
    assert "08:30" in s.on_clock(ct_ts(9, 6), ctx(ct_ts(9, 6))).reason   # grace over


def test_exits():
    s = IronCondor(C)

    class P:
        entry = 0.45
    q = lambda mid: type("CQ", (), {"mid": lambda self, credit: mid})()
    assert s.on_quote(P, q(0.22), ct_ts(10, 0), None).reason.startswith("take profit")
    assert s.on_quote(P, q(0.90), ct_ts(10, 0), None).urgent
    assert "14:25" in s.on_quote(P, q(0.40), ct_ts(14, 25), None).reason


def test_vix_sources():
    assert asyncio.run(SimVix(type("F", (), {"base_iv": 0.16})()).prior_close(DAY)) == 16.0

    class RH:
        async def start(self):
            pass

        async def call(self, tool, args):
            if tool == "get_indexes":
                return {"indexes": [{"id": "vix-id", "symbol": "VIX"}]}
            return {"results": [{"bars": [
                {"begins_at": "2026-09-24T00:00:00Z", "close_value": "15.67"},
                {"begins_at": "2026-09-25T00:00:00Z", "close_value": "14.87"},
                {"begins_at": "2026-09-26T00:00:00Z", "close_value": "14.87", "interpolated": True},
                {"begins_at": "2026-09-28T00:00:00Z", "close_value": "16.10"}]}]}
    assert asyncio.run(RobinhoodVix(RH()).prior_close(date(2026, 9, 28))) == 14.87


def test_partial_opening_window_skips_instead_of_guessing():         # review #4
    s = IronCondor(C)
    warm(s)
    for i in range(15, 30):                                          # restart at 08:45: only 08:45-08:59 bars
        t = ct_ts(8, 30 + i)
        s.on_bar("1m", Bar("1m", t, 765.0, 765.0, 765.0, 765.0, 100, 1, t + 60), None)
    assert s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0))) is None
    assert "30 minutes" in s.on_clock(ct_ts(9, 6), ctx(ct_ts(9, 6))).reason
