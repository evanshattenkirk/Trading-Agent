"""Multi-leg quotes and positions.

Prices are in the structure's own sign: a credit structure quotes the credit it collects (and the debit to
buy it back), a debit structure the debit it pays. natural = every leg at its touch (sell at bid, buy at ask).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from ..exits import Contract
from .base import Leg

_ids = itertools.count(1)


def leg_problem(q, now: float, max_age: float, max_spread_pct: float, tick_exempt: float, opening: bool) -> str | None:
    """Why a leg quote can't be used, or None. Entries need the full check; an open position only needs a fresh,
    uncrossed quote with an ask, so a zero-bid or wide wing never blocks a stop check."""
    if q is None:
        return "no quote"
    if now - q.ts > max_age:
        return f"stale ({now - q.ts:.0f}s)"
    if q.ask <= 0:
        return "zero ask"
    if q.bid > q.ask:
        return "crossed"
    if opening:
        if q.bid <= 0:
            return "zero bid"
        spread, mid = q.ask - q.bid, (q.ask + q.bid) / 2
        if spread > tick_exempt + 1e-9 and spread > max_spread_pct * mid:
            return f"spread {spread / mid:.0%} of mid"
    return None


@dataclass
class ComboQuote:
    legs: list[Leg]
    quotes: list
    problem: str | None = None

    def mid(self, credit: bool) -> float:
        v = sum((1 if l.side == "sell" else -1) * l.ratio * (q.bid + q.ask) / 2 for l, q in zip(self.legs, self.quotes))
        return round(v if credit else -v, 4)

    def half_spread(self) -> float:
        return sum(l.ratio * (q.ask - q.bid) / 2 for l, q in zip(self.legs, self.quotes))

    def natural(self, credit: bool, opening: bool) -> float:
        receiving = credit == opening
        m, h = self.mid(credit), self.half_spread()
        return round(m - h if receiving else m + h, 4)


def paper_fair(cq: ComboQuote, credit: bool, opening: bool, model: str, cents_per_leg: float) -> float:
    """The price a paper combo order fills at: natural, or mid -/+ cents per leg, never worse than natural."""
    mid, nat = cq.mid(credit), cq.natural(credit, opening)
    if model == "natural":
        return round(nat, 2)
    n = sum(l.ratio for l in cq.legs)
    receiving = credit == opening
    f = mid - cents_per_leg * n if receiving else mid + cents_per_leg * n
    f = max(f, nat) if receiving else min(f, nat)
    return round(f, 2)


@dataclass
class ComboPosition:
    book: str                   # "B"
    setup: str                  # "IRON FLY"
    legs: list[Leg]
    contracts: list[Contract]   # same order as legs
    qty_initial: int
    entry: float                # fill: credit received (credit) or debit paid, per share
    credit: bool
    width: float
    opened_ts: float
    strike_reason: str = ""
    entry_reasons: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    id: str = ""
    qty: int = 0
    mark: float = 0.0           # mid price to close
    peak: float = 0.0           # best mark seen (lowest debit for a credit structure)
    stop: float = 0.0           # display: close price that stops out (0 = not price-based)
    target: float = 0.0         # display: close price that takes profit
    realized: float = 0.0
    fees: float = 0.0
    fills: list = field(default_factory=list)
    status: str = "open"
    closed_ts: float | None = None
    exit_reason: str | None = None
    last_quote_ts: float = 0.0  # last time the host saw usable quotes on every leg (safety watchdog)
    l2: dict | None = None
    exiting: bool = False

    def __post_init__(self):
        self.id = self.id or f"{self.book}{next(_ids)}"
        self.qty = self.qty or self.qty_initial
        self.mark = self.mark or self.entry
        self.peak = self.peak or self.entry
        self.last_quote_ts = self.last_quote_ts or self.opened_ts

    def pnl_per_share(self, close_px: float) -> float:
        return (self.entry - close_px) if self.credit else (close_px - self.entry)

    def _risk(self, qty: int) -> float:
        per = (self.width - self.entry) if self.credit else self.entry
        return round(max(0.0, per) * 100 * qty, 2)

    @property
    def max_loss(self) -> float:
        return self._risk(self.qty)

    @property
    def risk_basis(self) -> float:
        return self._risk(self.qty_initial)

    @property
    def unrealized(self) -> float:
        return self.pnl_per_share(self.mark) * 100 * self.qty

    @property
    def total_pnl(self) -> float:
        return self.realized + self.unrealized - self.fees

    @property
    def pnl_pct(self) -> float:
        return self.total_pnl / self.risk_basis if self.risk_basis else 0.0

    @property
    def label(self) -> str:
        c = self.contracts[0]
        legs = "/".join(f"{'-' if l.side == 'sell' else '+'}{l.strike:g}{l.right[0].upper()}" for l in self.legs)
        return f"{c.symbol} {legs} {c.expiry[5:]}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "book": self.book, "contract": self.label, "occ": ",".join(c.occ for c in self.contracts),
            "setup": self.setup, "qty": self.qty, "qty_initial": self.qty_initial, "entry": round(self.entry, 2),
            "mark": round(self.mark, 3), "peak": round(self.peak, 3), "stop": round(self.stop, 2),
            "target": round(self.target, 2), "credit": self.credit, "width": self.width,
            "max_loss": self.max_loss if self.status == "open" else self.risk_basis,
            "pnl_pct": round(self.pnl_pct * 100, 1), "realized": round(self.realized, 2),
            "unrealized": round(self.unrealized, 2), "total_pnl": round(self.total_pnl, 2), "fees": round(self.fees, 2),
            "opened_ts": self.opened_ts, "closed_ts": self.closed_ts, "status": self.status,
            "exit_reason": self.exit_reason, "strike_reason": self.strike_reason, "entry_reasons": self.entry_reasons,
            "fills": self.fills, "l2": self.l2,
            "legs": [{"right": l.right, "strike": l.strike, "side": l.side, "ratio": l.ratio} for l in self.legs],
            "meta": {k: v for k, v in self.meta.items() if isinstance(v, (int, float, str))},
        }
