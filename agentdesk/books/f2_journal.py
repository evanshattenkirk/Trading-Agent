"""Book F2 tables in the engine's SQLite journal (research/strategy_f2_prereg.md).

  f2_positions  every F2 spread as JSON, open or closed. Open rows are restored at startup: F2 holds across days.
  f2_decisions  every candidate F2 looked at, traded or skipped, with its reason and the observe-only option
                context (each leg's IV, the call/put skew between them, ATM IV vs 20-day realized vol, debit/width)
Closed F2 trades also go into `trades` with book = 'F2'.
"""
from __future__ import annotations

import json
import sqlite3

from .combo import ComboPosition
from .e_journal import pos_from_dict, pos_to_dict

SCHEMA = """
CREATE TABLE IF NOT EXISTS f2_positions (
  id TEXT PRIMARY KEY, status TEXT, symbol TEXT, setup TEXT, opened_ts REAL, updated_ts REAL, data TEXT
);
CREATE TABLE IF NOT EXISTS f2_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, ts REAL, symbol TEXT, setup TEXT, outcome TEXT, reason TEXT,
  spot REAL, expiry TEXT, long_k REAL, short_k REAL, debit REAL, width REAL, lots INTEGER, iv_long REAL,
  iv_short REAL, skew REAL, iv_rv REAL, signal TEXT
);
"""
DECISION_COLS = ("day", "ts", "symbol", "setup", "outcome", "reason", "spot", "expiry", "long_k", "short_k", "debit",
                 "width", "lots", "iv_long", "iv_short", "skew", "iv_rv", "signal")


class F2Journal:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(SCHEMA)

    def save(self, pos: ComboPosition, now: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO f2_positions VALUES (?,?,?,?,?,?,?)",
                        (pos.id, pos.status, pos.meta.get("symbol"), pos.meta.get("setup_key"), pos.opened_ts, now,
                         json.dumps(pos_to_dict(pos))))
        self.db.commit()

    def open_positions(self) -> tuple[list[ComboPosition], list[str]]:
        ok, bad = [], []
        for pid, data in self.db.execute("SELECT id, data FROM f2_positions WHERE status='open' ORDER BY opened_ts").fetchall():
            try:
                ok.append(pos_from_dict(json.loads(data)))
            except (ValueError, KeyError, TypeError, IndexError):
                bad.append(pid)
        return ok, bad

    def decision(self, **row) -> None:
        if isinstance(row.get("signal"), dict):
            row = {**row, "signal": json.dumps(row["signal"])}
        self.db.execute(f"INSERT INTO f2_decisions ({','.join(DECISION_COLS)}) VALUES ({','.join('?' * len(DECISION_COLS))})",
                        tuple(row.get(c) for c in DECISION_COLS))
        self.db.commit()

    def traded_today(self, day: str, symbol: str) -> bool:
        return self.db.execute("SELECT 1 FROM f2_decisions WHERE day=? AND symbol=? AND outcome='opened' LIMIT 1",
                               (day, symbol)).fetchone() is not None

    def decisions(self, day: str | None = None, since: str | None = None) -> list[dict]:
        if day:
            cur = self.db.execute("SELECT * FROM f2_decisions WHERE day=? ORDER BY id", (day,))
        else:
            cur = self.db.execute("SELECT * FROM f2_decisions WHERE day>=? ORDER BY id", (since or "",))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def closed(self, since: str | None = None) -> list[dict]:
        out = []
        for (data,) in self.db.execute("SELECT data FROM f2_positions WHERE status='closed' ORDER BY opened_ts").fetchall():
            d = json.loads(data)
            if not since or str(d.get("meta", {}).get("entry_day", "")) >= since:
                out.append(d)
        return out
