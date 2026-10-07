"""Backtest: replay history through the exact live Engine, one session at a time.

Modes
  default   1m bars only. 144t bars can't be built from 1m data, so the trigger set is
            reduced to the 1m cross (SWING setup + SWING exits). Filters, RSI, strikes,
            sizing, exits and risk limits are identical to live.
  --ticks   downloads every SPY print per day (Alpaca, cached) and runs the full spec:
            144t + 1m triggers, SCALP and SWING. ~1M prints/day on SIP, so start with 5-10 days.
            On feed iex the "144t" bars use tick_bar_size_iex prints, exactly as the live engine does.

Option prices
  model      Black-Scholes on the replayed spot with a 0DTE skew. --iv is a VIX-style level (0.16 = VIX 16),
             priced on the trading-day clock of HANDOFF section 7 (0.80 x VIX/sqrt(252) x sqrt(RTH share
             left + 15/390)). Directionally useful, but real 0DTE IV moves intraday; treat P&L as an estimate.
  alpaca     real 1m option bars from Alpaca (needs options data on your plan).
  (Robinhood's get_option_historicals returned only gap-filled bars for past 0DTE contracts when
  tested on 2026-09-25, so it is not offered here. `--source robinhood` does pull real SPY 1m bars.)
"""
from __future__ import annotations

import asyncio
import copy
import csv
import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .bars import Bar, Trade
from .brokers.paper import PaperBroker
from .bus import Bus
from .clock import CT, at_ct, session_date
from .config import set_tick_bar_for_feed
from .engine import Engine
from .feeds.base import Feed, Heartbeat, Quote, QuoteSource
from .journal import Journal
from .pricing import quote_from_model, tick_spread, trading_clock_t_sec


class ReplayFeed(Feed):
    is_sim = True
    name = "replay"

    def __init__(self, day: date, history: list[Bar], bars: list[Bar] | None = None, trades_iter=None):
        self.day, self.hist, self.day_bars, self.trades_iter = day, history, bars, trades_iter
        self.t = at_ct(day, time(8, 0))
        self.px = history[-1].c if history else 0.0

    def now(self) -> float:
        return self.t

    async def history_1m(self, days: int) -> list[Bar]:
        return self.hist

    async def stream(self):
        yield Heartbeat(self.t)
        if self.trades_iter is not None:
            async for tr in self.trades_iter:
                self.t, self.px = tr.ts, tr.px
                yield tr
        else:
            for b in self.day_bars:
                up = b.c >= b.o
                for off, px in ((0, b.o), (15, b.l if up else b.h), (40, b.h if up else b.l), (59, b.c)):
                    self.t, self.px = b.t + off, px
                    yield Trade(self.t, px, max(1.0, b.v / 4))
        end = at_ct(self.day, time(15, 1))
        while self.t < end:
            self.t += 1
            yield Heartbeat(self.t)


class ModelQuotes(QuoteSource):
    """Black-Scholes 0DTE quotes on the replayed spot, with the simulator's skew.
    clock="trading" (default): `iv` is a VIX-style annual vol (prior VIX close / 100) and the time left counts on the
    trading-day clock of HANDOFF section 7, as the B/D research does (pricing.trading_clock_t_sec).
    clock="calendar": time left / 365 days, the convention before 2026-10-07. It priced 0DTE calls at about half of
    real (the "cheap-option model"); kept only so committed research (book_a_variants.py) reproduces."""
    name = "model"

    def __init__(self, feed: ReplayFeed, iv: float, clock: str = "trading"):
        if clock not in ("trading", "calendar"):
            raise ValueError(f"clock must be 'trading' or 'calendar', not {clock!r}")
        self.feed, self.iv, self.clock = feed, iv, clock

    async def quote(self, c) -> Quote | None:
        t_left = max(0.0, at_ct(self.feed.day, time(15, 15)) - self.feed.t)
        if self.clock == "trading":
            t_left = trading_clock_t_sec(t_left)
        bid, ask = quote_from_model(self.feed.px, c.strike, t_left, self.iv, c.right)
        return Quote(bid, ask, self.feed.t)


