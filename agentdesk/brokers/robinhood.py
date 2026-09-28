"""Robinhood Trading MCP client (https://agent.robinhood.com/mcp/trading).

- OAuth 2.1 + PKCE via the MCP SDK; tokens cached at ~/.agentdesk/rh_oauth.json (0600).
- Arguments follow the server's published tool schemas (verified 2026-09-27): account_number,
  legs[{option_id, side, position_effect, ratio_quantity}], quantity/price as strings,
  type 'limit', time_in_force 'gfd', ref_id idempotency key on place_option_order.
- Every order goes to review_option_order first. shadow mode stops there (and paper-fills);
  live mode then calls place_option_order and polls get_option_orders.
- The Agentic account is the one with agentic_allowed=true. Live mode also requires you to
  set robinhood.account_number yourself, and refuses to run if the account's option_level
  is empty (options not approved on that account).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import uuid
import time
import webbrowser
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..feeds.base import Quote, QuoteSource
from .base import Broker, OrderResult, OrderStateError

TERMINAL = ("filled", "cancelled", "canceled", "rejected", "failed", "voided")
from .paper import PaperBroker

log = logging.getLogger("agentdesk.robinhood")

class SchemaError(Exception):
    pass


def fit_args(tool: str, schema: dict, args: dict) -> dict:
    """Drop None/unknown keys (schemas are additionalProperties:false) and check required fields."""
    props = (schema or {}).get("properties") or {}
    out = {k: v for k, v in args.items() if v is not None and (not props or k in props)}
    missing = [r for r in (schema or {}).get("required", []) if r not in out]
    if missing:
        raise SchemaError(f"{tool}: missing required {missing}; server schema props: {list(props)}")
    return out


def find_key(obj, keys, depth=0):
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            r = find_key(v, keys, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, keys, depth + 1)
            if r is not None:
                return r
    return None


def dict_items(obj) -> list[dict]:
    """Flatten a response into the list of record dicts it most likely contains."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for k in ("results", "data", "items", "instruments", "options", "quotes", "orders", "accounts"):
            if isinstance(obj.get(k), list):
                return [x for x in obj[k] if isinstance(x, dict)]
        for v in obj.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        return [obj]
    return []


# --------------------------------------------------------------------------- OAuth plumbing
class FileTokenStorage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _write(self, d: dict) -> None:
        self.path.write_text(json.dumps(d))
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        d = self._read().get("tokens")
        return OAuthToken.model_validate(d) if d else None

    async def set_tokens(self, tokens) -> None:
        d = self._read()
        d["tokens"] = tokens.model_dump(mode="json")
        self._write(d)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        d = self._read().get("client")
        return OAuthClientInformationFull.model_validate(d) if d else None

    async def set_client_info(self, info) -> None:
        d = self._read()
        d["client"] = info.model_dump(mode="json")
        self._write(d)


class _Callback:
    """One-shot localhost HTTP server that captures ?code=&state= from the OAuth redirect."""

    def __init__(self, port: int):
        self.port = port
        self.fut: asyncio.Future | None = None
        self.server = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.fut = loop.create_future()

        async def handle(reader, writer):
            line = (await reader.readline()).decode()
            while (await reader.readline()) not in (b"\r\n", b""):
                pass
            path = line.split(" ")[1] if " " in line else "/"
            qs = parse_qs(urlparse(path).query)
            body = b"<h3>AgentDesk is connected to Robinhood. You can close this tab.</h3>"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            writer.close()
            if "code" in qs and not self.fut.done():
                self.fut.set_result((qs["code"][0], qs.get("state", [None])[0]))

        self.server = await asyncio.start_server(handle, "127.0.0.1", self.port)

    async def wait(self):
        await self.start() if self.server is None else None
        try:
            return await asyncio.wait_for(self.fut, timeout=300)
        finally:
            self.server.close()
            self.server = None


