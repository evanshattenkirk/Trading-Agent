"""Live-order and watchdog safety: an order the broker can't confirm, broker errors, stale quotes and position
mismatches all halt and flatten instead of leaving a position without a stop."""
import asyncio
import copy
import sys
from datetime import date, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.brokers.base import Broker, OrderResult, OrderStateError
from agentdesk.brokers.robinhood import RobinhoodBroker
from agentdesk.bus import Bus
from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.engine import Engine
from agentdesk.exits import Contract, ExitPlan, Position
from agentdesk.feeds.base import Quote
from agentdesk.journal import Journal

CFG = load_config()
NOW = at_ct(date(2026, 9, 28), time(9, 30))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    real = asyncio.sleep

    async def no_wait(_s, *a, **k):
        await real(0)
    monkeypatch.setattr(asyncio, "sleep", no_wait)


# --------------------------------------------------------------------------- RobinhoodBroker.submit
class FakeRH:
    """Scripted Robinhood MCP: `script[tool]` is a list of results (or exceptions) returned in order; the last repeats."""

    def __init__(self, **script):
        self.script = script
        self.account = "12346452"
        self.calls: list[str] = []

    async def call(self, tool, args):
        self.calls.append(tool)
        seq = self.script.get(tool, [{}])
        r = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(r, Exception):
            raise r
        return r


def order(state, filled=0, avg=None):
    return {"orders": [{"state": state, "processed_quantity": str(filled), "average_price": avg}]}


def live_broker(rh):
    b = RobinhoodBroker(rh, quotes=None, live=True, fill_timeout=0.02)
    b.confirm_tries = 3
    return b


C = Contract("SPY", "2026-09-28", 660.0, "call", broker_id="opt-1")


def test_fill_after_cancel_is_recorded():
    rh = FakeRH(place_option_order=[{"id": "o1"}], get_option_orders=[order("queued")])
    b = live_broker(rh)
    orig = rh.call

    async def call(tool, args):          # the cancel races a fill: after cancel, the order reads back filled
        if tool == "cancel_option_order":
            rh.script["get_option_orders"] = [order("filled", 2, "1.10")]
        return await orig(tool, args)
    rh.call = call
    res = run(b.submit(C, "buy", 2, 1.10, NOW))
    assert res.status == "filled" and res.filled_qty == 2 and res.avg_price == 1.10
    assert "o1" not in b.open_orders


def test_poll_errors_do_not_escape():
    rh = FakeRH(place_option_order=[{"id": "o1"}],
                get_option_orders=[RuntimeError("500"), RuntimeError("500"), order("filled", 1, "2.00")])
    res = run(live_broker(rh).submit(C, "buy", 1, 2.0, NOW))
    assert res.status == "filled" and res.filled_qty == 1


def test_unconfirmed_order_raises_with_known_fill():
    rh = FakeRH(place_option_order=[{"id": "o1"}], get_option_orders=[order("partially_filled", 1, "1.50")])
    b = live_broker(rh)
    with pytest.raises(OrderStateError) as e:
        run(b.submit(C, "buy", 3, 1.5, NOW))
    assert e.value.order_id == "o1" and e.value.filled_qty == 1 and e.value.avg_price == 1.5
    assert "o1" in b.open_orders                 # kept so cancel_all tries it again


def test_missing_order_id_and_failed_place_raise():
    with pytest.raises(OrderStateError):
        run(live_broker(FakeRH(place_option_order=[{"ok": True}])).submit(C, "buy", 1, 1.0, NOW))
    with pytest.raises(OrderStateError):
        run(live_broker(FakeRH(place_option_order=[RuntimeError("timeout")])).submit(C, "buy", 1, 1.0, NOW))


def test_premium_per_contract_is_converted():
    rh = FakeRH(place_option_order=[{"id": "o1"}], get_option_orders=[order("filled", 1, "125.00")])
    assert run(live_broker(rh).submit(C, "buy", 1, 1.25, NOW)).avg_price == 1.25


# --------------------------------------------------------------------------- engine watchdog
class Feed:
    is_sim = False
    name = "test"

    def __init__(self):
        self.t = NOW

    def now(self):
        return self.t

    async def history_1m(self, days):
        return []

    async def stream(self):
        return
        yield


class Quotes:
    def __init__(self):
        self.q: Quote | Exception | None = Quote(1.00, 1.04, NOW)

    async def quote(self, contract):
        if isinstance(self.q, Exception):
            raise self.q
        return self.q

    async def start(self):
        pass


class FakeBroker(Broker):
    name = "fake"

    def __init__(self, live=False):
        self.live = live
        self.held = 0
        self.submits = []
        self.fail: Exception | None = None
        self.cancelled = 0

    async def submit(self, contract, side, qty, limit, now):
        self.submits.append((side, qty))
        if self.fail:
            raise self.fail
        return OrderResult("filled", qty, limit, "x")

    async def cancel_all(self):
        self.cancelled += 1

    async def position_qty(self):
        return self.held if self.live else None


def engine(live=False):
    e = Engine(copy.deepcopy(CFG), Feed(), Quotes(), FakeBroker(live), Bus(), Journal(None), "paper")
    e.inline = True
    return e


def open_pos(e, qty=2):
    pos = Position(C, "SWING", qty, 1.00, e.feed.t)
    e.open.append((pos, ExitPlan(e.cfg["exits"], pos)))
    return pos


