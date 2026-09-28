import asyncio

import pytest

from books_fakes import DAY, FakeEngine, FakeQuotes, ct_ts
from agentdesk.books.base import MarketContext
from agentdesk.books.book import Book
from agentdesk.books.combo import ComboQuote
from agentdesk.books.host import BookHost
from agentdesk.books.iron_fly import IronFly
from agentdesk.config import load_config

C = load_config()["books"]["B_iron_fly"]


def ctx(now, spot=765.3, events=(), vix1d=None):
    return MarketContext(now=now, day=DAY, spot=spot, vwap=765.0, events=list(events), vix1d_flag=vix1d)


def test_enters_at_0845_atm_with_5_wings_once():
    s = IronFly(C)
    assert s.on_clock(ct_ts(8, 44, 59), ctx(ct_ts(8, 44, 59))) is None
    it = s.on_clock(ct_ts(8, 45), ctx(ct_ts(8, 45)))
    assert [(l.right, l.strike, l.side) for l in it.legs] == [("call", 765, "sell"), ("put", 765, "sell"),
                                                              ("call", 770, "buy"), ("put", 760, "buy")]
    assert it.credit and it.width == 5 and it.lots == 1
    assert "VIX1D unavailable" in " ".join(it.meta["notes"])
    assert s.on_clock(ct_ts(8, 45, 1), ctx(ct_ts(8, 45, 1))) is None


def test_skips_event_before_1400_and_vix1d_flag():
    s = IronFly(C)
    out = s.on_clock(ct_ts(8, 45), ctx(ct_ts(8, 45), events=[(ct_ts(13, 0), "FOMC")]))
    assert "FOMC" in out.reason
    s.new_day(DAY)
    assert s.on_clock(ct_ts(8, 45), ctx(ct_ts(8, 45), events=[(ct_ts(14, 30), "late speaker")])).legs
    s.new_day(DAY)
    assert "VIX1D" in s.on_clock(ct_ts(8, 45), ctx(ct_ts(8, 45), vix1d=True)).reason


def test_restart_after_grace_skips_the_day():                     # review focus 2
    s = IronFly(C)
    assert "missed" in s.on_clock(ct_ts(10, 15), ctx(ct_ts(10, 15))).reason
    assert s.on_clock(ct_ts(10, 16), ctx(ct_ts(10, 16))) is None


def test_transient_failure_retries_inside_grace_only():            # review focus 3
    s = IronFly(C)
    assert s.on_clock(ct_ts(8, 45), ctx(ct_ts(8, 45))).legs
    s.entry_failed(ct_ts(8, 45))
    assert s.on_clock(ct_ts(8, 45, 1), ctx(ct_ts(8, 45, 1))).legs
    s.entry_failed(ct_ts(8, 45, 1))
    assert "missed" in s.on_clock(ct_ts(8, 51), ctx(ct_ts(8, 51))).reason


def test_exits_take_profit_stop_and_1430():
    s = IronFly(C)

    class P:
        entry = 3.00
    q = lambda mid: type("CQ", (), {"mid": lambda self, credit: mid})()
    assert s.on_quote(P, q(1.50), ct_ts(10, 0), None).reason.startswith("take profit")
    assert s.on_quote(P, q(6.00), ct_ts(10, 0), None).urgent
    assert "14:30" in s.on_quote(P, q(2.50), ct_ts(14, 30), None).reason
    assert s.on_quote(P, q(2.50), ct_ts(14, 29), None) is None


def test_b_trades_one_fly_through_the_host_and_no_duplicate():
    fq = FakeQuotes(now=ct_ts(8, 45))
    for right, k, b, a in (("call", 765, 2.00, 2.02), ("put", 765, 1.90, 1.92), ("call", 770, 0.40, 0.41), ("put", 760, 0.35, 0.36)):
        fq.set(right, k, b, a)
    eng = FakeEngine(fq)
    eng.price = 765.3
    host = BookHost(eng, eng.cfg, books=[Book("B_iron_fly", C, IronFly(C))])
    for s in range(0, 3):
        fq.now = ct_ts(8, 45, s)
        asyncio.run(host.on_second(ct_ts(8, 45, s)))
    assert len(host.positions()) == 1 and host.books[0].trades == 1
    assert host.positions()[0].stop == pytest.approx(6.26) and host.positions()[0].target == pytest.approx(1.56)