class RobinhoodMCP:
    def __init__(self, cfg):
        self.cfg = cfg["robinhood"]
        self.url = self.cfg["mcp_url"]
        self.stack: AsyncExitStack | None = None
        self.session = None
        self.tools: dict[str, dict] = {}
        self.account: str | None = str(self.cfg["account_number"]) if self.cfg.get("account_number") else None
        self.account_info: dict = {}
        self._lock = asyncio.Lock()
        self._started = False
        self.chain_symbol = cfg.get("symbol", "SPY")

    async def start(self) -> None:
        if self._started:
            return
        from mcp import ClientSession
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.shared.auth import OAuthClientMetadata

        port = self.cfg["redirect_port"]
        cb = _Callback(port)

        async def redirect(url: str) -> None:
            await cb.start()
            print(f"\nOpen this URL to authorize AgentDesk with Robinhood (desktop browser):\n{url}\n")
            webbrowser.open(url)

        provider = OAuthClientProvider(
            server_url=self.url,
            client_metadata=OAuthClientMetadata(
                client_name="AgentDesk", redirect_uris=[f"http://localhost:{port}/callback"],
                grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                token_endpoint_auth_method="none"),
            storage=FileTokenStorage(Path(os.path.expanduser(self.cfg["token_dir"])) / "rh_oauth.json"),
            redirect_handler=redirect, callback_handler=cb.wait)
        self.stack = AsyncExitStack()
        read, write, _ = await self.stack.enter_async_context(streamablehttp_client(self.url, auth=provider, timeout=30))
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        listed = await self.session.list_tools()
        self.tools = {t.name: (t.inputSchema or {}) for t in listed.tools}
        self._started = True
        log.info("Robinhood MCP connected: %d tools", len(self.tools))
        await self._load_accounts()

    async def close(self) -> None:
        if self.stack:
            await self.stack.aclose()

    async def call(self, tool: str, args: dict):
        if tool not in self.tools:
            raise SchemaError(f"tool {tool} not offered by the server (have: {sorted(self.tools)})")
        args = fit_args(tool, self.tools[tool], args)
        async with self._lock:
            try:
                res = await self.session.call_tool(tool, args)
            except Exception:
                if tool.startswith(("get_", "review_")):          # read-only: one retry on a transient failure
                    await asyncio.sleep(0.3)
                    res = await self.session.call_tool(tool, args)
                else:
                    raise
            if getattr(res, "isError", False) and tool.startswith(("get_", "review_")) and "500" in _text(res):
                await asyncio.sleep(0.3)
                res = await self.session.call_tool(tool, args)
        if getattr(res, "isError", False):
            raise RuntimeError(f"{tool} error: {_text(res)[:400]}")
        data = getattr(res, "structuredContent", None)
        if not data:
            txt = _text(res)
            try:
                data = json.loads(txt)
            except json.JSONDecodeError:
                return {"text": txt}
        return data.get("data", data) if isinstance(data, dict) else data

    async def _load_accounts(self) -> None:
        data = await self.call("get_accounts", {})
        accts = data.get("accounts", []) if isinstance(data, dict) else []
        agentic = [a for a in accts if a.get("agentic_allowed")]
        if self.account:
            self.account_info = next((a for a in accts if str(a.get("account_number")) == self.account), {})
            if not self.account_info.get("agentic_allowed"):
                raise SystemExit(f"robinhood.account_number ...{self.account[-4:]} is not the agent-tradable account")
        elif agentic:
            self.account_info = agentic[0]
            self.account = str(agentic[0]["account_number"])
        lvl = self.account_info.get("option_level") or "none"
        log.info("Agentic account ...%s type=%s option_level=%s", (self.account or "????")[-4:],
                 self.account_info.get("type"), lvl)

    @property
    def option_level(self) -> str:
        return self.account_info.get("option_level") or ""

    async def instrument_id(self, contract) -> str | None:
        if contract.broker_id:
            return contract.broker_id
        data = await self.call("get_option_instruments", {
            "chain_symbol": contract.symbol, "expiration_dates": contract.expiry,
            "strike_price": f"{float(contract.strike):.4f}", "type": contract.right,
            "state": getattr(contract, "state", None) or "active"})
        for it in dict_items(data):
            sp = find_key(it, ["strike_price", "strike"])
            ty = str(find_key(it, ["type", "option_type"]) or "").lower()
            ex = str(find_key(it, ["expiration_date", "expiration"]) or "")
            if sp is not None and abs(float(sp) - contract.strike) < 1e-6 and (not ty or ty.startswith(contract.right[0])) \
                    and (not ex or ex.startswith(contract.expiry)):
                contract.broker_id = str(it.get("id") or find_key(it, ["id", "option_id", "instrument_id"]))
                so = it.get("sellout_datetime")
                if so:        # Robinhood force-closes at this time (14:45 CT for SPY 0DTE when checked)
                    from datetime import datetime as _dt
                    contract.sellout_ts = _dt.fromisoformat(so.replace("Z", "+00:00")).timestamp()
                return contract.broker_id
        return None

    async def price_book(self, symbol: str) -> dict | None:
        data = await self.call("get_equity_price_book", {"symbols": [symbol]})
        books = data.get("books", []) if isinstance(data, dict) else []
        return books[0] if books else None

    async def equity_bars(self, symbol: str, start_iso: str, end_iso: str, interval: str = "minute") -> list[dict]:
        data = await self.call("get_equity_historicals", {"symbols": [symbol], "start_time": start_iso,
                                                         "end_time": end_iso, "interval": interval, "bounds": "regular"})
        res = data.get("results", []) if isinstance(data, dict) else []
        return [b for b in (res[0].get("bars", []) if res else []) if not b.get("interpolated")]

    async def option_bars(self, instrument_id: str, start_iso: str, end_iso: str, interval: str = "minute") -> list[dict]:
        data = await self.call("get_option_historicals", {"instrument_ids": [instrument_id], "start_time": start_iso,
                                                         "end_time": end_iso, "interval": interval})
        res = data.get("results", []) if isinstance(data, dict) else []
        return [b for b in (res[0].get("bars", []) if res else []) if not b.get("interpolated")]


