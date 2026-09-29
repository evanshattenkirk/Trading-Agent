import asyncio
import copy

import pytest

from agentdesk.__main__ import build
from agentdesk.config import load_config

CFG = load_config()
SEEDS = (21, 7)


def run_day(seed, books_on):
    cfg = copy.deepcopy(CFG)
    if not books_on:
        for k in ("B_iron_fly", "C_orb_bull_put", "D_iron_condor", "G_call_calendar"):
            cfg["books"][k]["enabled"] = False
    engine, bus = build(cfg, "sim", 0, seed, "2026-09-28")
    engine.inline = True
    asyncio.run(engine.run())
    return engine


def a_trades(engine):
    return [(p.contract.label, p.setup, p.qty_initial, round(p.entry, 2), p.exit_reason, round(p.realized - p.fees, 2))
            for p in engine.closed]


@pytest.mark.parametrize("seed", SEEDS)
def test_book_a_is_identical_with_books_on_and_off(seed):
    off, on = run_day(seed, False), run_day(seed, True)
    assert a_trades(on) == a_trades(off)
    assert on.risk.st.day_pnl == pytest.approx(off.risk.st.day_pnl)


def test_books_trade_in_sim_and_journal_rows_are_tagged():
    books_traded, total = set(), 0
    for seed in SEEDS:
        e = run_day(seed, True)
        rows = e.journal.trades()
        assert {r["book"] for r in rows} <= {"A", "B", "C", "D", "F1", "G"}     # F1: stocks in play (shares); G: call calendar (F3)
        assert sum(1 for r in rows if r["book"] == "A") == len(e.closed)
        combo = [r for r in rows if r["book"] != "A"]
        books_traded |= {r["book"] for r in combo}
        assert sum(b.day_pnl for b in e.books.books) == pytest.approx(e.books.account.realized)
        assert sum(r["pnl"] for r in combo) == pytest.approx(e.books.account.realized)
        assert not e.books.positions()                      # everything flat by the close
        total += len(combo)
    assert total >= 1 and books_traded & {"B", "D"}