class AlpacaHistQuotes(QuoteSource):
    """Real option 1m bars; bid/ask approximated as close -/+ half a typical spread."""
    name = "alpaca-hist"

    def __init__(self, feed: ReplayFeed, fallback: ModelQuotes):
        self.feed, self.fallback = feed, fallback
        self.cache: dict[str, dict[int, float]] = {}

    async def _load(self, occ: str) -> dict[int, float]:
        import httpx
        from .feeds.alpaca import DATA, _headers, _ts
        start = datetime.fromtimestamp(at_ct(self.feed.day, time(8, 30)), timezone.utc)
        end = datetime.fromtimestamp(at_ct(self.feed.day, time(15, 15)), timezone.utc)
        out: dict[int, float] = {}
        async with httpx.AsyncClient(headers=_headers(), timeout=30) as c:
            r = await c.get(f"{DATA}/v1beta1/options/bars", params={"symbols": occ, "timeframe": "1Min",
                            "start": start.isoformat(), "end": end.isoformat(), "limit": 10000})
            if r.status_code == 200:
                for b in (r.json().get("bars") or {}).get(occ, []):
                    out[int(_ts(b["t"]) // 60)] = float(b["c"])
        return out

    async def quote(self, c) -> Quote | None:
        if c.occ not in self.cache:
            self.cache[c.occ] = await self._load(c.occ)
        m = int(self.feed.t // 60)
        bars = self.cache[c.occ]
        px = next((bars[k] for k in range(m, m - 5, -1) if k in bars), None)
        if px is None:
            return await self.fallback.quote(c)
        h = tick_spread(px) / 2
        return Quote(round(max(0.0, px - h), 2), round(px + h, 2), self.feed.t)


async def fetch_rh_bars(rh, days_needed: int, end: date) -> list[Bar]:
    """SPY 1m RTH bars from Robinhood, one session per call, walking back from `end`."""
    out, d, got = [], end, 0
    while got < days_needed and (end - d).days < days_needed * 2 + 10:
        if d.weekday() < 5:
            s = datetime.fromtimestamp(at_ct(d, time(8, 30)), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            e = datetime.fromtimestamp(at_ct(d, time(15, 0)), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            bars = await rh.equity_bars("SPY", s, e, "minute")
            if len(bars) > 300:
                got += 1
                for b in bars:
                    t = datetime.fromisoformat(b["begins_at"].replace("Z", "+00:00")).timestamp()
                    out.append(Bar("1m", t, float(b["open_price"]), float(b["high_price"]), float(b["low_price"]),
                                   float(b["close_price"]), float(b.get("volume") or 0), 0, t + 60))
        d -= timedelta(days=1)
    return sorted(out, key=lambda b: b.t)


async def _trades_cached(day: date, feed: str):
    from .feeds.alpaca import fetch_trades
    import numpy as np
    cache = Path(os.path.expanduser("~/.agentdesk/cache"))
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"SPY_trades_{feed}_{day}_clean.npz"      # bad prints dropped at download (feeds/prints.py)
    if not f.exists():
        start = datetime.fromtimestamp(at_ct(day, time(8, 30)), timezone.utc)
        end = datetime.fromtimestamp(at_ct(day, time(15, 0)), timezone.utc)
        ts, px, sz = [], [], []
        async for tr in fetch_trades("SPY", start, end, feed):
            ts.append(tr.ts); px.append(tr.px); sz.append(tr.sz)
        np.savez_compressed(f, ts=np.array(ts), px=np.array(px), sz=np.array(sz))
    d = np.load(f)
    for a, b, c in zip(d["ts"], d["px"], d["sz"]):
        yield Trade(float(a), float(b), float(c))


async def run_day(cfg, day: date, history: list[Bar], bars: list[Bar] | None, args, trades_iter=None, rh=None) -> list:
    feed = ReplayFeed(day, history, bars, trades_iter)
    model = ModelQuotes(feed, args.iv)
    quotes = AlpacaHistQuotes(feed, model) if args.options == "alpaca" else model
    eng = Engine(cfg, feed, quotes, PaperBroker(quotes), Bus(), Journal(None), "backtest")
    await eng.run()
    for pos, plan in list(eng.open):            # safety: anything left gets marked at the last quote
        q = await quotes.quote(pos.contract)
        pos.realized += ((q.bid if q else 0) - pos.entry) * 100 * pos.qty
        pos.qty, pos.status, pos.exit_reason = 0, "closed", "eod mark"
        eng.closed.append(pos)
    return eng.closed


def summarize(rows: list[dict]) -> dict:
    nets = [r["net"] for r in rows]
    if not nets:
        return {"trades": 0}
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x <= 0]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    days = defaultdict(float)
    for r in rows:
        days[r["day"]] += r["net"]

    def group(key):
        g = defaultdict(list)
        for r in rows:
            g[r[key]].append(r["net"])
        return {k: {"n": len(v), "net": round(sum(v), 2), "win_rate": round(sum(1 for x in v if x > 0) / len(v), 3)}
                for k, v in sorted(g.items())}

    return {
        "trades": len(nets), "days": len(days), "net": round(sum(nets), 2),
        "win_rate": round(len(wins) / len(nets), 3),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0, "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "expectancy": round(sum(nets) / len(nets), 2),
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "max_drawdown": round(mdd, 2),
        "green_days": sum(1 for v in days.values() if v > 0), "red_days": sum(1 for v in days.values() if v <= 0),
        "best_day": round(max(days.values()), 2), "worst_day": round(min(days.values()), 2),
        "by_setup": group("setup"), "by_hour": group("hour"), "by_exit": group("exit"), "by_offset": group("offset"),
    }


def _row(p, day) -> dict:
    return {"day": str(day), "open": datetime.fromtimestamp(p.opened_ts, CT).strftime("%H:%M:%S"),
            "hour": datetime.fromtimestamp(p.opened_ts, CT).strftime("%H:00"), "contract": p.contract.label,
            "setup": p.setup, "qty": p.qty_initial, "entry": round(p.entry, 2), "peak": round(p.peak, 2),
            "net": round(p.realized - p.fees, 2), "exit": (p.exit_reason or "").split(":")[0],
            "offset": p.strike_reason.split(":")[0], "hold_min": round(((p.closed_ts or p.opened_ts) - p.opened_ts) / 60, 1)}


async def main(cfg, args) -> None:
    cfg = copy.deepcopy(cfg)
    cfg["crew"]["enabled"] = False
    rows: list[dict] = []
    warm = cfg["data"]["history_days"]

    if args.sim_days:
        from .feeds.sim import SimFeed, SimQuotes
        from .crew import Crew
        d = date(2026, 1, 5)
        for i in range(args.sim_days):
            while d.weekday() >= 5:
                d += timedelta(days=1)
            feed = SimFeed(day=d, seed=1000 + i)
            q = SimQuotes(feed)
            eng = Engine(cfg, feed, q, PaperBroker(q), Bus(), Journal(None), "backtest")
            await eng.run()
            rows += [_row(p, d) for p in eng.closed]
            d += timedelta(days=1)
        label = f"{args.sim_days} synthetic days (plumbing check, no edge information)"
    else:
        if not args.ticks:
            cfg["strategy"]["trigger_timeframes"] = ["1m"]
        else:                                  # same "144t" construction as the live engine on this feed
            tick_n = set_tick_bar_for_feed(cfg, cfg["data"]["alpaca"]["feed"])
        rh = None
        if args.csv:
            bars = _load_csv(args.csv)
        elif args.source == "robinhood":
            from .brokers.robinhood import RobinhoodMCP
            rh = RobinhoodMCP(cfg)
            await rh.start()
            end_d = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
            bars = await fetch_rh_bars(rh, args.days + warm, end_d)
        else:
            from .feeds.alpaca import fetch_bars_1m
            end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc) - timedelta(days=1)
            start = end - timedelta(days=int(args.days * 1.5) + warm * 2 + 4)
            bars = await fetch_bars_1m("SPY", start, end, cfg["data"]["alpaca"]["feed"])
        by_day = defaultdict(list)
        for b in bars:
            by_day[session_date(b.t)].append(b)
        days = sorted(by_day)
        test_days = days[warm:][-args.days:]
        for d in test_days:
            i = days.index(d)
            hist = [b for dd in days[max(0, i - warm):i] for b in by_day[dd]]
            it = _trades_cached(d, cfg["data"]["alpaca"]["feed"]) if args.ticks else None
            closed = await run_day(cfg, d, hist, by_day[d], args, it, rh)
            rows += [_row(p, d) for p in closed]
            print(f"{d}  trades {len(closed):2d}  net {sum(p.realized - p.fees for p in closed):+8.2f}")
        label = (f"{len(test_days)} days {test_days[0]}..{test_days[-1]}, "
                 f"{f'1m+144t (ticks, {tick_n} prints/bar)' if args.ticks else '1m trigger only'}, "
                 f"options={args.options}") if test_days else "no data"

    out = Path(args.out)
    out.mkdir(exist_ok=True)
    if rows:
        with open(out / "trades.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    s = summarize(rows)
    s["run"] = label
    (out / "summary.json").write_text(json.dumps(s, indent=2))
    print(json.dumps({k: v for k, v in s.items() if not k.startswith("by_")}, indent=2))
    print(f"-> {out}/trades.csv, {out}/summary.json")


def _load_csv(path: str) -> list[Bar]:
    """CSV with columns t (epoch s or ISO), o,h,l,c,v at 1-minute resolution."""
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            t = r.get("t") or r.get("timestamp") or r.get("datetime") or r.get("Datetime")
            ts = float(t) if t.replace(".", "").isdigit() else datetime.fromisoformat(t).timestamp()
            g = lambda *k: float(next(r[x] for x in k if x in r))
            out.append(Bar("1m", ts, g("o", "open", "Open"), g("h", "high", "High"), g("l", "low", "Low"),
                           g("c", "close", "Close"), g("v", "volume", "Volume"), 0, ts + 60))
    return out
