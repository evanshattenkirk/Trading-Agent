"""Book F tables in the engine's SQLite journal (docs/BOOK_F_HANDOFF.md section 4.4).

  f_scans          every scanned name: RVOL5, first candle, OR high/low, ATR14, rank, picked, news tag, why not picked
  f_trades         every F position, open or closed, with its R multiple. Open rows are the restart check: a row
                   still open from an earlier session, or after the exit time, means shares were held too long
  f_shadow_shorts  would-be shorts on red first candles (hypothetical P&L; never an order)
F trades also go into `trades` with book = 'F1' (pnl_pct there is the R multiple: the basis is the initial risk).
"""
from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS f_scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, ts REAL, symbol TEXT, rvol5 REAL, direction TEXT,
  first_open REAL, first_close REAL, or_high REAL, or_low REAL, atr REAL, dollar_vol20 REAL, rank INTEGER,
  picked INTEGER, reason TEXT, ai INTEGER, news TEXT, news_ts REAL, news_late INTEGER, status TEXT
);
CREATE TABLE IF NOT EXISTS f_trades (
  id TEXT PRIMARY KEY, session TEXT, mode TEXT, symbol TEXT, qty INTEGER, entry REAL, stop REAL, atr REAL,
  or_high REAL, rvol5 REAL, rank INTEGER, ai INTEGER, opened_ts REAL, status TEXT, closed_ts REAL, exit_px REAL,
  exit_reason TEXT, pnl REAL, risk REAL, r REAL, news TEXT, fills TEXT
);
CREATE TABLE IF NOT EXISTS f_shadow_shorts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, symbol TEXT, rvol5 REAL, rank INTEGER, or_low REAL, atr REAL,
  entry_ts REAL, entry REAL, stop REAL, qty INTEGER, exit_ts REAL, exit_px REAL, exit_reason TEXT, pnl REAL, r REAL
);
"""


class FJournal:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(SCHEMA)

    def _rows(self, sql: str, args=()) -> list[dict]:
        cur = self.db.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------ scans
    def record_scan(self, session: str, ts: float, rows) -> None:
        from .f_stocks_in_play import AI_LIST
        self.db.executemany(
            "INSERT INTO f_scans (session,ts,symbol,rvol5,direction,first_open,first_close,or_high,or_low,atr,dollar_vol20,"
            "rank,picked,reason,ai,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(session, ts, r.symbol, r.rvol5, r.direction, r.open, r.close, r.or_high, r.or_low, r.atr, r.dollar_vol20,
              r.rank, int(r.picked), r.reason, int(r.symbol in AI_LIST), "armed" if r.picked else None) for r in rows])
        self.db.commit()

    def set_news(self, session: str, symbol: str, news: dict, ts: float, late: bool) -> None:
        self.db.execute("UPDATE f_scans SET news=?, news_ts=?, news_late=? WHERE session=? AND symbol=?",
                        (json.dumps(news), ts, int(late), session, symbol))
        self.db.execute("UPDATE f_trades SET news=? WHERE session=? AND symbol=?", (json.dumps(news), session, symbol))
        self.db.commit()

    def set_status(self, session: str, symbol: str, status: str) -> None:
        self.db.execute("UPDATE f_scans SET status=? WHERE session=? AND symbol=?", (status, session, symbol))
        self.db.commit()

    def scans(self, session: str | None = None, since: str | None = None) -> list[dict]:
        if session:
            return self._rows("SELECT * FROM f_scans WHERE session=? ORDER BY id", (session,))
        return self._rows("SELECT * FROM f_scans WHERE session>=? ORDER BY id", (since or "",))

    # ------------------------------------------------------------ positions / trades
    def open_position(self, session: str, mode: str, p) -> None:
        from .f_stocks_in_play import AI_LIST
        self.db.execute(
            "INSERT OR REPLACE INTO f_trades (id,session,mode,symbol,qty,entry,stop,atr,or_high,rvol5,rank,ai,opened_ts,status,"
            "risk,news,fills) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.id, session, mode, p.symbol, p.qty, p.entry, p.stop, p.atr, p.or_high, p.rvol5, p.rank,
             int(p.symbol in AI_LIST), p.opened_ts, "open", p.risk, json.dumps(p.news), json.dumps(p.fills)))
        self.db.commit()

    def close_position(self, p) -> None:
        self.db.execute("UPDATE f_trades SET status=?, closed_ts=?, exit_px=?, exit_reason=?, pnl=?, r=?, fills=? WHERE id=?",
                        ("closed", p.closed_ts, p.exit_px, p.exit_reason, p.pnl, p.r_multiple, json.dumps(p.fills), p.id))
        self.db.commit()

    def open_rows(self) -> list[dict]:
        return self._rows("SELECT * FROM f_trades WHERE status='open' ORDER BY opened_ts")

    def clear_stale(self, before_session: str) -> int:
        """Mark open rows from sessions before `before_session` as closed without P&L (after Evan checked them)."""
        cur = self.db.execute("UPDATE f_trades SET status='cleared', exit_reason='cleared by hand' "
                              "WHERE status='open' AND session<?", (before_session,))
        self.db.commit()
        return cur.rowcount

    def trades(self, since: str | None = None) -> list[dict]:
        return self._rows("SELECT * FROM f_trades WHERE status='closed' AND session>=? ORDER BY closed_ts", (since or "",))

    # ------------------------------------------------------------ shadow shorts
    def record_shadow(self, session: str, s: dict) -> None:
        self.db.execute(
            "INSERT INTO f_shadow_shorts (session,symbol,rvol5,rank,or_low,atr,entry_ts,entry,stop,qty,exit_ts,exit_px,"
            "exit_reason,pnl,r) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session, s["symbol"], s["rvol5"], s["rank"], s["or_low"], s["atr"], s["entry_ts"], s["entry"], s["stop"],
             s["qty"], s["exit_ts"], s["exit_px"], s["exit_reason"], s["pnl"], s["r"]))
        self.db.commit()

    def shadows(self, since: str | None = None) -> list[dict]:
        return self._rows("SELECT * FROM f_shadow_shorts WHERE session>=? ORDER BY id", (since or "",))


class TradeRow:
    """Adapter so Journal.record_trade can store an F position in `trades` (book 'F1')."""

    def __init__(self, p):
        self.p = p
        self.setup, self.qty_initial, self.entry = "F1", p.qty, p.entry
        self.opened_ts, self.closed_ts, self.realized, self.fees = p.opened_ts, p.closed_ts, p.pnl, 0.0
        self.peak, self.exit_reason, self.l2 = None, p.exit_reason, None
        self.strike_reason = f"OR high {p.or_high:.2f}, RVOL5 {p.rvol5 or 0:.2f}, rank {p.rank}"
        self.entry_reasons = [f"{p.qty} sh, stop {p.stop:.2f} (0.10 x ATR {p.atr:.2f}), risk ${p.risk:.2f}"]
        self.fills, self.risk_basis = p.fills, p.risk

    def to_dict(self) -> dict:
        return {"contract": f"{self.p.symbol} shares", "occ": self.p.symbol}
