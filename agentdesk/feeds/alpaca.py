"""Alpaca market data: SPY trades over websocket, 1m history over REST, option quotes (OPRA).

Env: ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY.
Plans: Basic (free) = IEX real-time only (a small slice of prints, so 144-tick bars form far
slower than on a consolidated chart). Algo Trader Plus ($99/mo) = full SIP + OPRA.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from ..bars import Bar, Trade
from ..clock import is_rth, session_date
from .base import Feed, Heartbeat, Quote, QuoteSource
from .prints import PrintFilter

log = logging.getLogger("agentdesk.alpaca")
DATA = "https://data.alpaca.markets"


def _headers() -> dict:
    k, s = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not k or not s:
        raise SystemExit("Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


async def fetch_bars_1m(symbol: str, start: datetime, end: datetime, feed: str) -> list[Bar]:
    out, token = [], None
    async with httpx.AsyncClient(headers=_headers(), timeout=30) as c:
        while True:
            params = {"timeframe": "1Min", "start": start.isoformat(), "end": end.isoformat(), "feed": feed,
                      "limit": 10000, "adjustment": "raw", "sort": "asc"}
            if token:
                params["page_token"] = token
            r = await c.get(f"{DATA}/v2/stocks/{symbol}/bars", params=params)
            r.raise_for_status()
            j = r.json()
            for b in j.get("bars") or []:
                t = _ts(b["t"])
                if is_rth(t):
                    out.append(Bar("1m", t, b["o"], b["h"], b["l"], b["c"], b["v"], b.get("n", 0), t + 60))
            token = j.get("next_page_token")
            if not token:
                return out


async def fetch_trades(symbol: str, start: datetime, end: datetime, feed: str, clean: bool = True):
    """Async generator of Trade for a window (used by backtest --ticks). Bad prints are dropped unless clean=False."""
    token, prints = None, PrintFilter(enabled=clean)
    async with httpx.AsyncClient(headers=_headers(), timeout=60) as c:
        while True:
            params = {"start": start.isoformat(), "end": end.isoformat(), "feed": feed, "limit": 10000, "sort": "asc"}
            if token:
                params["page_token"] = token
            r = await c.get(f"{DATA}/v2/stocks/{symbol}/trades", params=params)
            r.raise_for_status()
            j = r.json()
            for t in j.get("trades") or []:
                for tr in prints.push(Trade(_ts(t["t"]), float(t["p"]), float(t["s"])), t.get("c")):
                    yield tr
            token = j.get("next_page_token")
            if not token:
                if clean:
                    log.info("alpaca %s %s trades cleaned: %s", feed, symbol, prints.stats)
                return


class AlpacaFeed(Feed):
    name = "alpaca"

    def __init__(self, cfg):
        self.symbol = cfg["symbol"]
        self.feed = cfg["data"]["alpaca"]["feed"]
        self.q: asyncio.Queue = asyncio.Queue(maxsize=200000)
        self.prints = PrintFilter(enabled=cfg["data"]["alpaca"].get("clean_prints", True))

    async def history_1m(self, days: int) -> list[Bar]:
        end = datetime.now(timezone.utc) - timedelta(minutes=16)     # free plan: SIP history must be >15 min old
        start = end - timedelta(days=days + 4)
        bars = await fetch_bars_1m(self.symbol, start, end, self.feed)
        keep = set(sorted({session_date(b.t) for b in bars})[-(days + 1):])     # N prior days + today so far
        return [b for b in bars if session_date(b.t) in keep]

    async def _ws(self) -> None:
        import websockets
        url = f"wss://stream.data.alpaca.markets/v2/{self.feed}"
        h = _headers()
        backoff = 1
        while True:
            try:
                async with websockets.connect(url, max_size=2 ** 23, ping_interval=15) as ws:
                    await ws.recv()
                    await ws.send(json.dumps({"action": "auth", "key": h["APCA-API-KEY-ID"], "secret": h["APCA-API-SECRET-KEY"]}))
                    auth = json.loads(await ws.recv())
                    if not any(m.get("msg") == "authenticated" for m in auth):
                        raise RuntimeError(f"alpaca auth failed: {auth}")
                    await ws.send(json.dumps({"action": "subscribe", "trades": [self.symbol]}))
                    backoff = 1
                    log.info("alpaca %s stream connected", self.feed)
                    async for raw in ws:
                        await self._handle(raw)
            except Exception as ex:
                log.warning("alpaca ws dropped (%s); reconnecting in %ss", ex, backoff)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def _handle(self, raw) -> None:
        for m in json.loads(raw):
            if m.get("T") == "t":
                for tr in self.prints.push(Trade(_ts(m["t"]), float(m["p"]), float(m["s"])), m.get("c")):
                    await self.q.put(tr)

    async def _beat(self) -> None:
        import time
        while True:
            await self.q.put(Heartbeat(time.time()))
            await asyncio.sleep(1)

    async def stream(self):
        tasks = [asyncio.create_task(self._ws()), asyncio.create_task(self._beat())]
        try:
            while True:
                yield await self.q.get()
        finally:
            for t in tasks:
                t.cancel()


class AlpacaQuotes(QuoteSource):
    name = "alpaca"

    def __init__(self, cfg):
        self.feed = cfg["data"]["alpaca"]["options_feed"]
        self.client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self.client = httpx.AsyncClient(headers=_headers(), timeout=5)

    async def quote(self, contract) -> Quote | None:
        r = await self.client.get(f"{DATA}/v1beta1/options/quotes/latest", params={"symbols": contract.occ, "feed": self.feed})
        if r.status_code != 200:
            log.warning("alpaca option quote %s: %s", r.status_code, r.text[:200])
            return None
        q = (r.json().get("quotes") or {}).get(contract.occ)
        if not q:
            return None
        return Quote(float(q["bp"]), float(q["ap"]), _ts(q["t"]))
