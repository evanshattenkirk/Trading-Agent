"""SQLite trade journal (one row per closed position, fills as JSON)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session TEXT, mode TEXT, contract TEXT, occ TEXT, setup TEXT,
  qty INTEGER, entry REAL, opened_ts REAL, closed_ts REAL,
  realized REAL, fees REAL, pnl REAL, pnl_pct REAL, peak REAL,
  exit_reason TEXT, strike_reason TEXT, entry_reasons TEXT, fills TEXT, l2 TEXT,
  book TEXT DEFAULT 'A', legs TEXT, max_loss REAL
);
CREATE TABLE IF NOT EXISTS option_quotes (
  ts REAL, expiry TEXT, strike REAL, right TEXT, bid REAL, ask REAL, spot REAL
);
CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, ts REAL, desk TEXT, brief TEXT
);
"""


class Journal:
    def __init__(self, path: Path | str | None):
        self.db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self.db.executescript(SCHEMA)
        for col in ("l2 TEXT", "book TEXT DEFAULT 'A'", "legs TEXT", "max_loss REAL"):
            try:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass

    def record_trade(self, session: str, mode: str, p, book: str = "A") -> None:
        d = p.to_dict()
        basis = getattr(p, "risk_basis", None) or p.entry * 100 * p.qty_initial
        self.db.execute(
            "INSERT INTO trades (session,mode,contract,occ,setup,qty,entry,opened_ts,closed_ts,realized,fees,pnl,pnl_pct,peak,"
            "exit_reason,strike_reason,entry_reasons,fills,l2,book,legs,max_loss) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session, mode, d["contract"], d["occ"], p.setup, p.qty_initial, p.entry, p.opened_ts, p.closed_ts,
             p.realized, p.fees, p.realized - p.fees, (p.realized - p.fees) / basis if basis else 0, p.peak,
             p.exit_reason, p.strike_reason, json.dumps(p.entry_reasons), json.dumps(p.fills), json.dumps(p.l2),
             book, json.dumps(d["legs"]) if "legs" in d else None, getattr(p, "risk_basis", None)))
        self.db.commit()

    def record_quotes(self, rows: list[tuple]) -> None:
        if rows:
            self.db.executemany("INSERT INTO option_quotes VALUES (?,?,?,?,?,?,?)", rows)
            self.db.commit()

    def record_brief(self, session: str, ts: float, desk: str, brief: dict) -> None:
        self.db.execute("INSERT INTO briefs (session,ts,desk,brief) VALUES (?,?,?,?)", (session, ts, desk, json.dumps(brief)))
        self.db.commit()

    def trades(self, session: str | None = None, limit: int = 500) -> list[dict]:
        cur = self.db.execute(
            "SELECT * FROM trades " + ("WHERE session=? " if session else "") + "ORDER BY id DESC LIMIT ?",
            (session, limit) if session else (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