def test_stale_quote_halts_and_flattens():
    e = engine()
    open_pos(e)
    e.quotes.q = None
    e.feed.t += 11

    async def go():
        await e.manage(e.feed.t)
        e._watchdog(e.feed.t)
    run(go())
    assert e.risk.st.halted and e.risk.st.flatten_all and "no fresh quote" in e.risk.st.halt_reason
    assert e.risk.must_flatten(e.feed.t)
    e.quotes.q = Quote(0.95, 0.99, e.feed.t)       # quotes come back: the flatten sells everything
    run(e.manage(e.feed.t))
    assert not e.open and e.broker.submits[-1] == ("sell", 2)


def test_fresh_quotes_keep_trading():
    e = engine()
    open_pos(e)
    for _ in range(30):
        e.feed.t += 1
        e.quotes.q = Quote(1.00, 1.04, e.feed.t)
        run(e.manage(e.feed.t))
        e._watchdog(e.feed.t)
    assert not e.risk.st.halted and e.open


def test_consecutive_errors_halt():
    e = engine()
    open_pos(e)
    e.quotes.q = RuntimeError("robinhood 503")

    async def go():
        for _ in range(3):
            await e._run(e.manage(e.feed.t), "manage")
    run(go())
    assert e.risk.st.halted and "3 broker/API errors" in e.risk.st.halt_reason


def test_error_count_resets_on_good_quote():
    e = engine()
    open_pos(e)

    async def go():
        for q in (RuntimeError("x"), RuntimeError("x"), Quote(1.0, 1.04, NOW), RuntimeError("x"), RuntimeError("x")):
            e.quotes.q = q
            await e._run(e.manage(e.feed.t), "manage")
    run(go())
    assert not e.risk.st.halted


def test_ambiguous_entry_tracks_known_fill_and_halts():
    e = engine()
    e.broker.fail = OrderStateError("not confirmed", "o9", 1, 1.02)
    res = run(e._work_order(C, "buy", 3, 1.02, NOW))
    assert res.filled_qty == 1 and res.avg_price == 1.02
    assert e.broker.submits == [("buy", 3)]         # no reprice after an ambiguous order
    assert e.risk.st.halted and e.risk.st.flatten_all


def test_position_mismatch_halts_on_second_check():
    e = engine(live=True)
    open_pos(e, 2)
    e.broker.held = 3
    run(e._reconcile(NOW))
    assert not e.risk.st.halted
    run(e._reconcile(NOW + 30))
    assert e.risk.st.halted and "mismatch" in e.risk.st.halt_reason
    e2 = engine(live=True)
    open_pos(e2, 2)
    e2.broker.held = 2
    run(e2._reconcile(NOW))
    run(e2._reconcile(NOW + 30))
    assert not e2.risk.st.halted


def test_startup_position_read_failure_halts():
    e = engine()

    async def boom():
        raise RuntimeError("401")
    e.broker.open_positions = boom
    run(e.run())
    assert e.risk.st.halted and "could not read account positions" in e.risk.st.halt_reason


def test_kill_continues_after_a_failed_exit():
    e = engine()
    open_pos(e)
    e.quotes.q = RuntimeError("down")
    run(e.kill())
    assert e.risk.st.halted and e.risk.st.flatten_all and e.broker.cancelled == 1


def test_rate_limits_never_trip_the_safety_halt():             # 2026-09-29 12:50 CT: 3 throttles halted every book
    from agentdesk.brokers.base import RateLimited
    e = engine()
    open_pos(e)
    e.quotes.q = RateLimited("get_option_quotes error: error: RATE_LIMITED: too many requests, please try again shortly")
    msgs = []

    async def go():
        sub = e.bus.subscribe()
        for _ in range(6):
            e.feed.t += 1
            await e._run(e.manage(e.feed.t), "manage")
        while not sub.empty():
            msgs.append(sub.get_nowait())
    run(go())
    assert not e.risk.st.halted and e._errors == 0
    logs = [m for m in msgs if m["type"] == "log"]
    assert len(logs) == 1 and logs[0]["level"] == "warn" and "rate limit" in logs[0]["msg"]


def test_a_clean_call_of_another_kind_does_not_hide_failing_exit_checks():
    e = engine()
    open_pos(e)

    async def fine():
        return None

    async def go():
        for _ in range(3):
            e.quotes.q = RuntimeError("robinhood 503")
            await e._run(e.manage(e.feed.t), "manage")
            await e._run(fine(), "position check")
    run(go())
    assert e.risk.st.halted and "3 broker/API errors" in e.risk.st.halt_reason


def test_watchdog_skips_a_position_that_is_mid_exit():                              # 2026-10-01 sweep item 7
    e = engine()
    pos = open_pos(e)
    pos._exiting = True                         # a slow live sell is in flight; its own order checks guard it
    e._watchdog(e.feed.t + 30)
    assert not e.risk.st.halted
    pos._exiting = False

    async def go():
        e._watchdog(e.feed.t + 30)
    run(go())
    assert e.risk.st.halted and "no fresh quote" in e.risk.st.halt_reason


def test_watchdog_skips_a_paper_book_position_that_is_mid_exit():
    e = engine()
    p = type("P", (), {"label": "B fly", "last_quote_ts": e.feed.t, "exiting": True, "watchdog_exempt": False})()
    e.books = type("H", (), {"positions": lambda s: [p]})()
    e._watchdog(e.feed.t + 30)
    assert not e.risk.st.halted
