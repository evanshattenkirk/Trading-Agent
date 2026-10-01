"""Book A exit bugs found in the 2026-10-01 sweep: a scale-out that didn't fill is retried, a flatten waits for an
exit already in flight instead of skipping that position, and the entry fee counts toward the day's P&L."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.brokers.base import OrderResult
from agentdesk.exits import ExitIntent
from agentdesk.feeds.base import Quote

from test_safety import NOW, engine, open_pos, run


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    real = asyncio.sleep

    async def no_wait(_s, *a, **k):
        await real(0)
    monkeypatch.setattr(asyncio, "sleep", no_wait)


def unfilled_sells(e):
    real = e.broker.submit

    async def submit(contract, side, qty, limit, now):
        if side == "sell":
            e.broker.submits.append((side, qty))
            return OrderResult("unfilled", 0, limit, "x")
        return await real(contract, side, qty, limit, now)
    e.broker.submit = submit


def test_unfilled_scale_out_is_retried():
    e = engine()
    pos = open_pos(e, qty=4)
    unfilled_sells(e)
    e.quotes.q = Quote(1.24, 1.27, NOW)                 # mark >= +25%: first scale-out
    run(e.manage(NOW))
    assert pos.qty == 4 and pos.scales_done == 0         # nothing sold, so the scale-out isn't done
    e.broker.submit = type(e.broker).submit.__get__(e.broker)
    run(e.manage(NOW + 1))
    assert pos.qty == 2 and pos.scales_done == 1


def test_scale_out_dropped_while_another_exit_runs_is_retried():
    e = engine()
    pos = open_pos(e, qty=4)
    pos._exiting = True                                  # e.g. a cross-back exit is mid-order
    e.quotes.q = Quote(1.24, 1.27, NOW)
    run(e.manage(NOW))
    assert pos.scales_done == 0


def test_flatten_waits_for_an_exit_in_flight():
    e = engine()
    pos = open_pos(e, qty=2)
    pos._exiting = True

    async def go():
        async def finish():
            await asyncio.sleep(0)
            pos._exiting = False                         # the scale-out finished without selling everything
        t = asyncio.create_task(finish())
        await e.flatten("shutdown")
        await t
    run(go())
    assert not e.open and ("sell", 2) in e.broker.submits


def test_entry_fee_counts_toward_day_pnl():
    e = engine()
    pos = open_pos(e, qty=2)
    fee = e.cfg["sizing"]["fee_per_contract"]
    e.quotes.q = Quote(1.00, 1.00, NOW)
    run(e.exit(pos, e.open[0][1], ExitIntent(2, "test", urgent=True), NOW))
    # bought at 1.00, sold at 1.00: the day is down exactly the two fees (entry + exit)
    assert e.risk.st.day_pnl == pytest.approx(-(fee * 2) * 2)