def _text(res) -> str:
    return "".join(getattr(c, "text", "") for c in (res.content or []))


# --------------------------------------------------------------------------- quotes + broker
class OptionQuoteRecorder:
    """Records real 0DTE SPY quotes into the journal: calls and puts ATM-10..ATM+10 (covers B's +-$5 wings and D's
    +-0.9 EM shorts + $2 wings).
    Calls settle naked-vs-debit-spread on real prices; the ATM call+put pair measures what the straddle
    really costs vs the move that follows (the variance-risk-premium check behind the iron-fly idea)."""

    def __init__(self, rh: RobinhoodMCP, journal, every: float = 10.0):
        self.rh, self.journal, self.every = rh, journal, every

    async def run(self, price_fn, now_fn) -> None:
        from ..clock import is_rth, session_date
        from ..exits import Contract
        while True:
            try:
                now, px = now_fn(), price_fn()
                if px and is_rth(now):
                    exp = str(session_date(now))
                    atm = round(px)
                    cs = [Contract("SPY", exp, float(atm + k), r) for r in ("call", "put") for k in range(-10, 11)]
                    ids = {}
                    for c in cs:
                        oid = await self.rh.instrument_id(c)
                        if oid:
                            ids[oid] = (c.strike, c.right)
                    if ids:
                        data = await self.rh.call("get_option_quotes", {"instrument_ids": list(ids)})
                        rows = []
                        for q in dict_items(data.get("quotes", data) if isinstance(data, dict) else data):
                            oid = str(q.get("instrument_id") or find_key(q, ["instrument_id", "id"]) or "")
                            if oid in ids and q.get("bid_price") is not None:
                                rows.append((now, exp, ids[oid][0], ids[oid][1], float(q["bid_price"]), float(q["ask_price"]), px))
                        self.journal.record_quotes(rows)
            except Exception as ex:
                log.debug("quote recorder: %s", ex)
            await asyncio.sleep(self.every)


class RobinhoodQuotes(QuoteSource):
    name = "robinhood"

    def __init__(self, rh: RobinhoodMCP, max_age: float = 0.5):
        self.rh = rh
        self.cache: dict[str, Quote] = {}
        self.max_age = max_age

    async def start(self) -> None:
        await self.rh.start()

    async def quote(self, contract) -> Quote | None:
        oid = await self.rh.instrument_id(contract)
        if not oid:
            return None
        q = self.cache.get(oid)
        if q and time.time() - q.ts < self.max_age:
            return q
        data = await self.rh.call("get_option_quotes", {"instrument_ids": [oid]})
        bid = find_key(data, ["bid_price", "bid"])
        ask = find_key(data, ["ask_price", "ask"])
        if bid is None or ask is None:
            return None
        q = Quote(float(bid), float(ask), time.time())
        self.cache[oid] = q
        return q


