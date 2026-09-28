"""Book G, 10:00 ET SPY 0DTE/1DTE ATM call calendar (research candidate F3, research/strategies_new_prereg.md,
rules frozen; the write-up's 1 lot and $3.00 debit cap). Evan approved it as a paper book on 2026-09-28.

Monday-Thursday, only when the next session is the next calendar day. 09:00 CT: sell today's call and buy
tomorrow's call, both at round(SPY), one 2-leg debit order. Take profit at +25% of the debit, stop at -35%,
otherwise close both legs at 14:25 CT, so the short leg is never held into 15:30 ET.
"""
from __future__ import annotations

from datetime import timedelta

from ..clock import ct_time
from ..config import hhmm
from .base import ExitIntent, Leg, OrderIntent, Skip, Strategy, mins


class CallCalendar(Strategy):
    name = "CALL CALENDAR"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.decided = None

    def new_day(self, day) -> None:
        self.decided = None

    def on_clock(self, now, ctx):
        if self.decided == ctx.day:
            return None
        t, start = ct_time(now), hhmm(self.c["entry_ct"])
        if t < start:
            return None
        self.decided = ctx.day
        if ctx.day.weekday() > 3:
            return Skip("F3 trades Monday to Thursday only")
        if ctx.day + timedelta(days=1) in self.holidays:
            return Skip("next session is not tomorrow (holiday)")
        if mins(t) - mins(start) > self.c.get("entry_grace_min", 5):
            return Skip(f"missed the {self.c['entry_ct']} CT entry")
        if not ctx.spot:
            return Skip("no SPY price")
        k = float(round(ctx.spot))
        return OrderIntent([Leg("call", k, "sell", dte=0), Leg("call", k, "buy", dte=1)], credit=False, width=0.0,
                           reason=f"{k:g}C calendar: sell today, buy tomorrow (SPY {ctx.spot:.2f})",
                           lots=int(self.c.get("lots", 1)), max_price=float(self.c["max_debit"]))

    def entry_failed(self, now) -> None:
        self.decided = None

    def on_quote(self, pos, cq, now, ctx):
        v = cq.mid(False)
        if v >= pos.entry * (1 + self.c["take_profit_pct"]):
            return ExitIntent(f"take profit +{int(round(self.c['take_profit_pct'] * 100))}%")
        if v <= pos.entry * (1 - self.c["stop_pct"]):
            return ExitIntent(f"stop -{int(round(self.c['stop_pct'] * 100))}%", urgent=True)
        if ct_time(now) >= hhmm(self.c["close_ct"]):
            return ExitIntent(f"close {self.c['close_ct']} CT")
        return None

    def plan(self, pos) -> str:
        return (f"take profit at {pos.entry * (1 + self.c['take_profit_pct']):.2f}, stop at "
                f"{pos.entry * (1 - self.c['stop_pct']):.2f}, close {self.c['close_ct']} CT")
