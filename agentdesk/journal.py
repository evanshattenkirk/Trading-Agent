"""SQLite trade journal (one row per closed position, fills as JSON)."""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("agentdesk.journal")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session TEXT, mode TEXT, contract TEXT, occ TEXT, setup TEXT,
  qty INTEGER, entry REAL, opened_ts REAL, closed_ts REAL,
  realized REAL, fees REAL, pnl REAL, pnl_pct REAL, peak REAL,
  exit_reason TEXT, strike_reason TEXT, entry_reasons TEXT, fills TEXT, l2 TEXT,
  book TEXT DEFAULT 'A', legs TEXT, max_loss REAL, crew TEXT
);
CREATE TABLE IF NOT EXISTS crew_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, ts REAL, kind TEXT, book TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS option_quotes (
  ts REAL, expiry TEXT, strike REAL, right TEXT, bid REAL, ask REAL, spot REAL
);
CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, ts REAL, desk TEXT, brief TEXT
);
CREATE TABLE IF NOT EXISTS iv_history (
  day TEXT, ts REAL, symbol TEXT, kind TEXT, expiry TEXT, dte INTEGER, strike REAL, spot REAL,
  call_bid REAL, call_ask REAL, call_iv REAL, put_bid REAL, put_ask REAL, put_iv REAL, atm_iv REAL,
  earnings_date TEXT, earnings_timing TEXT, T INTEGER, PRIMARY KEY (day, symbol, kind)
);
CREATE TABLE IF NOT EXISTS rh_calls (
  ts REAL, tool TEXT, ms REAL, ok INTEGER, kind TEXT, err TEXT, tag TEXT
);
CREATE INDEX IF NOT EXISTS option_quotes_expiry_ts ON option_quotes(expiry, ts);
CREATE INDEX IF NOT EXISTS option_quotes_ts ON option_quotes(ts);
CREATE INDEX IF NOT EXISTS rh_calls_tag_ts ON rh_calls(tag, ts);
"""
IV_COLS = ("day", "ts", "symbol", "kind", "expiry", "dte", "strike", "spot", "call_bid", "call_ask", "call_iv",
           "put_bid", "put_ask", "put_iv", "atm_iv", "earnings_date", "earnings_timing", "T")


def use_wal(db: sqlite3.Connection) -> None:
    """The engine and the standalone recorder write the same file while the weekly report reads it: WAL lets readers
    and one writer work at once. The mode sticks to the file; a switch that can't get the lock is retried next open."""
    try:
        db.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as ex:
        log.warning("journal: could not switch to WAL (%s); staying in the current mode", ex)


