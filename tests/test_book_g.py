"""Book G: research candidate F3, the 10:00 ET SPY 0DTE/1DTE ATM call calendar (research/strategies_new_prereg.md
F3, rules frozen; Evan approved the paper book 2026-09-28)."""
import asyncio
from datetime import date

import pytest

from books_fakes import FakeEngine, FakeQuotes, ct_ts
from agentdesk.books.base import MarketContext
from agentdesk.books.book import Book
from agentdesk.books.call_calendar import CallCalendar
from agentdesk.books.host import BookHost, build_books
from agentdesk.config import load_config

C = load_config()["books"]["G_call_calendar"]
MON, THU, FRI = date(2026, 9, 28), date(2026, 10, 1), date(2026, 10, 2)
TODAY, TOMORROW = "2026-09-28", "2026-09-29"


def ctx(now, day=MON, spot=765.3):
    return MarketContext(now=now, day=day, spot=spot, vwap=spot)


def test_config_is_paper_only_one_lot_with_the_frozen_rules():
    assert C["enabled"] is True and C["paper_only"] is True and C["lots"] == 1
    assert (C["entry_ct"], C["close_ct"], C["take_profit_pct"], C["stop_pct"], C["max_debit"]) == \
        ("09:00", "14:25", 0.25, 0.35, 3.00)


def test_entry_at_0900_sells_todays_atm_call_and_buys_tomorrows():
    s = CallCalendar(C)
    assert s.on_clock(ct_ts(8, 59), ctx(ct_ts(8, 59))) is None
    it = s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0)))
    assert [(l.right, l.strike, l.side, l.dte) for l in it.legs] == [("call", 765, "sell", 0), ("call", 765, "buy", 1)]
    assert it.credit is False and it.lots == 1 and it.max_price == 3.00
    assert s.on_clock(ct_ts(9, 1), ctx(ct_ts(9, 1))) is None           # once a day


def test_strike_is_spy_rounded():
    s = CallCalendar(C)
    assert s.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0), spot=765.6)).legs[0].strike == 766


def test_friday_is_skipped():
    s = CallCalendar(C)
    now = ct_ts(9, 0, day=FRI)
    assert "Monday to Thursday" in s.on_clock(now, ctx(now, day=FRI)).reason


def test_thursday_trades():
    s = CallCalendar(C)
    now = ct_ts(9, 0, day=THU)
    assert s.on_clock(now, ctx(now, day=THU)).legs[1].dte == 1


def test_day_before_a_holiday_is_skipped():
    s = CallCalendar(C)
    s.holidays = {date(2026, 11, 26)}
    wed = date(2026, 11, 25)
    now = ct_ts(9, 0, day=wed)
    assert "holiday" in s.on_clock(now, ctx(now, day=wed)).reason


def test_late_start_skips_and_a_failed_entry_retries_inside_the_grace_window():
    s = CallCalendar(C)
    assert "missed" in s.on_clock(ct_ts(9, 6), ctx(ct_ts(9, 6))).reason
    s2 = CallCalendar(C)
    assert s2.on_clock(ct_ts(9, 0), ctx(ct_ts(9, 0)))
    s2.entry_failed(ct_ts(9, 0))
    assert s2.on_clock(ct_ts(9, 1), ctx(ct_ts(9, 1)))


class Pos:
    entry = 1.70


class CQ:
    def __init__(self, v):
        self.v = v

    def mid(self, credit):
        assert credit is False
        return self.v


def test_exits_take_profit_25_stop_35_close_1425():
    s, now = CallCalendar(C), ct_ts(10, 0)
    assert s.on_quote(Pos(), CQ(2.12), now, ctx(now)) is None
    assert "take profit" in s.on_quote(Pos(), CQ(2.13), now, ctx(now)).reason
    assert s.on_quote(Pos(), CQ(1.11), now, ctx(now)) is None
    st = s.on_quote(Pos(), CQ(1.10), now, ctx(now))
    assert "stop" in st.reason and st.urgent
    assert "14:25" in s.on_quote(Pos(), CQ(1.70), ct_ts(14, 25), ctx(ct_ts(14, 25))).reason


# ---------------------------------------------------------------- through the host

def host_at(rows, cfg=None):
    fq = FakeQuotes(now=ct_ts(9, 0))
    for exp, (b, a) in zip((TODAY, TOMORROW), rows):
        fq.set("call", 765, b, a, expiry=exp)
    eng = FakeEngine(fq)
    eng.price = 765.3
    book = Book("G_call_calendar", cfg or C, CallCalendar(cfg or C))
    return eng, fq, BookHost(eng, eng.cfg, books=[book])


def tick(host, fq, now):
    fq.now = now
    asyncio.run(host.on_second(now))


def test_host_buys_the_calendar_with_the_long_leg_in_tomorrows_expiry():
    eng, fq, host = host_at([(1.50, 1.52), (3.20, 3.24)])
    tick(host, fq, ct_ts(9, 0))
    (pos,) = host.positions()
    assert [c.expiry for c in pos.contracts] == [TODAY, TOMORROW]
    assert pos.label == "SPY -765C/+765C 09-28/09-29"
    assert pos.credit is False and pos.qty == 1
    assert pos.entry == pytest.approx(1.73)          # mid 1.72 + 1c per leg, never worse than natural 1.74
    assert pos.max_loss == pytest.approx(173.0) and pos.fees == pytest.approx(0.08)
    assert pos.target == pytest.approx(2.16) and pos.stop == pytest.approx(1.12)
    assert "take profit" in pos.meta["plan"]


def test_host_skips_a_debit_above_three_dollars():
    eng, fq, host = host_at([(1.00, 1.02), (4.10, 4.14)])
    tick(host, fq, ct_ts(9, 0))
    assert not host.positions()
    assert "3.00" in eng.bus.of("book_skip")[0]["why"]


def test_host_takes_profit_and_journals_book_g():
    eng, fq, host = host_at([(1.50, 1.52), (3.20, 3.24)])
    tick(host, fq, ct_ts(9, 0))
    fq.set("call", 765, 0.60, 0.62, expiry=TODAY)
    fq.set("call", 765, 2.90, 2.94, expiry=TOMORROW)        # calendar mid 2.31 >= 1.25 x 1.73
    tick(host, fq, ct_ts(10, 0))
    assert not host.positions()
    row = eng.journal.trades()[0]
    assert row["book"] == "G" and "take profit" in row["exit_reason"]


def test_build_books_passes_the_holiday_calendar():
    cfg = load_config()
    cfg["calendar"]["holidays"] = ["2026-11-26"]
    (g,) = [b for b in build_books(cfg) if b.key == "G_call_calendar"]
    assert date(2026, 11, 26) in g.strategy.holidays


def test_sim_quotes_price_a_later_expiry_higher():
    from agentdesk.exits import Contract
    from agentdesk.feeds.sim import SimFeed, SimQuotes
    f = SimFeed(day=MON, seed=1)
    f.t = ct_ts(9, 0)
    q = SimQuotes(f)
    k = float(round(f.px))
    a = asyncio.run(q.quote(Contract("SPY", TODAY, k, "call")))
    b = asyncio.run(q.quote(Contract("SPY", TOMORROW, k, "call")))
    assert b.bid > a.ask
