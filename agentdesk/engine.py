"""The trading engine: feed -> bars -> signals -> risk -> orders -> exits.

Deterministic. The LLM crew never sits in this path; it can only set restrictions
on the RiskManager (size multiplier, blackouts, cooldowns).
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from collections import deque
from datetime import time

from .bars import Bar, TickBarBuilder, TimeBarBuilder, Trade, VWAP
from .brokers.base import OrderResult, OrderStateError, RateLimited
from .clock import at_ct, ct, ct_time, hm, is_rth, session_date
from .exits import Contract, ExitIntent, ExitPlan, Position
from .feeds.base import Heartbeat
from .levels import Levels
from .risk import RiskManager
from .strategy import SignalEngine
from .strikes import choose_strike

log = logging.getLogger("agentdesk.engine")
MAX_BARS = {"144t": 4000, "1m": 400, "5m": 200, "15m": 120}
WATCHDOG = {"quote_stale_sec": 10, "max_consecutive_errors": 3, "reconcile_sec": 30}


class Engine:
    def __init__(self, cfg, feed, quotes, broker, bus, journal, mode: str = "sim"):
        self.cfg, self.feed, self.quotes, self.broker, self.bus, self.journal = cfg, feed, quotes, broker, bus, journal
        self.mode = mode
        self.symbol = cfg["symbol"]
        self.sig = SignalEngine(cfg["strategy"])
        self.tick_tf = self.sig.tick_tf
        self.tb = {tf: TimeBarBuilder(tf) for tf in ("1m", "5m", "15m")}
        # the "144t" series keeps its name; on the free IEX feed it is built from fewer prints (tick_bar_effective)
        self.tick = TickBarBuilder(cfg["strategy"].get("tick_bar_effective") or cfg["strategy"]["tick_bar_size"], self.tick_tf)
        self.vwap = VWAP()
        self.levels = Levels()
        self.risk = RiskManager(cfg)
        self.open: list[tuple[Position, ExitPlan]] = []
        self.closed: list[Position] = []
        self.bars: dict[str, deque] = {tf: deque(maxlen=n) for tf, n in MAX_BARS.items()}
        self.skips: deque = deque(maxlen=50)
        self.price: float | None = None
        self.day = None
        self.inline = feed.is_sim
        self.crew = None
        self.agent = {"activity": "offline", "text": "", "ts": 0.0}
        self._last_sec = 0
        self._last_manage = 0.0
        self._last_tick_emit = 0.0
        self._last_pos_emit = 0.0
        self._last_sig_emit = 0.0
        self._managing = False
        self.l2 = None              # L2Monitor
        self.l2_rh = None           # RobinhoodMCP for live books
        self.closers: list = []     # async close() callables run on shutdown (data feed, Robinhood session)
        self.simbook = None         # SimBook in the simulator
        self._last_l2_emit = 0.0
        self._day_orig: dict = {}   # original values of per-day crew tweaks, restored next session
        self.trade_tweaks: dict = {}
        self._last_tape = (0.0, None)
        self._busy = asyncio.Lock()
        self._stopped = False
        self.ocfg = cfg["orders"]
        self.wd = {**WATCHDOG, **(cfg["risk"].get("watchdog") or {})}
        self._errors = 0            # longest current run of failed background tasks (broker/API errors)
        self._streaks: dict[str, int] = {}     # per kind ("manage", "entry"...): one kind's success can't hide another's failures
        self._last_rate_log = -1e18
        self._last_reconcile = 0.0
        self._mismatches = 0
        self.books = None           # BookHost for paper books B/C/D (books/host.py); None when none is enabled

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        await self.broker.start()
        await self.quotes.start()
        await self._warmup()
        restored = self.risk.restore(str(self.day))      # a restart keeps today's P&L, counts, cooldown and halts
        if restored:
            self.bus.emit("log", self.feed.now(), level="warn" if self.risk.st.halted else "info", msg=restored)
        try:
            stale = await self.broker.open_positions()
        except Exception as ex:
            stale = None
            self.risk.halt(f"could not read account positions at startup ({ex}); fix the connection, then restart", sticky=False)
            self.bus.emit("log", self.feed.now(), level="error", msg=self.risk.st.halt_reason)
        if stale:
            self.risk.halt(f"{len(stale)} option position(s) already open in the account at startup; close them in the app, then restart",
                           sticky=False)
            self.bus.emit("log", self.feed.now(), level="warn", msg=self.risk.st.halt_reason)
        if self.books:
            if self.risk.st.halted:
                self.books.halt_all(self.risk.st.halt_reason)
            await self.books.start()
        self.set_agent(self.feed.now(), "arriving", "Booting up. Loading history...")
        if self.l2 and self.l2.enabled and self.l2_rh is not None and not self.inline:
            asyncio.create_task(self.l2.run_live(self.l2_rh, lambda st: self._on_l2(st, st.ts)))
        if self.l2_rh is not None and not self.inline and self.cfg["robinhood"].get("record_option_quotes", True):
            from .brokers.robinhood import OptionQuoteRecorder
            asyncio.create_task(OptionQuoteRecorder(self.l2_rh, self.journal).run(lambda: self.price, self.feed.now))
        async for item in self.feed.stream():
            if self._stopped:
                break
            await self.on_item(item)
        self.set_agent(self.feed.now(), "offline", "Session over.")

    def stop(self) -> None:
        self._stopped = True

    async def _warmup(self) -> None:
        hist = await self.feed.history_1m(self.cfg["data"]["history_days"])
        agg = {tf: TimeBarBuilder(tf) for tf in ("5m", "15m")}
        today = session_date(self.feed.now())
        last_day, dh, dl, dc = None, None, None, None
        n_today = 0
        for b in hist:
            d = session_date(b.t)
            closed = [b]
            for tf, bld in agg.items():
                c = bld.on_bar(b)
                if c:
                    closed.append(c)
            for x in closed:
                self.sig.on_bar_close(x)
            if d == today:                     # started mid-session: rebuild today's chart + levels
                n_today += 1
                self.levels.on_1m(b)
                self.vwap.add((b.h + b.l + b.c) / 3, b.v)
                for x in closed:
                    if x.tf == "5m":
                        self.levels.on_5m(x)
                    st = self.sig.tf[x.tf]
                    self.bars[x.tf].append({"tf": x.tf, "t": x.t, "end": x.end, "o": x.o, "h": x.h, "l": x.l, "c": x.c,
                                            "v": x.v, "macd": st.last.macd if st.last else None,
                                            "sig": st.last.signal if st.last else None, "rsi": st.rsi_val})
                self.price = b.c
                continue
            if last_day is not None and d != last_day:
                dh = dl = None
            last_day = d
            dh = b.h if dh is None else max(dh, b.h)
            dl = b.l if dl is None else min(dl, b.l)
            dc = b.c
        if dc is not None:
            self.levels.set_prior_day(dh, dl, dc)
            self.price = self.price or dc
        for bld in agg.values():               # partial 5m/15m from history must not bleed into live bars
            bld.cur = None
        self.day = today
        self.bus.emit("log", self.feed.now(), level="info",
                      msg=f"Warm-up: {len(hist)} 1m bars from {self.feed.name} ({n_today} from today); prior day H/L/C "
                          f"{dh and round(dh, 2)}/{dl and round(dl, 2)}/{dc and round(dc, 2)}")

    # ------------------------------------------------------------------ feed handling
    async def on_item(self, item) -> None:
        now = item.ts
        if isinstance(item, Trade):
            await self._on_trade(item)
        sec = int(now)
        if sec != self._last_sec:
            self._last_sec = sec
            await self._on_second(now)

    async def _on_trade(self, tr: Trade) -> None:
        now = tr.ts
        self.price = tr.px
        closed: list[Bar] = []
        if is_rth(now):
            self.vwap.add(tr.px, tr.sz)
            for tf in ("15m", "5m", "1m"):          # slow TFs first so filters are current when triggers fire
                c = self.tb[tf].on_trade(tr)
                if c:
                    closed.append(c)
        c = self.tick.on_trade(tr)
        if c:
            closed.append(c)
        for b in closed:
            await self._on_bar(b)
        if is_rth(now):
            for tf in ("1m", "5m", "15m"):
                self.sig.on_live_price(tf, tr.px)
        wall = _time.time()
        if wall - self._last_tick_emit > 0.25:
            self._last_tick_emit = wall
            self.bus.emit("tick", now, price=tr.px, vwap=self.vwap.value)

    async def _on_second(self, now: float) -> None:
        d = session_date(now)
        if self.day is not None and d != self.day and ct_time(now) < time(8, 30):
            self._new_day(d, now)
        for tf in ("1m", "5m", "15m"):
            c = self.tb[tf].flush(now)
            if c:
                await self._on_bar(c)
        if self.crew:
            await self.crew.on_clock(now)
        if self.simbook is not None and self.l2 and self.l2.enabled and is_rth(now) and self.price:
            st = self.l2.update(self.simbook.book(self.price), now)
            if st:
                self._on_l2(st, now)
        self._update_idle(now)
        poll = self.cfg["robinhood"]["quote_poll_ms"] / 1000
        if self.open and now - self._last_manage >= poll:
            self._last_manage = now
            await self._run(self.manage(now), "manage")
        if self.books:
            await self.books.run(self.books.on_second(now), self.inline)
        self._watchdog(now)

    def _new_day(self, d, now: float) -> None:
        from .proposals import set_path
        for k, v in self._day_orig.items():
            set_path(self.cfg, k, v)
        self._day_orig.clear()
        self.trade_tweaks.clear()
        self._streaks.clear()
        self._errors = 0
        if self.levels.hod is not None and self.price is not None:
            self.levels.set_prior_day(self.levels.hod, self.levels.lod, self.price)
        self.day = d
        self.levels.reset_session()
        self.vwap.reset()
        self.tick.reset()
        self.risk.reset_day(str(d))
        self.closed.clear()
        for q in self.bars.values():
            q.clear()
        self.bus.emit("session", now, day=str(d))

    def _on_l2(self, st, now: float) -> None:
        gap = 15.0 if self.inline else 1.0
        clock = now if self.inline else _time.time()
        if clock - self._last_l2_emit >= gap:
            self._last_l2_emit = clock
            ok, why = self.l2.gate("call", self.price or st.mid, now)
            self.bus.emit("l2", now, book=st.to_dict(), mode=self.l2.mode, gate_ok=ok, gate_why=why)
        # the tape reader speaks up about big walls near price, at most every 3 minutes
        last_t, last_px = self._last_tape
        w = st.ask_wall if st.ask_wall and st.ask_wall[0] - st.mid <= 0.30 and st.ask_wall[1] >= 25000 else None
        if w and w[0] != last_px and now - last_t > 1200 and is_rth(now):
            self._last_tape = (now, w[0])
            self.bus.emit("crew", now, desk="tape", phase="say", who="tape",
                          text=f"{w[1] / 1000:.0f}k sitting on the {w[0]:.2f} offer")

    async def _run(self, coro, what: str = "task") -> None:
        """Inline in the simulator (deterministic), background task when live. Either way errors are caught,
        logged and counted, never lost."""
        if self.inline:
            await self._guard(coro, what)
        else:
            asyncio.create_task(self._guard(coro, what))

    async def _guard(self, coro, what: str) -> None:
        try:
            await coro
            self._streaks.pop(what, None)
            self._errors = max(self._streaks.values(), default=0)
        except asyncio.CancelledError:
            raise
        except RateLimited as ex:           # Robinhood is pacing the account: skip this round; stale quotes still trip the watchdog
            now = self.feed.now()
            if now - self._last_rate_log >= 60:
                self._last_rate_log = now
                self.bus.emit("log", now, level="warn", msg=f"{what}: Robinhood rate limit, calls paused and retried ({str(ex)[:120]})")
        except Exception as ex:
            now = self.feed.now()
            self._streaks[what] = self._streaks.get(what, 0) + 1
            self._errors = max(self._streaks.values())
            log.exception("%s failed (%d in a row)", what, self._errors)
            self.bus.emit("log", now, level="error", msg=f"{what} failed ({self._errors} in a row): {ex}")
            if self._errors >= self.wd["max_consecutive_errors"]:
                self._trip(f"{self._errors} broker/API errors in a row (last: {what}: {ex})", now)

    # ------------------------------------------------------------------ safety watchdog
    def _trip(self, reason: str, now: float) -> None:
        """Stop trading and flatten. Used when the engine can no longer trust what it knows about its positions."""
        first = not (self.risk.st.halted and self.risk.st.flatten_all)
        self.risk.halt(f"SAFETY: {reason}", flatten=True)
        if self.books:
            self.books.halt_all(f"SAFETY: {reason}", flatten=True)
        if not first:
            return
        log.error("SAFETY HALT: %s", reason)
        self.bus.emit("log", now, level="error", msg=f"SAFETY HALT: {reason}. Flattening; check open positions and orders in the Robinhood app.")
        self.set_agent(now, "alarm", f"SAFETY HALT: {reason}. Flattening. Check the Robinhood app.")
        self.bus.emit("risk", now, risk=self.risk.to_dict())
        asyncio.ensure_future(self._cancel_all_quietly())

    async def _cancel_all_quietly(self) -> None:
        try:
            await self.broker.cancel_all()
        except Exception as ex:
            log.warning("cancel_all failed: %s", ex)

    def _watchdog(self, now: float) -> None:
        stale = self.wd["quote_stale_sec"]
        held = [p for p, _ in self.open] + [p for p in (self.books.positions() if self.books else [])
                                            if not getattr(p, "watchdog_exempt", False)]   # book E checks its own multi-day positions
        for pos in held:
            age = now - pos.last_quote_ts
            if age > stale:
                label = getattr(pos, "label", None) or pos.contract.label
                self._trip(f"no fresh quote for {label} in {age:.0f}s, stop can't be checked", now)
                break
        rs = self.wd["reconcile_sec"]
        if self.broker.live and rs and now - self._last_reconcile >= rs and not self._busy.locked() \
                and not any(getattr(p, "_exiting", False) for p, _ in self.open):
            self._last_reconcile = now
            asyncio.ensure_future(self._guard(self._reconcile(now), "position check"))

    async def _reconcile(self, now: float) -> None:
        """Compare contracts the account holds with contracts the engine is managing. Two mismatches in a row halt."""
        held = await self.broker.position_qty()
        if held is None or self._busy.locked() or any(getattr(p, "_exiting", False) for p, _ in self.open):
            return
        mine = sum(p.qty for p, _ in self.open)
        if held == mine:
            self._mismatches = 0
            return
        self._mismatches += 1
        self.bus.emit("log", now, level="warn", msg=f"position check: account holds {held}, engine tracks {mine}")
        if self._mismatches >= 2:
            self._trip(f"position mismatch: account holds {held} contracts, engine tracks {mine}", now)

    # ------------------------------------------------------------------ bars & signals
    async def _on_bar(self, b: Bar) -> None:
        cross = self.sig.on_bar_close(b)
        if b.tf == "1m":
            self.levels.on_1m(b)
        elif b.tf == "5m":
            self.levels.on_5m(b)
        st = self.sig.tf[b.tf]
        d = {"tf": b.tf, "t": b.t, "end": b.end, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v,
             "macd": st.last.macd if st.last else None, "sig": st.last.signal if st.last else None,
             "rsi": st.rsi_val}
        if b.tf == self.tick_tf:
            d["i"] = self.tick.count
        self.bars[b.tf].append(d)
        self.bus.emit("bar", b.end or b.t, bar=d, vwap=self.vwap.value)

        if cross:
            self.bus.emit("cross", cross.ts, tf=cross.tf, dir=cross.direction)
            if cross.direction == "down":
                ripping = (self.sig.hist_rising("5m") and self.price is not None and self.vwap.value is not None
                           and self.price > self.vwap.value and self.sig.tf["5m"].live()[0] is not None
                           and self.sig.tf["5m"].live()[0].bull)
                for pos, plan in list(self.open):
                    intent = plan.on_cross_down(cross.tf, ripping)
                    if intent:
                        await self._run(self.exit(pos, plan, intent, cross.ts), "exit")
                    elif ripping and pos.scales_done and cross.tf == plan.exit_tf:
                        self.bus.emit("log", cross.ts, level="info", msg=f"{cross.tf} crossed back but tape is ripping: runner holds for 5m")

        if b.tf in self.cfg["strategy"]["trigger_timeframes"] and is_rth(b.end or b.t):
            now = b.end or b.t
            if b.tf == "1m" or (not self.inline and _time.time() - self._last_sig_emit > 3):
                self._last_sig_emit = _time.time()
                ok, passed, failed = self.sig.check(now)
                self.bus.emit("signal", now, snap=self.sig.snapshot(), ok=ok, passed=passed, failed=failed,
                              levels=self._levels_payload())
            if self.sig.ready():
                es = self.sig.evaluate(now, self.price or b.c)
                if es:
                    await self._run(self.enter(es), "entry")
        if self.books and b.tf in ("1m", "5m"):
            await self.books.run(self.books.on_bar(b), self.inline)

    def _levels_payload(self) -> list:
        px = self.price or 0
        return [{"px": p, "name": n} for p, n in self.levels.all(px, self.vwap.value) if n != "$5 round" and abs(p - px) < 8]

    # ------------------------------------------------------------------ entries
    async def enter(self, es) -> None:
        if self._busy.locked():
            return
        async with self._busy:
            now = es.ts
            ok, why = self.risk.can_enter(now, len(self.open), self.risk.open_pnl([p for p, _ in self.open]))
            self.sig.consume(es)
            if not ok:
                self._skip(now, es, why)
                return
            if es.setup not in (self.cfg["strategy"].get("enabled_setups") or ["SWING", "SCALP"]):
                self._skip(now, es, f"{es.setup} disabled today by crew tweak")
                return
            self.set_agent(now, "thinking", f"{es.setup} setup: {es.trigger_tf} cross. Picking a strike...")
            spot = self.price
            l2_note = None
            if self.l2 and self.l2.enabled and self.l2.latest is not None:
                l2_ok, l2_why = self.l2.gate(es.side, spot, now)
                st = self.l2.latest
                l2_note = {"imb": round(st.imbalance, 3), "micro_c": round(st.micro_edge_c, 2),
                           "ask_wall": st.ask_wall, "bid_wall": st.bid_wall, "would_block": not l2_ok, "why": l2_why,
                           "age_s": round(now - st.ts, 1), "mode": self.l2.mode}
                es.reasons.append(st.short() + ("" if l2_ok else f" (would block: {l2_why})"))
                if self.l2.mode == "enforce" and not l2_ok:
                    self._skip(now, es, l2_why)
                    return
            scfg = dict(self.cfg["strikes"])
            if "strikes.max_offset" in self.trade_tweaks:
                scfg["max_offset"] = self.trade_tweaks["strikes.max_offset"]
            choice = choose_strike(spot, now, es.side, self.levels, self.vwap.value, scfg)
            expiry = str(session_date(now))
            contract = await self.broker.resolve(Contract(self.symbol, expiry, choice.strike, es.side))
            q = await self.quotes.quote(contract)
            if q is None:
                self._skip(now, es, "no option quote")
                return
            if q.spread_pct > self.cfg["strikes"]["max_spread_pct"] and choice.offset > -1:
                alt = Contract(self.symbol, expiry, choice.strike - (1 if es.side == "call" else -1), es.side)
                alt = await self.broker.resolve(alt)
                q2 = await self.quotes.quote(alt)
                if q2 and q2.spread_pct <= self.cfg["strikes"]["max_spread_pct"]:
                    contract, q = alt, q2
                    choice.reason += f"; spread wide, stepped to {alt.strike:g}"
                else:
                    self._skip(now, es, f"spread {q.spread_pct:.0%} too wide")
                    return
            up_mult = 1.0
            if self.crew:
                up_mult, checks = self.crew.size_up(now, es.setup, spot)
                self.bus.emit("conviction", now, mult=up_mult, checks=checks, setup=es.setup)
            qty, size_why = self.risk.size(q.ask, up_mult)
            crew_fx = None
            if self.crew:                      # the quantity without the crew's cut or size-up, for the scorecard
                from .proposals import get_path
                tweaks = {k: get_path(self.cfg, k) for k in self._day_orig}
                tweaks.update(self.trade_tweaks)
                crew_fx = self.crew.effect("A", qty=qty, qty_1x=self.risk.size(q.ask, 1.0, cut=1.0)[0], up=up_mult,
                                           tweaks=tweaks)
            if qty <= 0:
                self._skip(now, es, size_why)
                return
            bp = await self.broker.buying_power()
            if bp is not None and qty * q.ask * 100 > bp:
                qty = int(bp // (q.ask * 100))
                if qty < 1:
                    self._skip(now, es, f"buying power ${bp:.0f} too low")
                    return
                size_why += f"; trimmed to {qty} by buying power"
            limit = min(q.ask, round(q.mark + self.ocfg["entry_spread_frac"] * (q.ask - q.bid), 2))
            self.set_agent(now, "typing", f"BUY {qty}x {contract.strike:g}{'C' if es.side == 'call' else 'P'} @ {limit:.2f}")
            res = await self._work_order(contract, "buy", qty, limit, now)
            self.bus.emit("order", now, side="buy", contract=contract.label, qty=qty, limit=limit,
                          status=res.status, filled=res.filled_qty, price=res.avg_price, msg=res.message,
                          review=res.review)
            if res.filled_qty <= 0:
                self._skip(now, es, f"entry not filled ({res.message or res.status})")
                return
            pos = Position(contract, es.setup, res.filled_qty, res.avg_price, now,
                           strike_reason=f"{choice.offset:+d}: {choice.reason}", entry_reasons=es.reasons, l2=l2_note)
            pos.mark, pos.bid, pos.ask = q.mark, q.bid, q.ask
            pos.crew = crew_fx
            pos.fees += self.cfg["sizing"]["fee_per_contract"] * res.filled_qty
            pos.fills.append({"ts": now, "side": "buy", "qty": res.filled_qty, "px": res.avg_price, "why": "entry"})
            ecfg = self.cfg["exits"]
            ex_tw = {k: v for k, v in self.trade_tweaks.items() if k.startswith("exits.")}
            if ex_tw:
                import copy as _copy
                from .proposals import set_path
                ecfg = _copy.deepcopy(dict(self.cfg["exits"]))
                for k, v in ex_tw.items():
                    set_path(ecfg, k[len("exits."):], v)
                pos.entry_reasons.append(f"trade tweak: {ex_tw}")
            self.trade_tweaks.clear()
            plan = ExitPlan(ecfg, pos)
            self.open.append((pos, plan))
            self.bus.emit("position", now, pos=pos.to_dict(), targets=plan.targets(), event="open", size_note=size_why)
            self.set_agent(now, "watching", f"In {pos.qty}x {contract.label}. Stop {pos.stop:.2f}, targets {', '.join(f'{t:.2f}' for t in plan.targets())}")

    async def _submit(self, contract, side, qty, limit, now) -> OrderResult:
        """broker.submit, except an order the broker can't confirm halts and flattens. Whatever did fill is
        returned so the engine tracks it (and the flatten sells it)."""
        try:
            return await self.broker.submit(contract, side, qty, limit, now)
        except OrderStateError as ex:
            self._trip(f"{side} {qty}x {contract.label}: {ex}", now)
            return OrderResult("partial" if ex.filled_qty else "unfilled", ex.filled_qty, ex.avg_price or limit,
                               ex.order_id, f"ambiguous: {ex}")

    async def _work_order(self, contract, side, qty, limit, now):
        res = await self._submit(contract, side, qty, limit, now)
        tries = 0
        while res.status in ("unfilled", "partial") and tries < self.ocfg["max_reprices"] \
                and not res.message.startswith("ambiguous"):
            tries += 1
            q = await self.quotes.quote(contract)
            if q is None:
                break
            left = qty - res.filled_qty
            px = q.ask if side == "buy" else q.bid
            nxt = await self._submit(contract, side, left, px, now)
            if nxt.filled_qty:
                tot = res.filled_qty + nxt.filled_qty
                nxt.avg_price = round((res.avg_price * res.filled_qty + nxt.avg_price * nxt.filled_qty) / tot, 3)
                nxt.filled_qty = tot
                nxt.status = "filled" if tot == qty else "partial"
            else:
                nxt.filled_qty, nxt.avg_price = res.filled_qty, res.avg_price
            res = nxt
        return res

    def _skip(self, now, es, why: str) -> None:
        item = {"setup": es.setup, "tf": es.trigger_tf, "why": why}
        self.skips.append({"ts": now, **item})
        if self.crew and why.startswith("blackout:"):
            self.crew.note_block("A", why, now)
        self.bus.emit("skip", now, **item)
        self.set_agent(now, "watching", f"Passed on {es.setup.lower()} signal: {why}")

    # ------------------------------------------------------------------ position management
    async def manage(self, now: float) -> None:
        if self._managing:
            return
        self._managing = True
        try:
            await self._manage(now)
        finally:
            self._managing = False

    async def _manage(self, now: float) -> None:
        flat = self.risk.must_flatten(now)
        for pos, plan in list(self.open):
            q = await self.quotes.quote(pos.contract)
            if q is None or (q.bid <= 0 and q.ask <= 0) or (not self.inline and now - q.ts > self.wd["quote_stale_sec"]):
                continue            # the watchdog halts if this lasts longer than quote_stale_sec
            pos.last_quote_ts = now
            pos.bid = q.bid
            if not flat and self.risk.check_open_loss([p for p, _ in self.open]):
                flat = self.risk.st.halt_reason
                self.bus.emit("log", now, level="error", msg=f"{flat}. Flattening.")
            so = getattr(pos.contract, "sellout_ts", None)
            if not flat and so and now >= so - 300:
                flat = "5 min before Robinhood's sellout time"
            intent = ExitIntent(pos.qty, flat, urgent=True) if flat else plan.on_quote(q.bid, q.ask, now)
            if flat:
                plan.on_quote(q.bid, q.ask, now)
            if intent:
                await self.exit(pos, plan, intent, now)
            elif now - self._last_pos_emit >= 2:
                self._last_pos_emit = now
                self.bus.emit("position", now, pos=pos.to_dict(), targets=plan.targets(), event="update")

    async def exit(self, pos: Position, plan: ExitPlan, intent: ExitIntent, now: float) -> None:
        if pos.qty <= 0 or pos.status != "open" or getattr(pos, "_exiting", False):
            return
        pos._exiting = True
        try:
            await self._exit(pos, plan, intent, now)
        finally:
            pos._exiting = False

    async def _exit(self, pos: Position, plan: ExitPlan, intent: ExitIntent, now: float) -> None:
        q = await self.quotes.quote(pos.contract)
        if q is None:
            return
        n = min(intent.qty, pos.qty)
        limit = q.bid if intent.urgent else round((q.bid + q.mark) / 2, 2)
        self.set_agent(now, "typing", f"SELL {n}x {pos.contract.strike:g}{pos.contract.right[0].upper()}: {intent.reason}")
        res = await self._work_order(pos.contract, "sell", n, limit, now)
        self.bus.emit("order", now, side="sell", contract=pos.contract.label, qty=n, limit=limit,
                      status=res.status, filled=res.filled_qty, price=res.avg_price, msg=res.message, review=res.review)
        if res.filled_qty <= 0:
            self.bus.emit("log", now, level="warn", msg=f"exit not filled: {intent.reason}")
            return
        pnl = (res.avg_price - pos.entry) * 100 * res.filled_qty
        fee = self.cfg["sizing"]["fee_per_contract"] * res.filled_qty
        pos.realized += pnl
        pos.fees += fee
        pos.qty -= res.filled_qty
        pos.fills.append({"ts": now, "side": "sell", "qty": res.filled_qty, "px": res.avg_price, "why": intent.reason})
        self.risk.on_realized(pnl - fee)
        self.bus.emit("fill", now, pos_id=pos.id, qty=res.filled_qty, px=res.avg_price, why=intent.reason, pnl=round(pnl - fee, 2))
        if pos.qty <= 0:
            await self._close(pos, plan, intent.reason, now)
        else:
            self.bus.emit("position", now, pos=pos.to_dict(), targets=plan.targets(), event="scale")
            self.set_agent(now, "celebrating", f"Paid myself: {intent.reason}. Runner on {pos.qty}x, stop {pos.stop:.2f}")
        self.bus.emit("risk", now, risk=self.risk.to_dict())

    async def _close(self, pos: Position, plan: ExitPlan, reason: str, now: float) -> None:
        pos.status, pos.closed_ts, pos.exit_reason = "closed", now, reason
        self.open = [(p, pl) for p, pl in self.open if p.id != pos.id]
        self.closed.append(pos)
        net = pos.realized - pos.fees
        self.risk.on_trade_closed(net, now)
        self.journal.record_trade(str(self.day), self.mode, pos)
        self.bus.emit("trade_closed", now, pos=pos.to_dict(), net=round(net, 2), risk=self.risk.to_dict())
        if net >= 0:
            self.set_agent(now, "celebrating", f"Closed {pos.contract.label} +${net:.0f} ({reason})")
        else:
            self.set_agent(now, "frustrated", f"Closed {pos.contract.label} -${abs(net):.0f} ({reason})")
        if self.crew:
            await self.crew.on_trade_closed(pos, net, now)

    # ------------------------------------------------------------------ crew proposals
    def apply_tweak(self, item: dict, now: float) -> None:
        from .proposals import get_path, set_path, write_override
        import copy as _copy
        if item["scope"] == "trade":
            self.trade_tweaks.update(item["params"])
        elif item["scope"] == "day":
            for k, v in item["params"].items():
                self._day_orig.setdefault(k, _copy.deepcopy(get_path(self.cfg, k)))
                set_path(self.cfg, k, v)
        elif item["scope"] == "standing":
            for k, v in item["params"].items():
                self._day_orig.pop(k, None)
                set_path(self.cfg, k, v)
            write_override(item["params"])
        self.bus.emit("log", now, level="info", msg=f"crew tweak ({item['scope']}): {item['title']} {item['params']}")

    def decide_proposal(self, pid: str, approve: bool) -> dict | None:
        now = self.feed.now()
        book = self.crew.book
        item = next((i for i in book.items if i["id"] == pid), None)
        if item is None or item["status"] != "pending":
            return None
        if not approve:
            item = book.set_status(pid, "rejected by you")
        elif item["scope"] == "standing":
            item = book.set_status(pid, "approved")
            self.apply_tweak(item, now)
        else:
            item = book.set_status(pid, "approved: queued for build + backtest (not trading)")
        self.bus.emit("proposal", now, item=item)
        return item

    # ------------------------------------------------------------------ controls
    async def kill(self) -> None:
        now = self.feed.now()
        self.risk.halt("KILL switch", flatten=True)
        self.set_agent(now, "alarm", "KILL SWITCH. Flattening everything.")
        for pos, plan in list(self.open):
            try:
                await self.exit(pos, plan, ExitIntent(pos.qty, "kill switch", urgent=True), now)
            except Exception as ex:             # keep going: the other positions and the cancels still matter
                log.exception("kill: exit %s failed", pos.contract.label)
                self.bus.emit("log", now, level="error", msg=f"kill: exit {pos.contract.label} failed: {ex}")
        if self.books:
            await self.books.kill(now)
        await self._cancel_all_quietly()
        self.bus.emit("risk", now, risk=self.risk.to_dict())

    async def flatten(self, reason: str = "manual flatten") -> None:
        now = self.feed.now()
        try:
            for pos, plan in list(self.open):
                await self.exit(pos, plan, ExitIntent(pos.qty, reason, urgent=True), now)
        finally:
            if self.books:
                await self.books.flatten(reason, now)

    def pause(self, on: bool) -> None:
        self.risk.set_paused(on)
        now = self.feed.now()
        self.set_agent(now, "paused" if on else "watching", "Paused: no new entries" if on else "Back on it.")
        self.bus.emit("risk", now, risk=self.risk.to_dict())

    # ------------------------------------------------------------------ agent avatar state
    def set_agent(self, now: float, activity: str, text: str = "") -> None:
        self.agent = {"activity": activity, "text": text, "ts": now}
        self.bus.emit("agent", now, activity=activity, text=text)

    def _update_idle(self, now: float) -> None:
        a = self.agent["activity"]
        age = now - self.agent["ts"]
        t = ct_time(now)
        if a in ("consulting",):
            return
        if not is_rth(now):
            want = "coffee" if t < time(8, 30) else "offline"
            if a not in (want, "briefing") and age > 5:
                self.set_agent(now, want, "Pre-market prep." if want == "coffee" else "Market closed.")
            return
        if self.risk.st.halted and a != "alarm" and age > 5:
            self.set_agent(now, "halted", f"Done for the day: {self.risk.st.halt_reason}")
        elif a in ("celebrating", "frustrated", "typing", "thinking", "arriving", "coffee", "briefing") and age > 20:
            self.set_agent(now, "watching", "Watching the tape." if not self.open else "Managing the position.")

    # ------------------------------------------------------------------ snapshot for new UI clients
    def snapshot(self) -> dict:
        now = self.feed.now()
        return {
            "type": "snapshot", "ts": now, "mode": self.mode, "feed": self.feed.name, "broker": self.broker.name,
            "symbol": self.symbol, "day": str(self.day), "price": self.price, "vwap": self.vwap.value,
            "bars": {tf: list(q) for tf, q in self.bars.items()},
            "signal": {"snap": self.sig.snapshot(), **dict(zip(("ok", "passed", "failed"), self.sig.check(now)))},
            "levels": self._levels_payload() if self.price else [],
            "positions": [{"pos": p.to_dict(), "targets": pl.targets()} for p, pl in self.open],
            "closed": [p.to_dict() for p in self.closed],
            "skips": list(self.skips),
            "risk": self.risk.to_dict(),
            "agent": self.agent,
            "crew": self.crew.state() if self.crew else {},
            "l2": ({"book": self.l2.latest.to_dict() if self.l2.latest else None, "mode": self.l2.mode}
                   if self.l2 else None),
            "config": {k: self.cfg[k] for k in ("strategy", "strikes", "sizing", "exits", "risk")},
            "books": self.books.snapshot() if self.books else None,
        }
