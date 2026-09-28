"""Interfaces shared by the paper books (HANDOFF section 9)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..clock import ct_time
from ..config import hhmm


@dataclass(frozen=True)
class Leg:
    right: str              # "call" | "put"
    strike: float
    side: str               # side at open: "sell" | "buy"
    ratio: int = 1


@dataclass
class OrderIntent:
    legs: list[Leg]         # sold legs first for a credit structure, so the order's direction comes out right
    credit: bool
    width: float            # widest wing in $: max loss per share = width - credit
    reason: str
    lots: int | None = None         # fixed lots (B, D)
    budget: float | None = None     # size from max loss: floor(budget / max loss per lot) (C)
    meta: dict = field(default_factory=dict)


@dataclass
class ExitIntent:
    reason: str
    urgent: bool = False    # urgent: start at natural; otherwise work from mid


@dataclass
class Skip:
    reason: str


@dataclass
class MarketContext:
    now: float
    day: date
    spot: float | None
    vwap: float | None
    events: list = field(default_factory=list)      # [(event ts, name)] high-impact events today
    vix_prev: float | None = None
    vix1d_flag: bool | None = None                  # None: no VIX1D source today
    size_mult: float = 1.0                          # crew cut, <= 1.0
    open: list = field(default_factory=list)        # this book's open positions


class Strategy:
    name = "strategy"

    def __init__(self, cfg: dict):
        self.c = cfg

    def warmup(self, bars_1m: list) -> None: ...

    def new_day(self, day) -> None: ...

    def on_bar(self, tf: str, bar, ctx: MarketContext):
        return None

    def on_clock(self, now: float, ctx: MarketContext):
        return None

    def on_quote(self, pos, cq, now: float, ctx: MarketContext):
        return None

    def on_closed(self, pos, now: float) -> None: ...

    def entry_failed(self, now: float) -> None:
        """The host couldn't place the intent for a transient reason (quote, fill)."""

    def plan(self, pos) -> str:
        return ""


def mins(t) -> int:
    return t.hour * 60 + t.minute


def credit_exit(pos, close_mid: float, now: float, c: dict) -> ExitIntent | None:
    """B and D: take profit when the debit to close is <= (1 - tp) x credit; stop when it reaches
    stop_debit_x_credit x credit (a loss of 1x credit at 2.0); close at close_ct."""
    if close_mid <= pos.entry * (1 - c["take_profit_pct"]):
        return ExitIntent(f"take profit {int(round(c['take_profit_pct'] * 100))}%")
    if close_mid >= pos.entry * c["stop_debit_x_credit"]:
        return ExitIntent(f"stop: debit {c['stop_debit_x_credit']:g}x credit", urgent=True)
    if ct_time(now) >= hhmm(c["close_ct"]):
        return ExitIntent(f"close {c['close_ct']} CT")
    return None
