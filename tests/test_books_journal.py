import json
import sqlite3

from books_fakes import contracts
from agentdesk.books.base import Leg
from agentdesk.books.combo import ComboPosition
from agentdesk.journal import Journal

PUTS = [Leg("put", 764, "sell"), Leg("put", 762, "buy")]


def test_combo_trade_row_carries_book_legs_and_risk(tmp_path):
    j = Journal(tmp_path / "j.db")
    p = ComboPosition("C", "ORB BULL PUT", PUTS, contracts(PUTS), 1, 0.50, True, 2.0, 100.0)
    p.realized, p.fees, p.closed_ts, p.exit_reason = 20.0, 0.16, 200.0, "SPY target +0.45%"
    j.record_trade("2026-09-28", "paper", p, book="C")
    r = j.trades()[0]
    assert r["book"] == "C" and r["max_loss"] == 150.0 and len(json.loads(r["legs"])) == 2
    assert abs(r["pnl_pct"] - (20.0 - 0.16) / 150.0) < 1e-9


def test_old_database_gains_columns_and_book_a_default(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, mode TEXT, contract TEXT,"
                     " occ TEXT, setup TEXT, qty INTEGER, entry REAL, opened_ts REAL, closed_ts REAL, realized REAL,"
                     " fees REAL, pnl REAL, pnl_pct REAL, peak REAL, exit_reason TEXT, strike_reason TEXT,"
                     " entry_reasons TEXT, fills TEXT);"
                     "INSERT INTO trades (session, contract) VALUES ('2026-09-25', 'SPY 766C 09-25');")
    db.commit()
    db.close()
    r = Journal(path).trades()[0]
    assert r["book"] == "A" and r["legs"] is None