def order_args(account: str, legs: list[dict], qty: int, price: float, review: bool, symbol: str = "SPY") -> dict:
    a = {"account_number": account, "legs": legs, "quantity": str(int(qty)), "price": f"{price:.2f}",
         "type": "limit", "time_in_force": "gfd"}
    if len(legs) > 1:
        a["direction"] = "debit" if legs[0]["side"] == "buy" else "credit"
    if review:
        a.update({"chain_symbol": symbol, "underlying_type": "equity"})
    return a


class RobinhoodBroker(Broker):
    def __init__(self, rh: RobinhoodMCP, quotes, live: bool, fill_timeout: float = 2.5):
        self.rh, self.quotes, self.live = rh, quotes, live
        self.name = "robinhood-live" if live else "robinhood-shadow"
        self.paper = PaperBroker(quotes)
        self.fill_timeout = fill_timeout
        self.open_orders: set[str] = set()
        self.confirm_tries = 6

    async def start(self) -> None:
        await self.rh.start()
        if not self.rh.account:
            raise SystemExit("No agent-tradable Robinhood account found.")
        if self.rh.option_level in ("", "option_level_0"):
            raise SystemExit("Options are not approved on the Agentic account. Apply at "
                             f"https://applink.robinhood.com/upgrade_options?account_number={self.rh.account}")
        if self.live and not self.rh.cfg.get("account_number"):
            raise SystemExit("Live mode: set robinhood.account_number in config.yaml to your Agentic account number.")

    async def resolve(self, contract):
        await self.rh.instrument_id(contract)
        return contract

    async def submit(self, contract, side, qty, limit, now) -> OrderResult:
        legs = [{"option_id": contract.broker_id, "side": side, "position_effect": "open" if side == "buy" else "close"}]
        try:
            review = await self.rh.call("review_option_order", order_args(self.rh.account, legs, qty, limit, True, contract.symbol))
        except Exception as ex:
            return OrderResult("rejected", message=f"review failed: {ex}")
        if not self.live:
            res = await self.paper.submit(contract, side, qty, limit, now)
            res.review = _short(review)
            return res
        args = order_args(self.rh.account, legs, qty, limit, False)
        args["ref_id"] = str(uuid.uuid4())
        try:
            placed = await self.rh.call("place_option_order", args)
        except Exception:
            try:
                placed = await self.rh.call("place_option_order", args)   # one retry, same ref_id = no duplicate
            except Exception as ex:
                # the first call may have reached Robinhood; we can't know whether an order exists
                raise OrderStateError(f"place_option_order failed twice, order state unknown: {ex}") from ex
        oid = str(find_key(placed, ["id", "order_id"]) or "")
        if not oid:
            raise OrderStateError(f"place_option_order returned no order id: {_short(placed, 200)}")
        self.open_orders.add(oid)
        deadline = time.time() + self.fill_timeout
        rec = None
        while time.time() < deadline:
            await asyncio.sleep(0.4)
            rec = await self._read_order(oid) or rec
            if rec and rec[0] in TERMINAL:
                break
        if not rec or rec[0] not in TERMINAL:
            try:
                await self.rh.call("cancel_option_order", {"account_number": self.rh.account, "order_id": oid})
            except Exception as ex:
                log.warning("cancel %s failed: %s", oid, ex)
            # a fill can land between the last poll and the cancel: re-read until the order is final
            for _ in range(self.confirm_tries):
                await asyncio.sleep(0.5)
                rec = await self._read_order(oid) or rec
                if rec and rec[0] in TERMINAL:
                    break
        if not rec or rec[0] not in TERMINAL:
            filled, avg = (rec[1], _per_share(rec[2], limit)) if rec else (0, 0.0)
            raise OrderStateError(f"order {oid} not confirmed final after cancel (state {rec[0] if rec else 'unknown'}, "
                                  f"filled {filled})", oid, filled, avg)
        self.open_orders.discard(oid)
        state, filled, avg = rec
        avg = _per_share(avg, limit) or limit
        status = "filled" if filled >= qty else "partial" if filled else ("rejected" if state in ("rejected", "failed") else "unfilled")
        return OrderResult(status, filled, avg, oid, state, _short(review), {"placed": _short(placed)})

    async def _read_order(self, oid: str) -> tuple[str, int, float] | None:
        """(state, filled qty, avg price) or None if the read failed."""
        try:
            data = await self.rh.call("get_option_orders", {"account_number": self.rh.account, "order_id": oid})
        except Exception as ex:
            log.warning("get_option_orders %s failed: %s", oid, ex)
            return None
        rec = next(iter(dict_items(data.get("orders", data) if isinstance(data, dict) else data)), None)
        if rec is None:
            return None
        state = str(rec.get("state") or "").lower()
        filled = int(float(rec.get("processed_quantity") or rec.get("filled_quantity") or 0))
        avg = float(rec.get("average_price") or rec.get("processed_premium_per_contract") or rec.get("price") or 0)
        return state, filled, avg

    async def buying_power(self) -> float | None:
        try:
            v = find_key(await self.rh.call("get_portfolio", {"account_number": self.rh.account}),
                         ["option_buying_power", "buying_power", "cash_available_for_trading"])
            return float(v) if v is not None else None
        except Exception:
            return None

    async def open_positions(self) -> list:
        data = await self.rh.call("get_option_positions", {"account_number": self.rh.account, "nonzero": True})
        return [p for p in dict_items(data.get("positions", data) if isinstance(data, dict) else data)
                if float(find_key(p, ["quantity", "qty"]) or 0) > 0]

    async def position_qty(self) -> int | None:
        if not self.live:
            return None                 # shadow fills are simulated; the account holds nothing
        return int(sum(float(find_key(p, ["quantity", "qty"]) or 0) for p in await self.open_positions()))

    async def cancel_all(self) -> None:
        for oid in list(self.open_orders):
            try:
                await self.rh.call("cancel_option_order", {"account_number": self.rh.account, "order_id": oid})
            except Exception:
                pass
        self.open_orders.clear()


