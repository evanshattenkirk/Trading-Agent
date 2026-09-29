"""A heartbeat flush closes a 1m bar on the clock. A print for that minute arriving afterwards (IEX prints can land
a second or two late) used to open a second bar for the same minute, so book A's MACD and RSI took that minute's close
twice (found 2026-09-29). A closed period is final: late prints for it no longer touch any time bar."""
from datetime import date, time

from agentdesk.bars import TimeBarBuilder, Trade
from agentdesk.clock import at_ct

T0 = at_ct(date(2026, 9, 29), time(9, 0))


def test_a_late_print_after_a_flush_does_not_reopen_the_minute():
    tb = TimeBarBuilder("1m")
    tb.on_trade(Trade(T0 + 10, 660.00, 100))
    first = tb.flush(T0 + 60.2)                       # heartbeat closes 09:00
    assert first is not None and first.t == T0
    assert tb.on_trade(Trade(T0 + 59.8, 661.00, 100)) is None     # late 09:00 print
    nxt = tb.on_trade(Trade(T0 + 61, 660.50, 100))                  # first 09:01 print
    assert nxt is None                                 # nothing closes: 09:00 was already published
    closed = tb.flush(T0 + 120.5)
    assert closed.t == T0 + 60 and closed.o == 660.50 and closed.h == 660.50


def test_a_late_print_for_a_closed_minute_does_not_change_the_next_bar():
    tb = TimeBarBuilder("1m")
    tb.on_trade(Trade(T0 + 10, 660.00, 100))
    tb.on_trade(Trade(T0 + 61, 660.50, 100))          # closes 09:00, opens 09:01
    tb.on_trade(Trade(T0 + 59.9, 655.00, 100))        # late 09:00 print
    closed = tb.flush(T0 + 120.5)
    assert closed.t == T0 + 60 and closed.l == 660.50 and closed.n == 1


def test_each_minute_closes_once_under_heartbeats_and_late_prints():
    tb, got = TimeBarBuilder("1m"), []
    for m in range(5):
        base = T0 + 60 * m
        for s in (5, 30, 58):
            c = tb.on_trade(Trade(base + s, 660 + m, 100))
            got += [c] if c else []
        c = tb.flush(base + 60.1)
        got += [c] if c else []
        c = tb.on_trade(Trade(base + 59.5, 700, 100))  # always one late print after the flush
        got += [c] if c else []
    ts = [b.t for b in got]
    assert ts == [T0 + 60 * m for m in range(5)]
    assert all(b.h < 700 for b in got)
