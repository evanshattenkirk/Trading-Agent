"""One paper book's day state: open positions, trades, P&L and its own gates."""
from __future__ import annotations

from collections import deque

from ..clock import session_date


def todays_trades(journal, today: str, letter: str) -> list[dict]:
    """Today's closed trades for one book from the journal (book 'F' rows read as F1)."""
    names = {letter, "F"} if letter == "F1" else {letter}
    try:
        rows = journal.trades(session=today, limit=10000)
    except Exception:
        return []
    return [r for r in rows if (r.get("book") or "A") in names]


class Book:
    def __init__(self, key: str, cfg: dict, strategy):
        # the book id is the config key's prefix: A_macd_calls -> A, F1_stocks_in_play -> F1
        self.key, self.letter, self.c, self.strategy = key, key.split("_", 1)[0], cfg, strategy
        self.max_trades = int(cfg.get("max_trades_day", 1))
        self.open: list = []
        self.skips: deque = deque(maxlen=30)
        self.errors = 0             # the longest current failure streak among the kinds of call below
        self.streaks: dict = {}     # kind of call (clock check, position check, bar hook...) -> failures in a row
        self.entering = False
        self.reset_day()

    def succeeded(self, kind: str) -> None:
        """A clean call clears only its own kind's streak: a working clock check must not hide failing exits."""
        self.streaks.pop(kind, None)
        self.errors = max(self.streaks.values(), default=0)

    def failed(self, kind: str) -> int:
        self.streaks[kind] = self.streaks.get(kind, 0) + 1
        self.errors = max(self.streaks.values())
        return self.errors

    def reset_day(self) -> None:
        self.streaks, self.errors = {}, 0     # yesterday's streaks must not halt today's book on its first error
        self.closed: list = []
        self.trades = self.wins = self.losses = 0
        self.day_pnl = 0.0
        self.halted, self.halt_reason, self.blocked = False, None, None

    def can_enter(self) -> tuple[bool, str]:
        if self.halted:
            return False, f"book halted: {self.halt_reason}"
        if self.blocked:
            return False, self.blocked
        if self.open:
            return False, "position already open"
        if self.trades >= self.max_trades:
            return False, f"max {self.max_trades} trades today"
        return True, "ok"

    def on_open(self, pos) -> None:
        self.open.append(pos)
        self.trades += 1

    def on_close(self, pos, net: float) -> None:
        self.open = [p for p in self.open if p.id != pos.id]
        self.closed.append(pos)
        self.day_pnl += net
        if net >= 0:
            self.wins += 1
        else:
            self.losses += 1

    def restore_day(self, rows: list[dict], today: str) -> float:
        """A restart mid-day (Evan, 2026-10-01): today's closed trades from the journal count toward today's P&L,
        wins and losses, and the ones opened today toward the trade limit. Returns their net for the account."""
        total = 0.0
        for r in rows:
            net = float(r.get("pnl") or 0.0)
            total += net
            self.day_pnl += net
            if net >= 0:
                self.wins += 1
            else:
                self.losses += 1
            if r.get("opened_ts") and str(session_date(float(r["opened_ts"]))) == today:
                self.trades += 1
        return total

    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted, self.halt_reason = True, reason

    def to_dict(self) -> dict:
        unreal = sum(p.unrealized - p.fees for p in self.open)
        return {"book": self.letter, "key": self.key, "name": self.strategy.name, "day_pnl": round(self.day_pnl, 2),
                "open_pnl": round(unreal, 2), "trades": self.trades, "max_trades": self.max_trades, "wins": self.wins,
                "losses": self.losses, "halted": self.halted, "halt_reason": self.halt_reason, "blocked": self.blocked,
                "open": [p.to_dict() for p in self.open], "closed": [p.to_dict() for p in self.closed],
                "last_skip": self.skips[-1] if self.skips else None}
