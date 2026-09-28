"""Synthetic SPY session for demos and plumbing tests.

A regime-switching random walk (trend-up / trend-down / chop) with a U-shaped
trade rate and one scheduled "event" jump. Option quotes come from Black-Scholes
with a 0DTE skew. This proves the machinery works; it says nothing about edge.
"""
from __future__ import annotations

import asyncio
import math
import random
from datetime import date, datetime, time, timedelta

from ..bars import Bar, Trade, TimeBarBuilder
from ..clock import CT, at_ct
from ..pricing import quote_from_model
from .base import Feed, Heartbeat, Quote, QuoteSource

REGIMES = {
    # name: (drift $/sec, vol $/sqrt(sec), mean dwell sec)
    "up": (0.0010, 0.032, 1500),
    "down": (-0.0010, 0.036, 1200),
    "chop": (0.0, 0.030, 1800),
}


class SimFeed(Feed):
    is_sim = True
    name = "sim"

    def __init__(self, day: date | None = None, seed: int = 7, speed: float = 0.0, start_px: float = 662.0,
                 pre_minutes: int = 50, post_minutes: int = 10, trade_rate: float = 9.0):
        self.day = day or _next_weekday(datetime.now(CT).date())
        self.rng = random.Random(seed)
        self.speed = speed
        self.px = start_px
        self.t = at_ct(self.day, time(8, 30)) - pre_minutes * 60
        self.pre, self.post = pre_minutes, post_minutes
        self.rate = trade_rate
        self.base_iv = 0.16
        self.event = self._make_event()
        self.mood = self.rng.choices(["risk_on", "neutral", "risk_off"], weights=[0.35, 0.4, 0.25])[0]

    def _make_event(self) -> dict | None:
        if self.rng.random() < 0.5:
            return None                        # about half of sim days have a scheduled high-impact event
        hh, mm, name = self.rng.choice([(12, 0, "Fed speaker (sim)"), (13, 0, "FOMC minutes (sim)"), (9, 0, "ISM Services (sim)")])
        return {"ts": at_ct(self.day, time(hh, mm)), "time": f"{hh:02d}:{mm:02d}", "name": name, "impact": "high"}

    def now(self) -> float:
        return self.t

    def _walk_day(self, rng: random.Random, px: float, day: date, with_trades: bool, rate: float):
        """Yields (second_ts, open_px, close_px, n_trades) for each RTH second."""
        start = at_ct(day, time(8, 30))
        regime = rng.choice(list(REGIMES))
        left = rng.expovariate(1 / REGIMES[regime][2])
        ev_ts = self.event["ts"] if self.event and day == self.day else None
        anchor = px
        for s in range(6 * 3600 + 30 * 60):
            ts = start + s
            if left <= 0:
                regime = rng.choices(list(REGIMES), weights=[0.36, 0.28, 0.36])[0]
                left = rng.expovariate(1 / REGIMES[regime][2])
            left -= 1
            drift, vol, _ = REGIMES[regime]
            frac = s / 23400
            u = 1.0 + 1.6 * math.exp(-frac * 14) + 0.9 * math.exp(-(1 - frac) * 18)   # U-shaped activity
            p0 = px
            px = px + drift + vol * math.sqrt(u) * rng.gauss(0, 1) - (px - anchor) / 9000.0
            if ev_ts is not None and ev_ts <= ts < ev_ts + 1:
                px += rng.choice([-1, 1]) * rng.uniform(0.6, 1.4)
            n = _poisson(rng, rate * u) if with_trades else 0
            yield ts, p0, px, n

    async def history_1m(self, days: int) -> list[Bar]:
        out: list[Bar] = []
        rng = random.Random(self.rng.random())
        d, dates = self.day, []
        while len(dates) < days:
            d = d - timedelta(days=1)
            if d.weekday() < 5:
                dates.append(d)
        px = self.px * (1 - 0.002 * days)
        for d in reversed(dates):
            b = TimeBarBuilder("1m")
            for ts, p0, p1, _ in self._walk_day(rng, px, d, False, 0):
                c = b.on_trade(Trade(ts, p1, 1000))
                if c:
                    out.append(c)
                px = p1
            last = b.flush(ts + 60)
            if last:
                out.append(last)
            px += rng.gauss(0, 1.2)            # overnight gap
        self.px = px
        return out

    async def stream(self):
        pace = 1.0 / self.speed if self.speed else 0.0
        # pre-market: heartbeats only
        open_ts = at_ct(self.day, time(8, 30))
        while self.t < open_ts:
            yield Heartbeat(self.t)
            self.t += 1
            await asyncio.sleep(pace) if pace else await asyncio.sleep(0)
        for ts, p0, p1, n in self._walk_day(self.rng, self.px, self.day, True, self.rate):
            self.t = ts
            for i in range(n):
                f = (i + 1) / (n + 1)
                px = round(p0 + (p1 - p0) * f + self.rng.gauss(0, 0.006), 2)
                size = self.rng.choice([100, 100, 100, 200, 300, 500, 1000]) * self.rng.randint(1, 3)
                yield Trade(ts + f, px, size)
            self.px = p1
            yield Heartbeat(ts + 0.999)
            if pace:
                await asyncio.sleep(pace)
            elif int(ts) % 30 == 0:
                await asyncio.sleep(0)
        end = self.t + self.post * 60
        while self.t < end:
            self.t += 1
            yield Heartbeat(self.t)
            await asyncio.sleep(pace) if pace else await asyncio.sleep(0)


class SimQuotes(QuoteSource):
    name = "sim"

    def __init__(self, feed: SimFeed):
        self.feed = feed

    async def quote(self, contract) -> Quote | None:
        close = at_ct(self.feed.day, time(15, 0))
        t_left = max(0.0, close - self.feed.t) + 900       # SPY 0DTE trades until 15:15 CT
        bid, ask = quote_from_model(self.feed.px, contract.strike, t_left, self.feed.base_iv, contract.right)
        return Quote(bid, ask, self.feed.t)


def _poisson(rng: random.Random, lam: float) -> int:
    if lam > 30:
        return max(0, int(rng.gauss(lam, math.sqrt(lam))))
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def _next_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d
