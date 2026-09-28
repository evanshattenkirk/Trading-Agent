"""Book B, iron fly (HANDOFF 7B, kept exactly as Evan specified).

08:45 CT: sell the ATM call and put, buy wings +-$5, one 4-leg credit order. Skip days with a high-impact event
before 14:00 CT and days the Vol desk flags VIX1D more than 3 points above VIX. Take profit at 50% of credit; stop
when the debit to close reaches 2x credit (a loss of 1x credit); close by 14:30 CT. 1 lot.
"""
from __future__ import annotations

from ..clock import ct_time
from ..config import hhmm
from .base import Leg, OrderIntent, Skip, Strategy, credit_exit, mins


class IronFly(Strategy):
    name = "IRON FLY"

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
        if mins(t) - mins(start) > self.c.get("entry_grace_min", 5):
            return Skip(f"missed the {self.c['entry_ct']} CT entry")
        cutoff = hhmm(self.c.get("skip_event_before_ct", "14:00"))
        ev = [n for ts, n in ctx.events if ct_time(ts) < cutoff]
        if ev:
            return Skip(f"high-impact event before {self.c.get('skip_event_before_ct', '14:00')} CT: {ev[0]}")
        if ctx.vix1d_flag:
            return Skip(f"Vol desk: VIX1D more than {self.c.get('vix1d_gap', 3):g} points above VIX")
        if not ctx.spot:
            return Skip("no SPY price")
        k, w = float(round(ctx.spot)), float(self.c["wings"])
        notes = [] if ctx.vix1d_flag is not None else ["VIX1D unavailable: check skipped"]
        return OrderIntent([Leg("call", k, "sell"), Leg("put", k, "sell"), Leg("call", k + w, "buy"), Leg("put", k - w, "buy")],
                           credit=True, width=w, reason=f"ATM {k:g} iron fly, wings +-{w:g}",
                           lots=int(self.c.get("lots", 1)), meta={"notes": notes})

    def entry_failed(self, now) -> None:
        self.decided = None            # try again next second; the grace window still bounds it

    def on_quote(self, pos, cq, now, ctx):
        return credit_exit(pos, cq.mid(True), now, self.c)

    def plan(self, pos) -> str:
        return (f"take profit at {pos.entry * (1 - self.c['take_profit_pct']):.2f} debit, stop at "
                f"{pos.entry * self.c['stop_debit_x_credit']:.2f}, close {self.c['close_ct']} CT")
