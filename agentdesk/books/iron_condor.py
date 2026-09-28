"""Book D, 10:00 ET iron condor (HANDOFF 7D, quiet filter per Evan's 2026-09-28 answer).

09:00 CT: short call and put at +-0.9x the remaining-session expected move from the prior VIX close, wings $2
beyond, one 4-leg credit order. Quiet filter: the 08:30-09:00 CT range (over that window's last close) is below its
trailing 14-day median, and price is within 0.12% of VWAP. Take profit 50%; stop at a 2x-credit debit; close 14:25.
"""
from __future__ import annotations

import math
import statistics

from ..clock import ct_time, session_date
from ..config import hhmm
from .base import Leg, OrderIntent, Skip, Strategy, credit_exit, mins

OPEN_MIN = 8 * 60 + 30
CLOSE_MIN = 15 * 60


def expected_move(spot: float, vix: float, now: float, rth_mult: float = 0.80) -> float:
    """strategies_bcd.sd_left: remaining RTH share of the VIX-implied day plus the 15-minute SPY 0DTE tail."""
    t = ct_time(now)
    left = max(0.0, (CLOSE_MIN - (mins(t) + t.second / 60)) / 390)
    return rth_mult * vix / 100 / math.sqrt(252) * math.sqrt(left + 15 / 390) * spot


class IronCondor(Strategy):
    name = "IRON CONDOR"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.decided = None
        self.days: dict = {}        # date -> [high, low, last close, complete] over the first quiet_window_min
        self.seen: dict = {}        # date -> minutes of that window that had a bar (complete = all of them)

    def warmup(self, bars_1m: list) -> None:
        for b in bars_1m:
            self._track(b)

    def new_day(self, day) -> None:
        self.decided = None

    def on_bar(self, tf, bar, ctx):
        if tf == "1m":
            self._track(bar)
        return None

    def _track(self, b) -> None:
        if b.tf != "1m":
            return
        w = self.c.get("quiet_window_min", 30)
        m = mins(ct_time(b.t)) - OPEN_MIN
        if not 0 <= m < w:
            return
        d = session_date(b.t)
        seen = self.seen.setdefault(d, set())
        seen.add(m)
        cur = self.days.get(d)
        if cur is None:
            self.days[d] = cur = [b.h, b.l, b.c, False]
        else:
            cur[0], cur[1], cur[2] = max(cur[0], b.h), min(cur[1], b.l), b.c
        cur[3] = len(seen) == w         # a partial window (late start, gap) never counts as a quiet reading

    def _rng(self, d) -> float:
        h, l, c, _ = self.days[d]
        return (h - l) / c

    def on_clock(self, now, ctx):
        if self.decided == ctx.day:
            return None
        t, start = ct_time(now), hhmm(self.c["entry_ct"])
        if t < start:
            return None
        late = mins(t) - mins(start) > self.c.get("entry_grace_min", 5)
        quiet = self.c.get("quiet_filter", True)
        today = self.days.get(ctx.day)
        if quiet and not (today and today[3]):
            if not late:
                return None                 # the 08:59 bar hasn't closed yet
            self.decided = ctx.day
            w = self.c.get("quiet_window_min", 30)
            have = len(self.seen.get(ctx.day, ()))
            return Skip(f"quiet filter needs all {w} minutes of 08:30-09:00 CT bars, have {have}")
        self.decided = ctx.day
        if late:
            return Skip(f"missed the {self.c['entry_ct']} CT entry")
        if ctx.vix_prev is None:
            return Skip("no prior VIX close")
        if not ctx.spot:
            return Skip("no SPY price")
        notes = []
        if quiet:
            n = self.c.get("quiet_lookback_days", 14)
            past = [self._rng(d) for d in sorted(d for d, v in self.days.items() if d < ctx.day and v[3])][-n:]
            if len(past) < n:
                return Skip(f"quiet filter needs {n} prior days, have {len(past)}")
            rng, med = self._rng(ctx.day), statistics.median(past)
            if rng >= med:
                return Skip(f"not quiet: 08:30-09:00 range {rng:.2%} >= {n}-day median {med:.2%}")
            vmax = self.c.get("vwap_max_pct", 0.0012)
            if ctx.vwap is None or abs(ctx.spot / ctx.vwap - 1) > vmax:
                return Skip(f"price more than {vmax:.2%} from VWAP")
            notes.append(f"quiet: range {rng:.2%} < median {med:.2%}")
        em = expected_move(ctx.spot, ctx.vix_prev, now, self.c.get("em_rth_mult", 0.80))
        d = self.c["short_em_mult"] * em
        kc, kp, w = float(math.ceil(ctx.spot + d)), float(math.floor(ctx.spot - d)), float(self.c["wings"])
        return OrderIntent([Leg("call", kc, "sell"), Leg("put", kp, "sell"), Leg("call", kc + w, "buy"), Leg("put", kp - w, "buy")],
                           credit=True, width=w, reason=f"shorts {kp:g}P/{kc:g}C at +-{self.c['short_em_mult']:g} EM "
                           f"(EM ${em:.2f}, prior VIX {ctx.vix_prev:.2f})", lots=int(self.c.get("lots", 1)),
                           meta={"notes": notes, "em": round(em, 3)})

    def entry_failed(self, now) -> None:
        self.decided = None

    def on_quote(self, pos, cq, now, ctx):
        return credit_exit(pos, cq.mid(True), now, self.c)

    def plan(self, pos) -> str:
        return (f"take profit at {pos.entry * (1 - self.c['take_profit_pct']):.2f} debit, stop at "
                f"{pos.entry * self.c['stop_debit_x_credit']:.2f}, close {self.c['close_ct']} CT")
