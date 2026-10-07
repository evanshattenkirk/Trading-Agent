"""Engine fixes from the 2026-10-06 review: a journal write that fails never loses the trade or its event (M12),
scale-outs round half up and roll back cleanly (D2, L7), the spread step never goes ITM from ATM, pre-market prints
stay out of the 144t series, and a mid-day restart continues today's bars instead of leaving a hole (M11)."""
import asyncio
import logging
import sqlite3
import sys
from datetime import date, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.exits import ExitIntent

from test_safety import NOW, engine, open_pos, run


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    real = asyncio.sleep

    async def no_wait(_s, *a, **k):
        await real(0)
    monkeypatch.setattr(asyncio, "sleep", no_wait)


def taps(e) -> list[dict]:
    seen: list[dict] = []
    e.bus.taps.append(seen.append)
    return seen


# --------------------------------------------------------------------------- M12: record_trade failure
def failing_journal(e, fails: int):
    real = e.journal.record_trade
    calls = []

    def record_trade(*a, **k):
        calls.append(a)
        if len(calls) <= fails:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    e.journal.record_trade = record_trade
    return calls


def test_a_locked_journal_is_retried_once_and_the_trade_lands():
    e = engine()
    pos = open_pos(e, qty=2)
    calls = failing_journal(e, fails=1)
    seen = taps(e)
    run(e.exit(pos, e.open[0][1], ExitIntent(2, "test", urgent=True), NOW))
    assert len(calls) == 2 and len(e.journal.trades()) == 1
    assert [x for x in seen if x["type"] == "trade_closed"]
    assert not e._streaks and e._errors == 0


def test_a_journal_that_keeps_failing_still_emits_trade_closed_and_never_counts_as_an_error(caplog):
    e = engine()
    pos = open_pos(e, qty=2)
    calls = failing_journal(e, fails=99)
    seen = taps(e)
    with caplog.at_level(logging.ERROR, logger="agentdesk.engine"):
        run(e._guard(e.exit(pos, e.open[0][1], ExitIntent(2, "test", urgent=True), NOW), "manage"))
    assert len(calls) == 2 and e.journal.trades() == []
    closed = [x for x in seen if x["type"] == "trade_closed"]
    assert closed and closed[0]["pos"]["id"] == pos.id
    assert not e.open and e.closed == [pos]
    assert not e._streaks and e._errors == 0 and not e.risk.st.halted        # no strike toward the safety halt
    assert "not journaled" in caplog.text and f"'id': {pos.id}" in caplog.text     # the trade dict is in the log
    assert any(x["type"] == "log" and x["level"] == "error" and "not journaled" in x["msg"] for x in seen)


# --------------------------------------------------------------------------- D2 / L7: scale-outs
def _plan(setup, qty):
    from agentdesk.exits import ExitPlan, Position
    from test_safety import C
    p = Position(C, setup, qty, 1.00, NOW)
    return p, ExitPlan(engine().cfg["exits"], p)


@pytest.mark.parametrize("qty,first,second", [(5, 3, 1), (4, 2, 1), (3, 2, None), (2, 1, None)])
def test_swing_scale_outs_round_half_up_and_keep_a_runner(qty, first, second):
    p, plan = _plan("SWING", qty)                     # HANDOFF 7A: sell 50% at +25%, 25% at +50% of the initial size
    x = plan.on_quote(1.24, 1.27, NOW + 60)
    assert x.scale and x.qty == first
    p.qty -= x.qty
    x = plan.on_quote(1.49, 1.52, NOW + 60)
    assert (x.qty if x and x.scale else None) == second


def test_scalp_scale_out_rounds_half_up():
    p, plan = _plan("SCALP", 5)                       # 50% at +15%
    assert plan.on_quote(1.15, 1.17, NOW + 30).qty == 3


def test_an_unfilled_scale_out_puts_the_stop_back_with_the_scale_count():
    from agentdesk.brokers.base import OrderResult
    from agentdesk.feeds.base import Quote
    e = engine()
    pos = open_pos(e, qty=4)
    assert pos.stop == 0.65                           # -35%

    async def unfilled(contract, side, qty, limit, now):
        return OrderResult("unfilled", 0, limit, "x")
    e.broker.submit = unfilled
    e.quotes.q = Quote(1.24, 1.27, NOW)               # +25%: first scale-out, which moves the stop to breakeven
    run(e.manage(NOW))
    assert pos.qty == 4 and pos.scales_done == 0
    assert pos.stop == 0.65                           # no scale happened, so no breakeven stop either
    e.quotes.q = Quote(0.80, 0.82, NOW + 1)           # -19%: above the -35% stop, so the position is still held
    run(e.manage(NOW + 1))
    assert pos.qty == 4 and e.open


# --------------------------------------------------------------------------- D1: one fee for every book
def test_book_a_pays_the_same_fee_per_contract_per_side_as_the_books():
    from agentdesk.brokers.base import OrderResult
    from agentdesk.feeds.base import Quote
    e = engine()
    assert e.cfg["sizing"]["fee_per_contract"] == 0.04 == e.cfg["books"]["account"]["fee_per_leg"]
    pos = open_pos(e, qty=2)
    e.quotes.q = Quote(1.20, 1.22, NOW)

    async def fill(contract, side, qty, limit, now):
        return OrderResult("filled", qty, 1.20, "x")
    e.broker.submit = fill
    run(e.exit(pos, e.open[0][1], ExitIntent(2, "test", urgent=True), NOW))
    assert pos.fees == pytest.approx(0.08) and e.journal.trades()[0]["pnl"] == pytest.approx(40.0 - 0.08)


# --------------------------------------------------------------------------- spread step (HANDOFF 7A)
class WideAt:
    """Quotes that are too wide (30%) at the listed strikes and 2% wide elsewhere; records what was quoted."""

    def __init__(self, *wide):
        self.wide, self.asked = set(wide), []

    async def quote(self, contract):
        from agentdesk.feeds.base import Quote
        self.asked.append(contract.strike)
        return Quote(0.85, 1.15, NOW) if contract.strike in self.wide else Quote(0.99, 1.01, NOW)

    async def start(self):
        pass


def _enter(base: int, quotes):
    from agentdesk.strategy import EntrySignal
    e = engine()
    e.quotes = quotes
    e.cfg["strikes"]["schedule"] = [{"from": "08:30", "base": base, "max": base}]
    e.price = 660.2
    seen = taps(e)
    run(e.enter(EntrySignal("call", "SWING", NOW, 660.2, "1m", ("1m", 1))))
    return e, seen


def test_a_wide_spread_at_atm_skips_instead_of_stepping_itm():
    e, seen = _enter(0, WideAt(660.0))
    assert not e.open and e.quotes.asked == [660.0]                  # never quoted the 659 ITM strike
    assert any(x["type"] == "skip" and "too wide" in x["why"] for x in seen)


def test_a_wide_spread_otm_still_steps_toward_atm():
    e, _ = _enter(1, WideAt(661.0))
    assert e.quotes.asked == [661.0, 660.0]
    assert e.open and e.open[0][0].contract.strike == 660.0