def _per_share(avg: float, limit: float) -> float:
    return avg / 100 if avg > 20 and limit < 20 else avg     # some fields report premium per contract


def _short(d, n=600):
    s = json.dumps(d, default=str)
    return json.loads(s) if len(s) <= n else {"truncated": s[:n]}


# --------------------------------------------------------------------------- rh-inspect (read-only)
async def inspect_main(cfg, out: str) -> None:
    from datetime import datetime
    from ..clock import CT
    from ..exits import Contract

    rh = RobinhoodMCP(cfg)
    await rh.start()
    Path(out).write_text(json.dumps(rh.tools, indent=2))
    print(f"\nConnected. {len(rh.tools)} tools. Schemas written to {out}")
    acct = rh.account or ""
    print(f"Agentic account: ...{acct[-4:] if acct else 'NOT FOUND'}  type={rh.account_info.get('type')}  "
          f"option_level={rh.option_level or 'NONE (apply: https://applink.robinhood.com/upgrade_options?account_number=' + acct + ')'}")
    try:
        bp = find_key(await rh.call("get_portfolio", {"account_number": acct}), ["option_buying_power", "buying_power"])
        print(f"Buying power: {bp}")
    except Exception as ex:
        print(f"portfolio failed: {ex}")
    q = await rh.call("get_equity_quotes", {"symbols": ["SPY"]})
    px = float(find_key(q, ["last_trade_price", "last_extended_hours_trade_price", "ask_price"]))
    print(f"SPY {px:.2f}")
    book = await rh.price_book("SPY")
    print(f"Level 2: {len(book.get('bids', [])) if book else 0} bid levels / {len(book.get('asks', [])) if book else 0} ask levels "
          f"(empty outside market hours)")
    today = datetime.now(CT).date().isoformat()
    c = Contract("SPY", today, float(int(px) + 1), "call")
    oid = await rh.instrument_id(c)
    print(f"Instrument {c.label}: {oid or 'none (no expiry today?)'}")
    if oid and rh.option_level:
        qt = await RobinhoodQuotes(rh).quote(c)
        print(f"Quote: {qt}")
        if qt:
            legs = [{"option_id": oid, "side": "buy", "position_effect": "open"}]
            print("review_option_order (simulation, nothing placed):")
            rev = await rh.call("review_option_order", order_args(acct, legs, 1, max(0.01, qt.bid), True))
            print(json.dumps(rev, indent=2, default=str)[:3000])
    await rh.close()
