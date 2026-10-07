"""The stale-quote watchdog vs Robinhood throttling and slow entries (review 2026-10-06, H2 and M9).

H2: RATE_LIMITED pauses of 2, 4 and 8 s in a row hold every call for 14 s; that time must not count toward a
position's quote age, while a real outage (no throttle, no quotes) still trips the 10 s watchdog.
M9: an entry whose orders took longer than the watchdog's window must not trip it on the tick after the fill."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.brokers.robinhood import CallBudget
from agentdesk.feeds.base import Quote

from test_safety import NOW, engine, open_pos, run


def throttled_engine():
    e = engine()
    b = CallBudget(per_min=120, per_s=3, clock=lambda: e.feed.t, sleep=None)
    e.quotes.rh = SimpleNamespace(budget=b)          # the engine's Robinhood client and its call budget
    return e, b


def tick_until(e, t_end, at=None):
    """One watchdog check per second, as _on_second runs it; `at` maps a second to something that happens then."""
    async def go():
        t = e.feed.t
        while t < t_end and not e.risk.st.halted:
            t += 1
            e.feed.t = t
            (at or {}).get(t, lambda: None)()
            e._watchdog(t)
        return t
    return run(go())


def test_budget_says_when_it_holds_every_call():
    clock = [1000.0]
    b = CallBudget(per_min=3, per_s=100, clock=lambda: clock[0], sleep=None)
    assert not b.holding()
    b.throttled()                                                    # 2 s pause
    assert b.holding()
    clock[0] += 2.5
    assert not b.holding()
    b.recent = [clock[0]] * 3                                        # this minute's budget used up
    assert b.holding()
    clock[0] += 61
    assert not b.holding()


def test_back_to_back_throttles_do_not_trip_the_watchdog():          # 2026-09-29: 773 RATE_LIMITED answers
    e, b = throttled_engine()
    open_pos(e)
    t0 = e.feed.t
    tick_until(e, t0 + 15, at={t0 + 1: b.throttled, t0 + 3: b.throttled, t0 + 7: b.throttled})    # paused to +15
    assert not e.risk.st.halted
    e.quotes.q = Quote(1.00, 1.04, t0 + 16)                          # the pause ends and a quote comes back
    run(e.manage(t0 + 16))
    tick_until(e, t0 + 25)
    assert not e.risk.st.halted and e.open


def test_a_full_minute_budget_does_not_trip_the_watchdog():
    e, b = throttled_engine()
    b.per_min = 5
    open_pos(e)
    t0 = e.feed.t
    b.recent = [t0 + 0.5] * 5                                         # every call waits for the minute window
    tick_until(e, t0 + 30)
    assert not e.risk.st.halted


def test_a_real_outage_still_trips_the_watchdog():
    e, _ = throttled_engine()
    open_pos(e)
    t0 = e.feed.t
    end = tick_until(e, t0 + 30)
    assert e.risk.st.halted and "no fresh quote" in e.risk.st.halt_reason and end == t0 + 11


def test_an_outage_after_a_throttle_trips_once_the_throttle_time_is_used_up():
    e, b = throttled_engine()
    open_pos(e)
    t0 = e.feed.t
    end = tick_until(e, t0 + 60, at={t0 + 1: b.throttled})          # one 2 s pause, then nothing comes back
    assert e.risk.st.halted and t0 + 11 <= end <= t0 + 14


def test_a_slow_entry_does_not_trip_the_watchdog_on_the_next_tick():
    from agentdesk.strategy import EntrySignal
    e = engine()
    e.price = 660.0
    e.quotes.q = Quote(1.00, 1.00, NOW)
    real = e.broker.submit

    async def slow_fill(contract, side, qty, limit, now):
        e.feed.t += 15             # resolve, quotes, one timed-out call and its retry: filled 15 s after the signal
        return await real(contract, side, qty, limit, now)
    e.broker.submit = slow_fill

    async def go():
        await e.enter(EntrySignal("call", "SWING", NOW, 660.0, "1m", ("1m", 1)))
        e._watchdog(e.feed.t + 1)
    run(go())
    assert e.open and not e.risk.st.halted
