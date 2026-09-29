"""Single-stock option chains for book E and the IV recorder (HANDOFF 7E; spec
docs/superpowers/specs/2026-09-29-book-e-design.md).

Pure helpers: expiry pickers (front, pre- and post-announcement, ~30 days), the ATM strike, and a call pacer.
RobinhoodChains (below) is the read-only chain data both users share.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .feeds.base import Quote


@dataclass
class IVQuote(Quote):
    iv: float | None = None


def front_expiry(exps, today: date) -> date | None:
    return min((e for e in exps if e > today), default=None)


def d30_expiry(exps, today: date, days: int = 30) -> date | None:
    return min((e for e in exps if e > today), key=lambda e: (abs((e - today).days - days), e), default=None)


def pre_expiry(exps, d: date, timing: str) -> date | None:
    """Last expiry before the announcement: before D for am or unknown timing, on or before D for pm."""
    return max((e for e in exps if (e <= d if timing == "pm" else e < d)), default=None)


def post_expiry(exps, d: date, timing: str) -> date | None:
    """First expiry that carries the announcement: on or after D for am, after D for pm or unknown."""
    return min((e for e in exps if (e >= d if timing == "am" else e > d)), default=None)


def atm_strike(strike_sets: list, spot: float | None) -> float | None:
    """The strike nearest spot that is listed (call and put) in every given expiry; ties go to the lower strike."""
    if not strike_sets or not spot:
        return None
    common = set.intersection(*(set(s) for s in strike_sets))
    return min(common, key=lambda k: (abs(k - spot), k), default=None)


class Pacer:
    """At most `rate` calls per second: wait() sleeps until the next slot."""

    def __init__(self, rate: float, clock=time.monotonic, sleep=asyncio.sleep):
        self.gap, self.clock, self.sleep = 1.0 / rate, clock, sleep
        self.next = -1e18

    async def wait(self) -> None:
        now = self.clock()
        if now < self.next:
            await self.sleep(self.next - now)
            now = self.next
        self.next = now + self.gap

log = logging.getLogger("agentdesk.iv")


def _num(v) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


class RobinhoodChains:
    """Read-only chain data for single stocks (get_option_chains, get_option_instruments, get_option_quotes,
    get_equity_quotes). Expirations are read once per symbol per day; strike lists are cached on disk per
    (symbol, expiry) for LIST_MAX_AGE_DAYS; each quote pass is one batched get_option_quotes call."""

    LIST_MAX_AGE_DAYS = 3

    def __init__(self, rh, cache_dir=None, pacer=None, clock=time.time):
        self.rh, self.pacer, self.clock = rh, pacer, clock
        self.dir = Path(os.path.expanduser(str(cache_dir))) if cache_dir else None
        self._exps: dict[str, tuple[date, list[date]]] = {}
        self._chains: dict[tuple[str, date], dict[tuple[float, str], str]] = {}
        self.cache: dict[str, IVQuote] = {}

    async def _call(self, tool: str, args: dict):
        if self.pacer is not None:
            await self.pacer.wait()
        return await self.rh.call(tool, args)

    async def spots(self, symbols) -> dict[str, float]:
        from .brokers.robinhood import dict_items
        data = await self._call("get_equity_quotes", {"symbols": list(symbols)})
        out = {}
        for it in dict_items(data):
            q = it.get("quote") if isinstance(it.get("quote"), dict) else it
            sym = str(q.get("symbol") or it.get("symbol") or "").upper()
            px = _num(q.get("last_trade_price"))
            if not px:
                b, a = _num(q.get("bid_price")), _num(q.get("ask_price"))
                px = round((b + a) / 2, 4) if b and a else None
            if sym and px:
                out[sym] = px
        return out

    async def expirations(self, symbol: str) -> list[date]:
        from .brokers.robinhood import find_key
        from .clock import session_date
        from .earnings import _day
        today = session_date(self.clock())
        hit = self._exps.get(symbol)
        if hit and hit[0] == today:
            return hit[1]
        # the schema isn't verified yet: fit_args keeps whichever of these names the server declares
        data = await self._call("get_option_chains", {"symbol": symbol, "symbols": [symbol], "chain_symbol": symbol,
                                                      "equity_symbol": symbol})
        raw = find_key(data, ["expiration_dates", "expirations"])
        if not isinstance(raw, list):
            keys = sorted(data.keys()) if isinstance(data, dict) else type(data).__name__
            raise RuntimeError(f"get_option_chains {symbol}: no expiration_dates in the response (keys: {keys})")
        exps = sorted({d for d in (_day(x) for x in raw) if d})
        self._exps[symbol] = (today, exps)
        return exps

    async def strikes(self, symbol: str, expiry: date) -> set[float]:
        ch = await self._chain(symbol, expiry)
        return {k for (k, r) in ch if r == "call" and (k, "put") in ch}

    def contract(self, symbol: str, expiry: date, strike: float, right: str):
        from .exits import Contract
        ch = self._chains.get((symbol, expiry)) or {}
        return Contract(symbol, str(expiry), float(strike), right, ch.get((float(strike), right)))

    async def quotes(self, contracts) -> list[IVQuote | None]:
        from .brokers.robinhood import dict_items, find_key
        ids = [c.broker_id for c in contracts]
        want = [i for i in dict.fromkeys(ids) if i]
        if not want:
            return [None] * len(ids)
        data = await self._call("get_option_quotes", {"instrument_ids": want})
        now = self.clock()
        for item in dict_items(data):
            q = item.get("quote") if isinstance(item.get("quote"), dict) else item
            oid = str(item.get("instrument_id") or q.get("instrument_id") or find_key(item, ["instrument_id", "id"]) or "")
            ask = _num(q.get("ask_price", q.get("ask")))
            if oid and ask is not None:          # no bid means nobody bids: 0, not "no quote"
                self.cache[oid] = IVQuote(_num(q.get("bid_price", q.get("bid"))) or 0.0, ask, now,
                                          _num(q.get("implied_volatility", q.get("iv"))))
        return [self.cache.get(i) if i else None for i in ids]

    async def quote(self, contract) -> IVQuote | None:
        return self.cache.get(contract.broker_id) if contract.broker_id else None

    # ------------------------------------------------------------ strike lists
    async def _chain(self, symbol: str, expiry: date) -> dict:
        key = (symbol, expiry)
        if key not in self._chains:
            ch = self._load(symbol, expiry)
            if ch is None:
                ch = await self._list(symbol, expiry)
                self._save(symbol, expiry, ch)
            self._chains[key] = ch
        return self._chains[key]

    async def _list(self, symbol: str, expiry: date) -> dict:
        from .brokers.robinhood import dict_items
        exp, ch, cursor = str(expiry), {}, None
        for _ in range(40):
            args = {"chain_symbol": symbol, "expiration_dates": exp, "state": "active"}
            if cursor:
                args["cursor"] = cursor
            data = await self._call("get_option_instruments", args)
            for it in dict_items(data):
                if str(it.get("expiration_date") or exp) != exp or not it.get("id"):
                    continue
                try:
                    ch[(float(it["strike_price"]), str(it["type"]).lower())] = str(it["id"])
                except (KeyError, TypeError, ValueError):
                    continue
            cursor = data.get("next") if isinstance(data, dict) else None
            if not cursor:
                break
        return ch

    def _path(self, symbol: str, expiry: date) -> Path:
        return self.dir / f"{symbol}_{expiry}.json"

    def _load(self, symbol: str, expiry: date) -> dict | None:
        if self.dir is None:
            return None
        try:
            d = json.loads(self._path(symbol, expiry).read_text())
            if self.clock() - float(d["listed"]) > self.LIST_MAX_AGE_DAYS * 86400:
                return None
            return {(float(k.split("|")[0]), k.split("|")[1]): v for k, v in d["ids"].items()}
        except (OSError, ValueError, KeyError, IndexError):
            return None

    def _save(self, symbol: str, expiry: date, ch: dict) -> None:
        if self.dir is None or not ch:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self._path(symbol, expiry)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")       # the recorder and the engine share this folder
        tmp.write_text(json.dumps({"listed": self.clock(), "ids": {f"{k}|{r}": v for (k, r), v in ch.items()}}))
        os.replace(tmp, p)
