"""Book C, ChatGPT's bullish 30-min ORB -> $2 bull-put credit spread (HANDOFF 7C, frozen; expected to lose).

On each 5m close from 09:00 to 13:30 CT: close above the 08:30-09:00 high with the prior close at or below it,
above VWAP, EMA20 above EMA20 three bars ago, RSI(14) 55-72, green candle, volume >= 0.8x the median of the prior
20 bars. Short put ~1 strike below spot, long put 2 lower; size floor(budget / max loss). Exits on SPY, first wins:
-0.18%, +0.45%, 5m MACD cross below signal, 45 minutes, 14:15 CT. At most 2 trades a day, one at a time.
"""
from __future__ import annotations

import math
import statistics
from collections import deque

from ..bars import TimeBarBuilder
from ..clock import ct_time, is_rth, session_date
from ..config import hhmm
from ..indicators import EMA, MACD, RSI
from .base import ExitIntent, Leg, OrderIntent, Strategy, mins

OPEN_MIN = 8 * 60 + 30


class OrbBullPut(Strategy):
    name = "ORB BULL PUT"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.ema, self.rsi, self.macd = EMA(cfg.get("ema", 20)), RSI(14), MACD(12, 26, 9)
        self.emas: deque = deque(maxlen=4)
        self.vols: deque = deque(maxlen=cfg.get("vol_lookback", 20))
        self.prev_close = None
        self.prev_macd = None
        self.orh: dict = {}
        self.or_bars: dict = {}     # date -> 5m bars seen inside the opening range (all of them needed)
        self.last_exit = 0.0
        self._live = False

    def warmup(self, bars_1m: list) -> None:
        b5 = TimeBarBuilder("5m")
        for b in bars_1m:
            if b.tf == "1m" and is_rth(b.t):
                c = b5.on_bar(b)
                if c:
                    self._update(c)

    def _update(self, bar) -> dict:
        """Commit a closed 5m bar (the backtest's indicator series is continuous across days)."""
        m = mins(ct_time(bar.t)) - OPEN_MIN
        if 0 <= m < self.c.get("or_minutes", 30):
            d = session_date(bar.t)
            self.orh[d] = max(self.orh.get(d, bar.h), bar.h)
            self.or_bars.setdefault(d, set()).add(m)
        e, r, mv = self.ema.update(bar.c), self.rsi.update(bar.c), self.macd.update(bar.c)
        if e is not None:
            self.emas.append(e)
        med = statistics.median(self.vols) if len(self.vols) == self.vols.maxlen else None
        self.vols.append(bar.v)
        prev, self.prev_close = self.prev_close, bar.c
        pm, self.prev_macd = self.prev_macd, mv
        down = bool(mv and pm and mv.macd < mv.signal and pm.macd >= pm.signal)
        return {"prev": prev, "ema_up": len(self.emas) == 4 and self.emas[-1] > self.emas[0], "rsi": r, "med": med,
                "macd_down": down}

    def on_bar(self, tf, bar, ctx):
        if tf != "5m":
            return None
        if not self._live:
            self._live = True
            if self.c.get("reset_volume_on_live"):
                self.vols.clear()           # only if history comes from a different feed than live bars
        s = self._update(bar)
        if ctx.open:
            pos = ctx.open[0]
            if s["macd_down"] and bar.end > pos.opened_ts + 1:
                return ExitIntent("5m MACD crossed below signal")
            return None
        if bar.t < self.last_exit:
            return None
        w = self.c.get("window_ct", {"start": "09:00", "end": "13:30"})
        if mins(ct_time(bar.t)) < mins(hhmm(w["start"])) or mins(ct_time(bar.end)) > mins(hhmm(w["end"])):
            return None
        orh = self.orh.get(ctx.day)
        if len(self.or_bars.get(ctx.day, ())) < self.c.get("or_minutes", 30) // 5:
            orh = None                  # a partial opening range (late start) would fake a breakout
        lo, hi = self.c.get("rsi", [55, 72])
        ok = (orh is not None and s["prev"] is not None and bar.c > orh and s["prev"] <= orh
              and ctx.vwap is not None and bar.c > ctx.vwap and s["ema_up"]
              and s["rsi"] is not None and lo <= s["rsi"] <= hi and bar.c > bar.o
              and s["med"] is not None and bar.v >= self.c.get("vol_mult", 0.8) * s["med"])
        if not ok:
            return None
        e = bar.c
        kp = float(math.floor(e) if e != math.floor(e) else e - 1)
        w2 = float(self.c["width"])
        return OrderIntent([Leg("put", kp, "sell"), Leg("put", kp - w2, "buy")], credit=True, width=w2,
                           reason=f"ORB breakout {e:.2f} > {orh:.2f}: short {kp:g}P / long {kp - w2:g}P",
                           budget=float(self.c["max_loss_budget"]),
                           meta={"und": e, "notes": [f"RSI {s['rsi']:.0f}", f"vol {bar.v:.0f} vs median {s['med']:.0f}"]})

    def on_quote(self, pos, cq, now, ctx):
        e = pos.meta["und"]
        px = ctx.spot
        sp, tp = self.c.get("stop_pct", 0.0018), self.c.get("target_pct", 0.0045)
        if px is not None:
            if px <= e * (1 - sp):
                return ExitIntent(f"SPY stop -{sp:.2%}", urgent=True)
            if px >= e * (1 + tp):
                return ExitIntent(f"SPY target +{tp:.2%}")
        hold = self.c.get("max_hold_min", 45)
        if now - pos.opened_ts >= hold * 60:
            return ExitIntent(f"{hold} min time exit")
        if ct_time(now) >= hhmm(self.c.get("close_ct", "14:15")):
            return ExitIntent(f"close {self.c.get('close_ct', '14:15')} CT")
        return None

    def on_closed(self, pos, now) -> None:
        self.last_exit = now

    def plan(self, pos) -> str:
        e = pos.meta["und"]
        return (f"SPY stop {e * (1 - self.c.get('stop_pct', 0.0018)):.2f}, target {e * (1 + self.c.get('target_pct', 0.0045)):.2f}, "
                f"5m MACD cross, {self.c.get('max_hold_min', 45)} min, {self.c.get('close_ct', '14:15')} CT")