class Journal:
    def __init__(self, path: Path | str | None, timeout: float = 30.0):
        self.db = sqlite3.connect(str(path) if path else ":memory:", timeout=timeout, check_same_thread=False)
        if path:
            use_wal(self.db)
        self.db.executescript(SCHEMA)
        for col in ("l2 TEXT", "book TEXT DEFAULT 'A'", "legs TEXT", "max_loss REAL", "crew TEXT"):
            try:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass

    def record_trade(self, session: str, mode: str, p, book: str = "A") -> None:
        d = p.to_dict()
        basis = getattr(p, "risk_basis", None) or p.entry * 100 * p.qty_initial
        crew = getattr(p, "crew", None) or (getattr(p, "meta", None) or {}).get("crew")     # what the crew did to it
        try:
            self._insert_trade(session, mode, p, book, d, basis, crew)
            self.db.commit()
        except Exception:
            try:
                self.db.rollback()      # a failed INSERT or COMMIT must not leave the row pending, or a retry writes it twice
            except Exception:
                pass
            raise

    def _insert_trade(self, session, mode, p, book, d, basis, crew) -> None:
        self.db.execute(
            "INSERT INTO trades (session,mode,contract,occ,setup,qty,entry,opened_ts,closed_ts,realized,fees,pnl,pnl_pct,peak,"
            "exit_reason,strike_reason,entry_reasons,fills,l2,book,legs,max_loss,crew) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session, mode, d["contract"], d["occ"], p.setup, p.qty_initial, p.entry, p.opened_ts, p.closed_ts,
             p.realized, p.fees, p.realized - p.fees, (p.realized - p.fees) / basis if basis else 0, p.peak,
             p.exit_reason, p.strike_reason, json.dumps(p.entry_reasons), json.dumps(p.fills), json.dumps(p.l2),
             book, json.dumps(d["legs"]) if "legs" in d else None, getattr(p, "risk_basis", None),
             json.dumps(crew) if crew else None))

    def record_quotes(self, rows: list[tuple]) -> None:
        if rows:
            self.db.executemany("INSERT INTO option_quotes VALUES (?,?,?,?,?,?,?)", rows)
            self.db.commit()

    def record_brief(self, session: str, ts: float, desk: str, brief: dict) -> None:
        self.db.execute("INSERT INTO briefs (session,ts,desk,brief) VALUES (?,?,?,?)", (session, ts, desk, json.dumps(brief)))
        self.db.commit()

    def record_crew(self, session: str, ts: float, kind: str, book: str | None, detail: dict) -> None:
        """What the crew did: a huddle's directive, an entry it blocked, a calendar disagreement, the day's usage."""
        self.db.execute("INSERT INTO crew_log (session,ts,kind,book,detail) VALUES (?,?,?,?,?)",
                        (session, ts, kind, book, json.dumps(detail, default=str)))
        self.db.commit()

    def crew_log(self, session: str | None = None) -> list[dict]:
        q = "SELECT session,ts,kind,book,detail FROM crew_log" + (" WHERE session=?" if session else "") + " ORDER BY id"
        rows = self.db.execute(q, (session,) if session else ()).fetchall()
        return [{"session": r[0], "ts": r[1], "kind": r[2], "book": r[3], "detail": json.loads(r[4])} for r in rows]

    def latest_straddle(self, expiry: str, spot: float, since: float) -> tuple[float, float] | None:
        """(strike, call mid + put mid) at the strike nearest spot from the newest recorded 0DTE quotes since `since`."""
        rows = self.db.execute("SELECT ts,strike,right,bid,ask FROM option_quotes WHERE expiry=? AND ts>=? "
                               "ORDER BY ts DESC LIMIT 400", (expiry, since)).fetchall()
        if not rows:
            return None
        k = min({r[1] for r in rows}, key=lambda x: abs(x - spot))
        mids = {}
        for ts, strike, right, bid, ask in rows:
            if strike == k and right not in mids and bid and ask and ask >= bid > 0:
                mids[right] = (bid + ask) / 2
        if "call" not in mids or "put" not in mids:
            return None
        return k, round(mids["call"] + mids["put"], 4)

    def record_iv(self, rows: list[dict]) -> None:
        """Book E's end-of-day ATM IV rows; one per (day, symbol, kind), so a rerun replaces."""
        if rows:
            self.db.executemany(f"INSERT OR REPLACE INTO iv_history ({','.join(IV_COLS)}) "
                                f"VALUES ({','.join('?' * len(IV_COLS))})", [tuple(r.get(c) for c in IV_COLS) for r in rows])
            self.db.commit()

    def iv_history(self, symbol: str, kind: str, T: int, before_event: str) -> list[float]:
        """ATM IV recorded at the same T in earlier earnings cycles (reports before `before_event`), oldest first."""
        cur = self.db.execute("SELECT atm_iv FROM iv_history WHERE symbol=? AND kind=? AND T=? AND earnings_date<? "
                              "AND atm_iv IS NOT NULL ORDER BY day", (symbol, kind, T, before_event))
        return [r[0] for r in cur.fetchall()]

    def trades(self, session: str | None = None, limit: int = 500) -> list[dict]:
        cur = self.db.execute(
            "SELECT * FROM trades " + ("WHERE session=? " if session else "") + "ORDER BY id DESC LIMIT ?",
            (session, limit) if session else (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
