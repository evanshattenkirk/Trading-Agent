"""Book E tables in the engine's SQLite journal (spec section 1).

  e_positions   every E position as JSON, open or closed. Open rows are restored at startup: E holds across days.
  e_decisions   every candidate E looked at, traded or skipped, with its reason, debit, IV and VIX.
Closed E trades also go into `trades` with book = 'E'.
"""
from __future__ import annotations

import json
import sqlite3

from ..exits import Contract
from .base import Leg
from .combo import ComboPosition

SCHEMA = """
CREATE TABLE IF NOT EXISTS e_positions (
  id TEXT PRIMARY KEY, status TEXT, symbol TEXT, structure TEXT, opened_ts REAL, updated_ts REAL, data TEXT
);
CREATE TABLE IF NOT EXISTS e_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, ts REAL, symbol TEXT, structure TEXT, T INTEGER,
  earnings_date TEXT, timing TEXT, outcome TEXT, reason TEXT, debit REAL, lots INTEGER, iv REAL, iv_pct REAL, vix REAL
);
"""
DECISION_COLS = ("day", "ts", "symbol", "structure", "T", "earnings_date", "timing", "outcome", "reason", "debit",
                 "lots", "iv", "iv_pct", "vix")


def pos_to_dict(p: ComboPosition) -> dict:
    return {"book": p.book, "setup": p.setup, "id": p.id,
            "legs": [[l.right, l.strike, l.side, l.ratio, l.dte] for l in p.legs],
            "contracts": [[c.symbol, c.expiry, c.strike, c.right, c.broker_id] for c in p.contracts],
            "qty_initial": p.qty_initial, "qty": p.qty, "entry": p.entry, "credit": p.credit, "width": p.width,
            "opened_ts": p.opened_ts, "strike_reason": p.strike_reason, "entry_reasons": list(p.entry_reasons),
            "meta": dict(p.meta), "mark": p.mark, "peak": p.peak, "stop": p.stop, "target": p.target,
            "realized": p.realized, "fees": p.fees, "fills": list(p.fills), "status": p.status,
            "closed_ts": p.closed_ts, "exit_reason": p.exit_reason}


def pos_from_dict(d: dict) -> ComboPosition:
    return ComboPosition(d["book"], d["setup"], [Leg(*l) for l in d["legs"]], [Contract(*c) for c in d["contracts"]],
                         d["qty_initial"], d["entry"], d["credit"], d["width"], d["opened_ts"],
                         strike_reason=d.get("strike_reason", ""), entry_reasons=list(d.get("entry_reasons") or []),
                         meta=dict(d.get("meta") or {}), id=d["id"], qty=d["qty"], mark=d["mark"], peak=d["peak"],
                         stop=d["stop"], target=d["target"], realized=d["realized"], fees=d["fees"],
                         fills=list(d.get("fills") or []), status=d.get("status", "open"),
                         closed_ts=d.get("closed_ts"), exit_reason=d.get("exit_reason"))


class EJournal:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(SCHEMA)

    def save(self, pos: ComboPosition, now: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO e_positions VALUES (?,?,?,?,?,?,?)",
                        (pos.id, pos.status, pos.meta.get("symbol"), pos.meta.get("structure"), pos.opened_ts, now,
                         json.dumps(pos_to_dict(pos))))
        self.db.commit()

    def open_positions(self) -> tuple[list[ComboPosition], list[str]]:
        ok, bad = [], []
        for pid, data in self.db.execute("SELECT id, data FROM e_positions WHERE status='open' ORDER BY opened_ts").fetchall():
            try:
                ok.append(pos_from_dict(json.loads(data)))
            except (ValueError, KeyError, TypeError, IndexError):
                bad.append(pid)
        return ok, bad

    def decision(self, **row) -> None:
        self.db.execute(f"INSERT INTO e_decisions ({','.join(DECISION_COLS)}) VALUES ({','.join('?' * len(DECISION_COLS))})",
                        tuple(row.get(c) for c in DECISION_COLS))
        self.db.commit()

    def traded(self, symbol: str, earnings_date: str, structure: str) -> bool:
        return self.db.execute("SELECT 1 FROM e_decisions WHERE symbol=? AND earnings_date=? AND structure=? "
                               "AND outcome='opened' LIMIT 1", (symbol, earnings_date, structure)).fetchone() is not None

    def decisions(self, day: str | None = None) -> list[dict]:
        cur = self.db.execute("SELECT * FROM e_decisions " + ("WHERE day=? " if day else "") + "ORDER BY id",
                              (day,) if day else ())
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
