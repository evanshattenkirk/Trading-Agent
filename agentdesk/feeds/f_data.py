"""Equity data for book F1 (docs/BOOK_F_HANDOFF.md section 4.2; F1 v2 in research/strategy_f1_prereg.md).

RobinhoodEquityData (paper/shadow): RVOL5 uses one volume source for today and for the 14-day base:
  bars_source: robinhood   get_equity_historicals 1-minute bars (up to 10 symbols per call, at most
                           `max_calls_per_s` calls per second, counted per minute)
  bars_source: alpaca_sip  Alpaca's consolidated (SIP) 1-minute bars, many symbols per call. Needs the paid
                           Algo Trader Plus plan for today's bars; the free plan serves SIP only 15 minutes late
The two sources never share a cache (or/ and or_sip/), so RVOL5 is never a mix.
The broad universe (universe.source: all_us) comes from the NASDAQ Trader symbol directory (common stocks and ADRs;
ETFs, test issues, warrants, rights, units and preferreds dropped) and Alpaca daily bars, which are free once 15
minutes old. Without Alpaca keys or the directory, us_daily() returns {} and F1 keeps the S&P universe.
Cached under ~/.agentdesk/cache/f/: constituents.csv and us_symbols.json (weekly), daily/DATE.json,
us_daily/DATE.json, or/DATE.json or or_sip/DATE.json (09:30-09:34 volume per name).
A symbol with no data is dropped, never guessed.

SimEquityData: a deterministic synthetic session for `--mode sim` (its own random generator, so book A's sim day
is unchanged). It says nothing about edge.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import os
import random
import time
import zlib
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..books import f_stocks_in_play as F
from ..brokers.base import RateLimited

log = logging.getLogger("agentdesk.f_data")
CONSTITUENTS_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
SYMBOL_DIRS = ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
               "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")
ALPACA_BARS = "https://data.alpaca.markets/v2/stocks/bars"
NOT_COMMON = ("warrant", " right", " rights", " unit", " units", "preferred", " notes", "debenture", "depositary share representing")


def parse_symbol_dir(text: str) -> set[str]:
    """Common stocks and ADRs from a NASDAQ Trader symbol file (nasdaqlisted.txt or otherlisted.txt): ETFs, test
    issues, warrants, rights, units and preferreds are dropped, and so are symbols with characters other than
    letters and one dot (BRK.B stays)."""
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("File Creation Time")]
    if not lines:
        return set()
    head = [h.strip() for h in lines[0].split("|")]
    col = {h: i for i, h in enumerate(head)}
    sym_i = col.get("Symbol", col.get("ACT Symbol"))
    if sym_i is None:
        return set()
    out = set()
    for l in lines[1:]:
        f = l.split("|")
        if len(f) < len(head):
            continue
        get = lambda k: f[col[k]].strip() if k in col else ""
        sym, name = get(head[sym_i]).upper(), get("Security Name").lower()
        if get("Test Issue") == "Y" or get("ETF") == "Y" or any(w in name for w in NOT_COMMON):
            continue
        if sym and sym.replace(".", "", 1).isalpha():
            out.add(sym)
    return out


async def alpaca_bars(symbols: list[str], timeframe: str, start: str, end: str, feed: str = "sip",
                      http=None, per_call: int = 200) -> dict[str, list[dict]]:
    """Alpaca multi-symbol bars, raw {symbol: [{t, o, h, l, c, v}]}. Paged; a 429 or 5xx backs off and retries.
    `http` is an httpx.AsyncClient-like object (tests pass a fake)."""
    import httpx
    from .alpaca import _headers
    own = http is None
    c = http or httpx.AsyncClient(headers=_headers(), timeout=60)
    out: dict[str, list[dict]] = {}
    try:
        for grp in _chunks(sorted(symbols), per_call):
            token = None
            while True:
                params = {"symbols": ",".join(grp), "timeframe": timeframe, "start": start, "end": end, "feed": feed,
                          "adjustment": "split", "limit": 10000, "sort": "asc"}
                if token:
                    params["page_token"] = token
                for i in range(6):
                    r = await c.get(ALPACA_BARS, params=params)
                    if r.status_code == 429 or r.status_code >= 500:
                        await asyncio.sleep(min(2 ** i, 20))
                        continue
                    r.raise_for_status()
                    break
                else:
                    raise RuntimeError(f"Alpaca bars kept failing ({r.status_code})")
                j = r.json()
                for sym, bs in (j.get("bars") or {}).items():
                    out.setdefault(sym, []).extend(bs)
                token = j.get("next_page_token")
                if not token:
                    break
    finally:
        if own:
            await c.aclose()
    return out


def _chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _utc(d: date, minute: int) -> str:
    ts = F.at_et(d, datetime.min.time().replace(hour=minute // 60, minute=minute % 60))
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Throttle:
    """At most `per_s` calls in any 1-second window; logs the count for each minute."""

    def __init__(self, per_s: int = 20):
        self.per_s, self.recent, self.minute, self.count = per_s, deque(), None, 0

    async def wait(self) -> None:
        while True:
            now = time.monotonic()
            while self.recent and now - self.recent[0] >= 1.0:
                self.recent.popleft()
            if len(self.recent) < self.per_s:
                break
            await asyncio.sleep(1.0 - (now - self.recent[0]) + 0.001)
        self.recent.append(time.monotonic())
        m = int(time.time() // 60)
        if m != self.minute:
            if self.minute is not None and self.count:
                log.info("book F: %d Robinhood calls in the last minute", self.count)
            self.minute, self.count = m, 0
        self.count += 1


class RobinhoodEquityData:
    def __init__(self, rh, cfg: dict):
        self.rh, self.c = rh, cfg
        self.dir = Path(os.path.expanduser(cfg.get("cache_dir", "~/.agentdesk/cache/f")))
        self.throttle = Throttle(int(cfg.get("max_calls_per_s", 20)))

    async def _call(self, tool: str, args: dict, retry: bool = True):
        start = getattr(self.rh, "start", None)
        if start is not None:
            await start()                                   # idempotent: the engine usually connected already
        if "account_number" in args and args["account_number"] is None:
            args = {**args, "account_number": getattr(self.rh, "account", None)}   # known only after start()
        await self.throttle.wait()
        try:
            return await self.rh.call(tool, args)
        except Exception as ex:
            if not retry or isinstance(ex, RateLimited):     # the client already paused every call; don't pile on
                raise
            await asyncio.sleep(0.3)
            await self.throttle.wait()
            return await self.rh.call(tool, args)          # one retry per call (handoff 4.2)

    def _read(self, path: Path):
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    def _write(self, path: Path, obj) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(json.dumps(obj, default=str))
        tmp.replace(path)

    # ------------------------------------------------------------ universe inputs
    async def sp500(self) -> list[str]:
        p = self.dir / "constituents.csv"
        fresh = p.exists() and time.time() - p.stat().st_mtime < 7 * 86400
        if not fresh:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=30) as c:
                    r = await c.get(CONSTITUENTS_URL)
                    r.raise_for_status()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(r.content)
            except Exception as ex:
                log.warning("S&P 500 list download failed (%s); %s", ex, "using the cached copy" if p.exists() else "extras only")
                if not p.exists():
                    return []
        return sorted({row["Symbol"].strip() for row in csv.DictReader(io.StringIO(p.read_text()))})

    async def us_symbols(self) -> list[str]:
        """Every listed US common stock and ADR (NASDAQ Trader symbol directory), cached a week."""
        p = self.dir / "us_symbols.json"
        cached = self._read(p) if p.exists() else None
        if cached and time.time() - p.stat().st_mtime < 7 * 86400:
            return cached
        try:
            import httpx
            syms: set[str] = set()
            async with httpx.AsyncClient(timeout=30) as c:
                for url in SYMBOL_DIRS:
                    r = await c.get(url)
                    r.raise_for_status()
                    syms |= parse_symbol_dir(r.text)
            if len(syms) < 1000:
                raise RuntimeError(f"only {len(syms)} symbols parsed")
            out = sorted(syms)
            self._write(p, out)
            return out
        except Exception as ex:
            log.warning("US symbol directory download failed (%s); %s", ex, "using the cached copy" if cached else "no broad universe")
            return cached or []

    async def us_daily(self, day: date) -> dict[str, list[dict]]:
        """Daily bars through yesterday for every US common stock with a close >= min_price and a 14-day average
        volume >= min_avg_volume, from Alpaca (free: these bars are more than 15 minutes old). {} when Alpaca keys
        or the symbol directory are missing, so the caller falls back to the S&P universe."""
        path = self.dir / "us_daily" / f"{day}.json"
        cached = self._read(path)
        if cached is None:
            syms = await self.us_symbols()
            if not syms:
                return {}
            u = self.c.get("universe", {})
            start = (datetime.combine(day, datetime.min.time()) - timedelta(days=50)).strftime("%Y-%m-%dT00:00:00Z")
            end = (datetime.combine(day, datetime.min.time()) - timedelta(days=1)).strftime("%Y-%m-%dT23:59:59Z")
            try:
                raw = await alpaca_bars(syms, "1Day", start, end, feed="sip")
            except (Exception, SystemExit) as ex:                  # _headers() exits without keys
                log.warning("Alpaca daily bars for the broad universe failed (%s); F1 keeps the S&P universe", ex)
                return {}
            cached = {}
            for s, bs in raw.items():
                rows = [{"d": b["t"][:10], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
                        for b in bs if b["t"][:10] < str(day)]
                if len(rows) >= 20 and rows[-1]["c"] >= u.get("min_price", 0) \
                        and sum(r["v"] for r in rows[-14:]) / 14 >= u.get("min_avg_volume", 0):
                    cached[s] = rows
            self._write(path, cached)
            for old in sorted((self.dir / "us_daily").glob("*.json"))[:-5]:     # keep the last 5 days
                old.unlink(missing_ok=True)
        return {s: [{**b, "d": date.fromisoformat(b["d"])} for b in bs] for s, bs in cached.items()}

    async def daily_bars(self, symbols: list[str], day: date) -> dict[str, list[dict]]:
        path = self.dir / "daily" / f"{day}.json"
        cached = self._read(path) or {}
        todo = [s for s in symbols if s not in cached]
        start = (datetime.combine(day, datetime.min.time()) - timedelta(days=50)).strftime("%Y-%m-%dT00:00:00Z")
        end = (datetime.combine(day, datetime.min.time()) - timedelta(days=1)).strftime("%Y-%m-%dT23:59:59Z")
        for grp in _chunks(todo, 10):
            try:
                data = await self._call("get_equity_historicals", {"symbols": grp, "start_time": start, "end_time": end,
                                                                   "interval": "day", "bounds": "regular"})
            except Exception as ex:
                log.warning("daily bars %s failed: %s", grp, ex)
                continue
            for s, bars in _parse_bars(data, grp).items():
                cached[s] = [{"d": b["begins_at"][:10], **_ohlcv(b)} for b in bars if b["begins_at"][:10] < str(day)]
        if todo:
            self._write(path, cached)
        return {s: [{**b, "d": date.fromisoformat(b["d"])} for b in bs] for s, bs in cached.items() if s in symbols and bs}

    async def tradable(self, symbols: list[str]) -> set[str]:
        ok = set()
        for grp in _chunks(symbols, 10):
            try:
                data = await self._call("get_equity_tradability", {"account_number": None, "symbols": grp})
            except Exception as ex:
                log.warning("tradability %s failed (%s); keeping them", grp, ex)
                ok |= set(grp)
                continue
            from ..brokers.robinhood import dict_items
            recs = {str(r.get("symbol") or "").upper(): r for r in dict_items(data)}
            for s in grp:
                r = recs.get(s)
                if r is None:
                    ok.add(s)
                    continue
                flag = r.get("tradable", r.get("regular_hours_tradable", r.get("tradability", True)))
                if flag is True or str(flag).lower() in ("true", "tradable", "yes"):
                    ok.add(s)
        return ok

    async def or_volumes(self, symbols: list[str], dates: list[date]) -> dict[date, dict[str, float]]:
        out = {}
        sub = "or_sip" if self.sip else "or"          # never mix volume sources in one RVOL5 base
        for d in dates:
            path = self.dir / sub / f"{d}.json"
            vols = self._read(path) or {}
            todo = [s for s in symbols if s not in vols]
            if todo:
                got = await self.minute_bars(todo, d, 570, 575)
                for s in todo:
                    bs = got.get(s)
                    if bs:
                        vols[s] = sum(b["v"] for b in bs)
                self._write(path, vols)
            out[d] = {s: v for s, v in vols.items() if s in symbols}
        return out

    # ------------------------------------------------------------ intraday
    @property
    def sip(self) -> bool:
        return self.c.get("bars_source") == "alpaca_sip"

    async def minute_bars(self, symbols: list[str], day: date, start: int, end: int) -> dict[str, list[dict]]:
        if end <= start:            # no whole minute in the window yet: Robinhood refuses it ("end must be after start")
            return {}
        if self.sip:
            return await self._sip_minute_bars(symbols, day, start, end)
        out = {}
        for grp in _chunks(symbols, 10):
            try:
                data = await self._call("get_equity_historicals", {"symbols": grp, "start_time": _utc(day, start),
                                                                   "end_time": _utc(day, end), "interval": "minute",
                                                                   "bounds": "regular"})
            except Exception as ex:
                log.warning("minute bars %s failed: %s; dropping them", grp, ex)
                continue
            for s, bars in _parse_bars(data, grp).items():
                rows = []
                for b in bars:
                    t = datetime.fromisoformat(b["begins_at"].replace("Z", "+00:00")).astimezone(F.ET)
                    m = t.hour * 60 + t.minute
                    if t.date() == day and start <= m < end:
                        rows.append({"t": m, **_ohlcv(b)})
                if rows:
                    out[s] = rows
        return out

    async def _sip_minute_bars(self, symbols: list[str], day: date, start: int, end: int) -> dict[str, list[dict]]:
        try:
            raw = await alpaca_bars(symbols, "1Min", _utc(day, start), _utc(day, end), feed="sip")
        except (Exception, SystemExit) as ex:   # SystemExit: no Alpaca keys
            log.warning("Alpaca SIP minute bars failed (%s); dropping %d names", ex, len(symbols))
            return {}
        out = {}
        for s, bs in raw.items():
            rows = []
            for b in bs:
                t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(F.ET)
                m = t.hour * 60 + t.minute
                if t.date() == day and start <= m < end:
                    rows.append({"t": m, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
            if rows:
                out[s] = rows
        return out

    async def quotes(self, symbols: list[str]) -> dict[str, F.Q]:
        from ..brokers.robinhood import dict_items
        out = {}
        for grp in _chunks(symbols, 10):
            data = await self._call("get_equity_quotes", {"symbols": grp})
            now = time.time()
            for r in dict_items(data):
                q = r.get("quote") if isinstance(r.get("quote"), dict) else r
                s = str(q.get("symbol") or r.get("symbol") or "").upper()
                b, a = q.get("bid_price"), q.get("ask_price")
                last = q.get("last_trade_price")
                if s and b not in (None, "") and a not in (None, "") and float(b) > 0 and float(a) > 0:
                    out[s] = F.Q(float(b), float(a), float(last) if last not in (None, "") else None, now)
        return out


def _ohlcv(b: dict) -> dict:
    return {"o": float(b["open_price"]), "h": float(b["high_price"]), "l": float(b["low_price"]),
            "c": float(b["close_price"]), "v": float(b.get("volume") or 0)}


def _parse_bars(data, symbols: list[str]) -> dict[str, list[dict]]:
    """get_equity_historicals: results[] with a symbol and bars[] (interpolated bars dropped)."""
    res = data.get("results", []) if isinstance(data, dict) else []
    out = {}
    for i, r in enumerate(res):
        s = str(r.get("symbol") or (symbols[i] if i < len(symbols) and len(res) == len(symbols) else "")).upper()
        if s:
            out[s] = [b for b in r.get("bars", []) if not b.get("interpolated") and b.get("open_price") is not None]
    return out


# ------------------------------------------------------------------ simulator
SIM_SP = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA", "AMD", "MU", "ORCL", "PLTR", "SMCI", "WDC",
          "STX", "MRVL", "JPM", "XOM", "UNH", "V", "MA", "HD", "LLY", "COST", "NFLX", "CRM", "BAC", "WMT", "KO", "PEP",
          "ABBV", "CVX", "ADBE", "INTC", "QCOM", "TXN", "GS", "CAT", "DE", "LOW"]
SIM_OTHER = ["TSM", "ARM"]


class SimEquityData:
    """40-odd synthetic large caps. Each day a handful are 'in play' (opening volume 2-6x normal, a gap and a
    momentum drift). Bars complete as the sim clock passes them; quotes walk inside the current bar."""

    def __init__(self, feed, seed: int = 5150):
        self.feed, self.seed = feed, seed
        self._day, self._bars, self._daily, self._orv = None, {}, {}, {}
        rng = random.Random(seed)
        self.base = {s: (rng.uniform(25, 600), rng.uniform(4e8, 6e9)) for s in SIM_SP + SIM_OTHER}

    def _rng(self, *key) -> random.Random:
        return random.Random(zlib.crc32(("|".join(map(str, (self.seed,) + key))).encode()))

    def _dates(self, day: date, n: int = 30) -> list[date]:
        out, d = [], day
        while len(out) < n:
            d -= timedelta(days=1)
            if d.weekday() < 5:
                out.append(d)
        return sorted(out)

    def _build(self, day: date) -> None:
        if self._day == day:
            return
        self._day, self._bars, self._daily, self._orv = day, {}, {}, {}
        dates = self._dates(day)
        pick = self._rng("inplay", day)
        names = SIM_SP + SIM_OTHER
        hot = set(pick.sample(names, pick.randint(3, 8)))
        for s in names:
            px0, dv = self.base[s]
            r = self._rng("daily", s, day)
            px, daily = px0, []
            atr = px0 * r.uniform(0.012, 0.03)
            for d in dates:
                o = px
                c = max(5.0, o + r.gauss(0, atr * 0.6))
                h, l = max(o, c) + abs(r.gauss(0, atr * 0.3)), min(o, c) - abs(r.gauss(0, atr * 0.3))
                daily.append({"d": d, "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2),
                              "v": round(dv / c * r.uniform(0.7, 1.3))})
                px = c
            self._daily[s] = daily
            or_base = dv / px * 0.03
            self._orv[s] = {d: or_base * r.uniform(0.8, 1.2) for d in dates}
            t = self._rng("today", s, day)
            up = t.random() < 0.6
            mult = t.uniform(2.0, 6.0) if s in hot else t.uniform(0.5, 1.6)
            drift = (1 if up else -1) * atr * (0.004 if s in hot else 0.0) * t.uniform(0.5, 1.5)
            gap = (1 if up else -1) * t.uniform(0.005, 0.03) if s in hot else t.gauss(0, 0.004)
            p = px * (1 + gap)
            sig = atr / math.sqrt(390) * 1.2
            bars = []
            for m in range(570, 960):
                first = m < 575
                o = p
                step = (drift * 6 if (first and s in hot) else drift) + t.gauss(0, sig)
                c = max(1.0, o + step)
                h, l = max(o, c) + abs(t.gauss(0, sig * 0.5)), min(o, c) - abs(t.gauss(0, sig * 0.5))
                v = or_base / 5 * mult * t.uniform(0.7, 1.3) if first else or_base / 15 * t.uniform(0.5, 1.5)
                bars.append({"t": m, "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "v": round(v)})
                p = c
            self._bars[s] = bars

    async def sp500(self) -> list[str]:
        return list(SIM_SP)

    async def daily_bars(self, symbols, day):
        self._build(day)
        return {s: list(self._daily[s]) for s in symbols if s in self._daily}

    async def us_daily(self, day):
        self._build(day)
        return {s: list(bs) for s, bs in self._daily.items()}

    async def tradable(self, symbols):
        return set(symbols)

    async def or_volumes(self, symbols, dates):
        if dates:
            self._build(self.feed_day())
        return {d: {s: self._orv[s][d] for s in symbols if s in self._orv and d in self._orv[s]} for d in dates}

    def feed_day(self) -> date:
        return F.et(self.feed.now()).date()

    def _cur_min(self) -> float:
        n = F.et(self.feed.now())
        return n.hour * 60 + n.minute + n.second / 60

    async def minute_bars(self, symbols, day, start, end):
        self._build(day)
        cur = self._cur_min() if day == self.feed_day() else 24 * 60
        return {s: [b for b in self._bars[s] if start <= b["t"] < end and b["t"] + 1 <= cur]
                for s in symbols if s in self._bars}

    async def quotes(self, symbols):
        day = self.feed_day()
        self._build(day)
        cur, now = self._cur_min(), self.feed.now()
        out = {}
        for s in symbols:
            bars = self._bars.get(s)
            if not bars:
                continue
            i = int(cur) - 570
            if i < 0:
                px = self._daily[s][-1]["c"]
            elif i >= len(bars):
                px = bars[-1]["c"]
            else:
                b, f = bars[i], cur - int(cur)
                px = min(b["h"], max(b["l"], b["o"] + (b["c"] - b["o"]) * f))
            h = max(0.01, round(px * 0.0001, 2))
            out[s] = F.Q(round(px - h, 2), round(px + h, 2), round(px, 2), now)
        return out
