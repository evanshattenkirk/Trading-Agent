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
import time as _time
from datetime import datetime, time, timedelta, timezone

import httpx

from ..bars import Bar, Trade
from ..clock import RTH_OPEN, at_ct, ct_time, is_rth, session_date
from .base import Feed, Heartbeat, Quote, QuoteSource
from .prints import PrintFilter

log = logging.getLogger("agentdesk.alpaca")
DATA = "https://data.alpaca.markets"
SIP_LAG = timedelta(minutes=16)     # the free plan refuses SIP history newer than 15 minutes; IEX history has no lag
STALL_SEC = 60                      # no print for this long in regular hours: warn (once per stall)


class StreamError(RuntimeError):
    """Alpaca sent {"T": "error"} (e.g. 406 connection limit exceeded): the socket may stay open and deliver nothing."""


def _errors(msgs: list, where: str) -> list:
    for m in msgs:
        if m.get("T") == "error":
            raise StreamError(f"{where}: error {m.get('code')} {m.get('msg')}")
    return msgs


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
        self.history_end: float | None = None      # where the warm-up history stops (the engine measures the gap)
        self.last_print = _time.time()             # last trade message from the socket (prints-stall warning)
        self.stalled = False
        cal = cfg.get("calendar") or {}
        self.holidays = {str(d) for d in cal.get("holidays") or []}
        self.half_days = {str(d) for d in cal.get("early_close") or []}

    async def history_1m(self, days: int) -> list[Bar]:
        now = datetime.now(timezone.utc)
        end = now - SIP_LAG if self.feed == "sip" else now
        try:
            bars = await fetch_bars_1m(self.symbol, end - timedelta(days=days + 4), end, self.feed)
        except httpx.HTTPStatusError as ex:
            if end != now or ex.response.status_code not in (403, 422):
                raise
            log.warning("alpaca %s history up to now refused (%s %s); using the 16-minute lag", self.feed,
                        ex.response.status_code, ex.response.text[:200])
            end = now - SIP_LAG
            bars = await fetch_bars_1m(self.symbol, end - timedelta(days=days + 4), end, self.feed)
        self.history_end = end.timestamp()
        keep = set(sorted({session_date(b.t) for b in bars})[-(days + 1):])     # N prior days + today so far
        return [b for b in bars if session_date(b.t) in keep]

    async def _ws(self) -> None:
        import websockets
        url = f"wss://stream.data.alpaca.markets/v2/{self.feed}"
        h = _headers()
        backoff = 1
        while True:
            up = None                       # when this connection's subscription was confirmed
            try:
                async with websockets.connect(url, max_size=2 ** 23, ping_interval=15, ping_timeout=30) as ws:
                    _errors(json.loads(await ws.recv()), "connect")
                    await ws.send(json.dumps({"action": "auth", "key": h["APCA-API-KEY-ID"], "secret": h["APCA-API-SECRET-KEY"]}))
                    auth = _errors(json.loads(await ws.recv()), "auth")
                    if not any(m.get("msg") == "authenticated" for m in auth):
                        raise RuntimeError(f"alpaca auth failed: {auth}")
                    await ws.send(json.dumps({"action": "subscribe", "trades": [self.symbol]}))
                    reply = _errors(json.loads(await asyncio.wait_for(ws.recv(), 10)), "subscribe")
                    sub = next((m for m in reply if m.get("T") == "subscription"), None)
                    if sub is not None and self.symbol not in (sub.get("trades") or []):
                        raise StreamError(f"subscribe: {self.symbol} trades not in {sub}")
                    up = _time.time()
                    log.info("alpaca %s stream connected: %s", self.feed, sub or reply)
                    await self._handle(reply)       # a print that came with the reply
                    async for raw in ws:
                        await self._handle(raw)
                    raise ConnectionError("stream closed by the server")
            except Exception as ex:
                if up is not None and (self.last_print >= up or _time.time() - up > STALL_SEC):
                    backoff = 1                 # this connection worked; an error right after subscribing doesn't count
                log.warning("alpaca ws dropped (%s); reconnecting in %ss", ex, backoff)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def _handle(self, raw) -> None:
        for m in json.loads(raw) if isinstance(raw, (str, bytes)) else raw:
            if m.get("T") == "t":
                self._printed()
                for tr in self.prints.push(Trade(_ts(m["t"]), float(m["p"]), float(m["s"])), m.get("c")):
                    await self.q.put(tr)
            elif m.get("T") == "error":           # any time: a drop, so the loop logs it and reconnects
                raise StreamError(f"error {m.get('code')} {m.get('msg')}")

    def _printed(self) -> None:
        now = _time.time()
        if self.stalled:
            self.stalled = False
            log.info("alpaca %s prints resumed after %.0f s", self.feed, now - self.last_print)
        self.last_print = now

    def _check_stall(self, now: float) -> None:
        """Warn once when no print has come for STALL_SEC during regular hours (a socket can stay up and deliver
        nothing); the quiet time counts from the open, and half-day afternoons and holidays are quiet anyway."""
        day = str(session_date(now))
        if not is_rth(now) or day in self.holidays or (day in self.half_days and ct_time(now) >= time(12, 0)):
            return
        quiet = now - max(self.last_print, at_ct(session_date(now), RTH_OPEN))
        if quiet >= STALL_SEC and not self.stalled:
            self.stalled = True
            log.warning("alpaca %s: no prints for %.0f s during regular hours", self.feed, quiet)

    async def _beat(self) -> None:
        while True:
            now = _time.time()
            self._check_stall(now)
            await self.q.put(Heartbeat(now))
            await asyncio.sleep(1)

    async def stream(self):
        self.last_print = _time.time()
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
