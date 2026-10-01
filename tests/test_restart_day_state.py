"""A mid-day restart rebuilds each book's day from the journal (Evan, 2026-10-01, sweep item 3): trade counts, wins,
losses and day P&L for B/C/D/G, E, F1 and F2, the shared account's realized P&L, F1's daily-loss halt and one entry
per name, and F2's daily-loss block."""
import asyncio
import copy
import sys
from datetime import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.clock import at_ct

from books_fakes import DAY, ct_ts


def journal_trade(j, session, book, pnl, opened_ts, occ="X", exit_reason="stop"):
    j.db.execute("INSERT INTO trades (session,mode,contract,occ,setup,qty,entry,opened_ts,closed_ts,realized,fees,pnl,"
                 "exit_reason,book) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (session, "paper", occ, occ, book, 1, 1.0, opened_ts, opened_ts + 600, pnl, 0.0, pnl, exit_reason, book))
    j.db.commit()


def test_bookhost_restart_keeps_todays_counts_pnl_and_account():
    from test_books_host import setup, tick
    eng, fq, host = setup(intents=("B",))
    journal_trade(eng.journal, str(DAY), "B", -50.0, ct_ts(8, 45))
    journal_trade(eng.journal, "2026-09-25", "B", -99.0, ct_ts(8, 45) - 3 * 86400)    # another day: ignored
    journal_trade(eng.journal, str(DAY), "A", -20.0, ct_ts(9, 0))                     # book A: risk_state has it
    asyncio.run(host.start())
    b = host.books[0]
    assert (b.trades, b.losses, b.day_pnl) == (1, 1, -50.0)
    assert host.account.realized == -50.0
    tick(host, fq, ct_ts(10, 0))
    assert not host.positions()                                   # B's 1 trade a day is used up


def test_f1_restart_keeps_its_daily_loss_halt_and_one_entry_per_name():
    from test_f_host import et, make, run
    from agentdesk.books import f_stocks_in_play as F
    eng, data, host = make(names=("NVDA", "MU"))
    lim = abs(host.c["daily_loss"])
    journal_trade(eng.journal, "2026-10-01", "F1", -(lim + 5), et(9, 40), occ="NVDA")
    eng.feed.t = et(10, 0)
    run(host.start())
    assert host.book.halted and "daily loss" in host.book.halt_reason
    assert host.book.trades == 1 and host.status.get("NVDA") == "stopped"


def test_f1_restart_does_not_rearm_a_name_it_already_traded():
    from test_f_host import et, make, run, at
    eng, data, host = make(names=("NVDA", "MU"))
    journal_trade(eng.journal, "2026-10-01", "F1", 10.0, et(9, 40), occ="NVDA", exit_reason="target")
    eng.feed.t = et(8, 0)
    run(host.start())
    at(host, eng, et(8, 30, 1))
    at(host, eng, et(9, 35, 5))
    assert "NVDA" not in host.armed and "MU" in host.armed


def test_f2_restart_keeps_its_daily_loss_block():
    from test_f2 import THU, et, make
    h, eng, ch, f1 = make()
    lim = abs(float(h.c["daily_loss"]))
    journal_trade(eng.journal, str(THU), "F2", -(lim + 10), et(THU, 9, 50))
    eng.feed.t = et(THU, 10, 0)
    asyncio.run(h.start())
    assert h.book.day_pnl == -(lim + 10) and "daily loss" in (h.book.blocked or "")


def test_e_restart_counts_todays_closed_trades():
    from test_e_host import MON, at, make
    h, eng, ch = make()
    journal_trade(eng.journal, str(MON), "E", 40.0, at(MON, 14, 45) - 3 * 86400)     # opened days ago, closed today
    eng.feed.t = at(MON, 9, 0)
    asyncio.run(h.start())
    assert h.book.day_pnl == 40.0 and h.book.wins == 1
    assert h.book.trades == 0                                    # it wasn't opened today
