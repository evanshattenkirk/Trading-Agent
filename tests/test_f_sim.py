"""A sim day runs books A-F together without cross-contamination (BOOK_F_HANDOFF section 8)."""
import asyncio
import copy

import pytest

from agentdesk.__main__ import build
from agentdesk.config import load_config

CFG = load_config()
SEEDS = (21, 7)


def run_day(seed, f_on=True, others_on=True, day="2026-09-28"):
    cfg = copy.deepcopy(CFG)
    cfg["books"]["F1_stocks_in_play"]["enabled"] = f_on
    if not others_on:
        for k in ("B_iron_fly", "C_orb_bull_put", "D_iron_condor", "G_call_calendar"):
            cfg["books"][k]["enabled"] = False
    engine, bus = build(cfg, "sim", 0, seed, day)
    engine.inline = True
    asyncio.run(engine.run())
    return engine


def a_trades(e):
    return [(p.contract.label, p.setup, p.qty_initial, round(p.entry, 2), p.exit_reason, round(p.realized - p.fees, 2))
            for p in e.closed]


def bcd_trades(e):
    return [(r["book"], r["contract"], r["qty"], r["entry"], r["exit_reason"], r["pnl"])
            for r in e.journal.trades() if r["book"] in ("B", "C", "D")]


@pytest.mark.parametrize("seed", SEEDS)
def test_a_through_e_are_identical_with_f_on_and_off(seed):
    off, on = run_day(seed, f_on=False), run_day(seed, f_on=True)
    assert a_trades(on) == a_trades(off)
    assert sorted(bcd_trades(on)) == sorted(bcd_trades(off))
    assert on.risk.st.day_pnl == pytest.approx(off.risk.st.day_pnl)


def test_f_scans_trades_and_is_flat_by_the_close():
    total = 0
    for seed in SEEDS:
        e = run_day(seed)
        f = e.books.fhost
        assert f.scanned and f.fj.scans()                          # the scan ran and every row was journaled
        rows = [r for r in e.journal.trades() if r["book"] == "F1"]
        assert len(rows) == len(f.book.closed) == len(f.fj.trades())
        assert not f.book.open and not e.books.positions()        # nothing held overnight
        assert all(r["exit_reason"] for r in rows)
        assert sum(r["pnl"] for r in rows) == pytest.approx(f.book.day_pnl)
        total += len(rows)
    assert total >= 1


def test_f_runs_alone_without_the_option_books():
    e = run_day(21, others_on=False)
    assert e.books.bookhost is None and e.books.fhost.scanned
