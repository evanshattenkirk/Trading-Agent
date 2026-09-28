"""Robinhood whole-share orders for book F (docs/BOOK_F_HANDOFF.md section 4.1).

review_equity_order first, always. Shadow stops there and fills on paper. Live (refused unless live_enabled is true
and Evan set robinhood.account_number) then calls place_equity_order with a ref_id, polls get_equity_orders and
cancels on timeout. Orders are limit, gfd, regular hours, whole-share quantity as a string. Argument names follow
the option tools and go through fit_args, so unknown keys are dropped; check them with `rh-inspect`.
Book F is paper_only, so in this build nothing here places a real order.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from .base import OrderResult, OrderStateError
from .robinhood import TERMINAL, _short, dict_items, find_key

log = logging.getLogger("agentdesk.robinhood_equity")


def equity_order_args(account: str, symbol: str, side: str, qty: int, limit: float) -> dict:
    return {"account_number": account, "symbol": symbol, "side": side, "quantity": str(int(qty)),
            "price": f"{limit:.2f}", "type": "limit", "time_in_force": "gfd", "market_hours": "regular_hours"}


class RobinhoodEquityBroker:
    def __init__(self, rh, paper, live: bool, cfg, fill_timeout: float = 3.0, poll_s: float = 0.4, reprices: int = 2):
        if live and not (cfg.get("live_enabled") and (cfg.get("robinhood") or {}).get("account_number")):
            raise SystemExit("Refusing live equity orders: set live_enabled: true and robinhood.account_number first.")
        self.rh, self.paper, self.live = rh, paper, live
        self.name = "robinhood-equity-live" if live else "robinhood-equity-shadow"
        self.fill_timeout, self.poll_s, self.reprices = fill_timeout, poll_s, reprices
        self.open_orders: set[str] = set()

    async def _review(self, args: dict):
        try:
            return _short(await self.rh.call("review_equity_order", args))
        except Exception as ex:
            return {"error": str(ex)[:200]}

    async def buy(self, sym: str, qty: int, limit: float, trigger: float, now: float) -> OrderResult:
        return await self._order(sym, "buy", qty, limit, now, lambda: self.paper.buy(sym, qty, limit, trigger, now))

    async def sell(self, sym: str, qty: int, limit: float, now: float, stop: float | None = None) -> OrderResult:
        return await self._order(sym, "sell", qty, limit, now, lambda: self.paper.sell(sym, qty, limit, now, stop))

    def bar_stop(self, bar: dict, stop: float) -> float | None:
        return self.paper.bar_stop(bar, stop)

    async def _order(self, sym, side, qty, limit, now, paper_fill) -> OrderResult:
        args = equity_order_args(self.rh.account, sym, side, qty, limit)
        review = await self._review(args)
        if not self.live:
            res = await paper_fill()
            res.review = review
            return res
        args = {**args, "ref_id": str(uuid.uuid4())}
        try:
            placed = await self.rh.call("place_equity_order", args)
        except Exception:
            try:
                placed = await self.rh.call("place_equity_order", args)     # same ref_id: no duplicate
            except Exception as ex:
                raise OrderStateError(f"place_equity_order failed twice, order state unknown: {ex}") from ex
        oid = str(find_key(placed, ["id", "order_id"]) or "")
        if not oid:
            raise OrderStateError(f"place_equity_order returned no order id: {_short(placed, 200)}")
        self.open_orders.add(oid)
        rec = await self._poll(oid, self.fill_timeout)
        if not rec or rec[0] not in TERMINAL:
            try:
                await self.rh.call("cancel_equity_order", {"account_number": self.rh.account, "order_id": oid})
            except Exception as ex:
                log.warning("cancel %s failed: %s", oid, ex)
            rec = await self._poll(oid, max(1.0, self.fill_timeout)) or rec
        if not rec or rec[0] not in TERMINAL:
            raise OrderStateError(f"equity order {oid} not confirmed final after cancel", oid,
                                  rec[1] if rec else 0, rec[2] if rec else 0.0)
        self.open_orders.discard(oid)
        state, filled, avg = rec
        status = "filled" if filled >= qty else "partial" if filled else ("rejected" if state in ("rejected", "failed") else "unfilled")
        return OrderResult(status, filled, avg or limit, oid, state, review, {"placed": _short(placed)})

    async def _poll(self, oid: str, timeout: float):
        deadline, rec = time.time() + timeout, None
        while True:
            await asyncio.sleep(self.poll_s)
            rec = await self._read(oid) or rec
            if (rec and rec[0] in TERMINAL) or time.time() >= deadline:
                return rec

    async def _read(self, oid: str):
        try:
            data = await self.rh.call("get_equity_orders", {"account_number": self.rh.account, "order_id": oid})
        except Exception as ex:
            log.warning("get_equity_orders %s failed: %s", oid, ex)
            return None
        rec = next(iter(dict_items(data.get("orders", data) if isinstance(data, dict) else data)), None)
        if rec is None:
            return None
        filled = float(rec.get("cumulative_quantity") or rec.get("filled_quantity") or rec.get("processed_quantity") or 0)
        return str(rec.get("state") or "").lower(), int(filled), float(rec.get("average_price") or 0)

    async def positions(self) -> list[dict]:
        data = await self.rh.call("get_equity_positions", {"account_number": self.rh.account})
        return [p for p in dict_items(data.get("positions", data) if isinstance(data, dict) else data)
                if float(find_key(p, ["quantity", "shares", "qty"]) or 0) > 0]

    async def cancel_all(self) -> None:
        for oid in list(self.open_orders):
            try:
                await self.rh.call("cancel_equity_order", {"account_number": self.rh.account, "order_id": oid})
            except Exception:
                pass
        self.open_orders.clear()


async def inspect_equity(rh, acct: str) -> None:
    """rh-inspect's equity section (read-only): tradability for 3 names and a review of 1 share of SPY."""
    from .robinhood import redact_account
    print("\nEquity (book F):")
    try:
        t = await rh.call("get_equity_tradability", {"symbols": ["NVDA", "AAPL", "MU"]})
        print("get_equity_tradability:", redact_account(json.dumps(t, default=str), acct)[:1200])
    except Exception as ex:
        print(f"get_equity_tradability failed: {ex}")
    try:
        q = await rh.call("get_equity_quotes", {"symbols": ["SPY"]})
        px = float(find_key(q, ["bid_price", "last_trade_price"]) or 0)
        args = equity_order_args(acct, "SPY", "buy", 1, max(0.01, round(px * 0.98, 2)))
        print("review_equity_order (simulation, nothing placed):")
        print(redact_account(json.dumps(await rh.call("review_equity_order", args), indent=2, default=str), acct)[:3000])
    except Exception as ex:
        print(f"review_equity_order failed: {ex}")
    for tool in ("review_equity_order", "place_equity_order", "get_equity_orders", "cancel_equity_order",
                 "get_equity_positions", "get_equity_historicals", "get_equity_tradability"):
        props = list(((rh.tools.get(tool) or {}).get("properties") or {}))
        print(f"  {tool}: {props or 'not offered'}")
