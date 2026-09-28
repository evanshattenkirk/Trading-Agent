"""Massive (formerly Polygon.io): SPY trades over websocket, 1m aggregates, option snapshots.

Env: MASSIVE_API_KEY (POLYGON_API_KEY also accepted). Real-time trades need a Stocks plan with
real-time data; option quotes need an Options plan with quotes. URLs are configurable because
the api.polygon.io -> api.massive.com migration is ongoing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..bars import Bar, Trade
from ..clock import is_rth, session_date
from .base import Feed, Heartbeat, Quote, QuoteSource

log = logging.getLogger("agentdesk.massive")


def _key() -> str:
    k = os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY")
    if not k:
        raise SystemExit("Set MASSIVE_API_KEY in .env")
    return k


class MassiveFeed(Feed):
    name = "massive"

    def __init__(self, cfg):
        self.symbol = cfg["symbol"]
        self.rest = cfg["data"]["massive"]["rest_url"]
        self.ws_url = cfg["data"]["massive"]["ws_url"]
        self.q: asyncio.Queue = asyncio.Queue(maxsize=200000)

    async def history_1m(self, days: int) -> list[Bar]:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days + 5)
        url = f"{self.rest}/v2/aggs/ticker/{self.symbol}/range/1/minute/{start}/{end}"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url, params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": _key()})
            r.raise_for_status()
            bars = [Bar("1m", b["t"] / 1000, b["o"], b["h"], b["l"], b["c"], b["v"], b.get("n", 0), b["t"] / 1000 + 60)
                    for b in r.json().get("results") or [] if is_rth(b["t"] / 1000)]
        keep = set(sorted({session_date(b.t) for b in bars})[-(days + 1):])
        return [b for b in bars if session_date(b.t) in keep]

    async def _ws(self) -> None:
        import websockets
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.ws_url, max_size=2 ** 23, ping_interval=15) as ws:
                    await ws.recv()
                    await ws.send(json.dumps({"action": "auth", "params": _key()}))
                    await ws.recv()
                    await ws.send(json.dumps({"action": "subscribe", "params": f"T.{self.symbol}"}))
                    backoff = 1
                    async for raw in ws:
                        for m in json.loads(raw):
                            if m.get("ev") == "T":
                                await self.q.put(Trade(m["t"] / 1000, float(m["p"]), float(m.get("s", 0))))
            except Exception as ex:
                log.warning("massive ws dropped (%s); reconnecting in %ss", ex, backoff)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def _beat(self) -> None:
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


class MassiveQuotes(QuoteSource):
    name = "massive"

    def __init__(self, cfg):
        self.rest = cfg["data"]["massive"]["rest_url"]
        self.client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self.client = httpx.AsyncClient(timeout=5)

    async def quote(self, contract) -> Quote | None:
        r = await self.client.get(f"{self.rest}/v3/snapshot/options/{contract.symbol}/O:{contract.occ}", params={"apiKey": _key()})
        if r.status_code != 200:
            return None
        lq = (r.json().get("results") or {}).get("last_quote") or {}
        if "bid" not in lq:
            return None
        return Quote(float(lq["bid"]), float(lq["ask"]), time.time())
