"""Runs paper books B, C, D next to the engine's book A.

The engine calls on_bar / on_second / kill / flatten / halt_all; book A's own path is untouched. Every fill here is
paper (PaperBroker.submit_combo); in shadow/live the first price of each order also goes to review_option_order.
One book's error halts and flattens that book only. A stale combo quote is caught by the engine's watchdog, which
halts everything, as for book A.
"""
from __future__ import annotations

import logging
from datetime import time

from ..brokers.paper import PaperBroker
from ..clock import ct_time, is_rth, session_date
from ..config import hhmm
from ..exits import Contract
from .account import AccountRisk
from .base import ExitIntent, MarketContext, OrderIntent, Skip
from .book import Book
from .combo import ComboPosition, ComboQuote, leg_problem, paper_fair
from .fills import ComboExecutor
from .legs import fetch_quotes

log = logging.getLogger("agentdesk.books")
FILLS = {"model": "mid_offset", "cents_per_leg": 1, "reprice_step_c": 1, "max_reprices": 4,
         "max_quote_age_s": 5, "max_leg_spread_pct": 0.25, "tick_exempt": 0.02}
HIST_SEED = 424242


def strategy_class(key: str):
    from .iron_condor import IronCondor
    from .iron_fly import IronFly
    from .orb_bull_put import OrbBullPut
    return {"B_iron_fly": IronFly, "C_orb_bull_put": OrbBullPut, "D_iron_condor": IronCondor}.get(key)


def build_books(cfg) -> list[Book]:
    out = []
    for key, bc in (cfg.get("books") or {}).items():
        cls = strategy_class(key) if isinstance(bc, dict) else None
        if cls is None or not bc.get("enabled"):
            continue
        if bc.get("paper_only") is not True:
            raise SystemExit(f"books.{key}: only a paper path exists for this book; set paper_only: true")
        out.append(Book(key, bc, cls(bc)))
    return out


class BookHost:
    def __init__(self, engine, cfg, vix=None, reviewer=None, books: list[Book] | None = None):
        self.e, self.cfg = engine, cfg
        bc = cfg.get("books") or {}
        self.account = AccountRisk(bc.get("account"))
        self.f = {**FILLS, **(bc.get("fills") or {})}
        self.broker = PaperBroker(engine.quotes)
        self.broker.combo_model, self.broker.combo_cents = self.f["model"], self.f["cents_per_leg"] / 100
        self.exec = ComboExecutor(self.broker, self.f, reviewer)
        self.vix = vix
        self.books = build_books(cfg) if books is None else books
        self.fee = self.account.c["fee_per_leg"]
        self.max_errors = (cfg["risk"].get("watchdog") or {}).get("max_consecutive_errors", 3)
        self.day = None
        self.vix_prev, self._vix_day, self._vix_try = None, None, -1e18
        self._last_manage, self._last_emit, self._busy = 0.0, 0.0, False

    @property
    def enabled(self) -> bool:
        return bool(self.books)

    def positions(self) -> list:
        return [p for b in self.books for p in b.open]

    def open_risk(self) -> float:
        a = sum(p.entry * 100 * p.qty for p, _ in self.e.open)
        return sum(p.max_loss for p in self.positions()) + a

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        try:
            hist = await self._history()
        except Exception as ex:
            hist = []
            self._log(self.e.feed.now(), "warn", f"books: history load failed ({ex}); indicators warm up live")
        for b in self.books:
            b.strategy.warmup(hist)

    async def _history(self) -> list:
        f = self.e.feed
        days = (self.cfg.get("books") or {}).get("history_days", 20)
        if f.is_sim:           # a separate generator: book A's sim day must not change
            from ..feeds.sim import SimFeed
            return await SimFeed(day=f.day, seed=HIST_SEED, start_px=f.px).history_1m(days)
        return await f.history_1m(days)

    def _new_day(self, d) -> None:
        self.day = d
        for b in self.books:
            b.reset_day()
            b.strategy.new_day(d)

    # ------------------------------------------------------------ engine hooks
    async def on_bar(self, bar) -> None:
        if bar.tf not in ("1m", "5m"):
            return
        now = bar.end or bar.t
        if session_date(now) != self.day:
            self._new_day(session_date(now))
        for book in self.books:
            await self._safe(book, self._book_bar(book, bar, now), now)

    async def on_second(self, now: float) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            if session_date(now) != self.day:
                self._new_day(session_date(now))
            await self._ensure_vix(now)
            if is_rth(now):
                for book in self.books:
                    await self._safe(book, self._book_clock(book, now), now)
            poll = self.cfg["robinhood"]["quote_poll_ms"] / 1000
            if self.positions() and now - self._last_manage >= poll:
                self._last_manage = now
                for book in self.books:
                    if book.open:
                        await self._safe(book, self._manage(book, now), now)
        finally:
            self._busy = False

    def halt_all(self, reason: str, flatten: bool = False) -> None:
        self.account.halt(reason, flatten)

    async def kill(self, now: float) -> None:
        self.account.halt("KILL switch", flatten=True)
        await self.flatten("KILL switch", now)

    async def flatten(self, reason: str, now: float) -> None:
        for book in self.books:
            for pos in list(book.open):
                try:
                    await self._exit(book, pos, ExitIntent(reason, urgent=True), now)
                except Exception as ex:          # keep going: the other positions still matter
                    log.exception("flatten %s failed", pos.label)
                    self._log(now, "error", f"flatten {pos.label} failed: {ex}")

    # ------------------------------------------------------------ per book
    async def _safe(self, book: Book, coro, now: float) -> None:
        try:
            await coro
            if book.error_ts != now:        # a clean call only clears errors when nothing else failed this tick
                book.errors = 0
        except Exception as ex:
            book.errors += 1
            book.error_ts = now
            log.exception("book %s failed (%d in a row)", book.letter, book.errors)
            self._log(now, "error", f"book {book.letter}: {ex} ({book.errors} in a row)")
            if book.errors >= self.max_errors and not book.halted:
                book.halt(f"{book.errors} errors in a row: {ex}")
                self._log(now, "error", f"book {book.letter} halted and flattening: {book.halt_reason}")
                for pos in list(book.open):
                    try:
                        await self._exit(book, pos, ExitIntent(f"book halted: {book.halt_reason}", urgent=True), now)
                    except Exception:
                        log.exception("book %s flatten failed", book.letter)

    async def _book_bar(self, book: Book, bar, now: float) -> None:
        await self._act(book, book.strategy.on_bar(bar.tf, bar, self._ctx(now, book)), now)

    async def _book_clock(self, book: Book, now: float) -> None:
        await self._act(book, book.strategy.on_clock(now, self._ctx(now, book)), now)

    async def _act(self, book: Book, out, now: float) -> None:
        if isinstance(out, OrderIntent):
            await self._enter(book, out, now)
        elif isinstance(out, Skip):
            self._skip(book, now, out.reason)
        elif isinstance(out, ExitIntent):
            for pos in list(book.open):
                await self._exit(book, pos, out, now)

    def _ctx(self, now: float, book: Book) -> MarketContext:
        d = session_date(now)
        st = self.e.risk.st
        before = self.cfg["risk"]["event_blackout"]["before_min"] * 60
        events = [(b.start + before, b.name) for b in st.blackouts if session_date(b.start + before) == d]
        return MarketContext(now=now, day=d, spot=self.e.price, vwap=self.e.vwap.value, events=events,
                             vix_prev=self.vix_prev if self._vix_day == d else None, vix1d_flag=self._vix1d_flag(d),
                             size_mult=min(1.0, st.size_mult), open=list(book.open))

    def _vix1d_flag(self, d) -> bool | None:
        crew = self.e.crew
        b = (crew.briefs.get("vol") if crew else None) or {}
        if "vix1d_flag" not in b or not b.get("ts") or session_date(b["ts"]) != d:
            return None             # crew briefs carry the time they were given; only today's answer counts
        return bool(b["vix1d_flag"])

    async def _ensure_vix(self, now: float) -> None:
        d = session_date(now)
        if self.vix is None or self._vix_day == d or now - self._vix_try < 60:
            return
        self._vix_try = now
        try:
            v = await self.vix.prior_close(d)
        except Exception as ex:
            v = None
            self._log(now, "warn", f"books: prior VIX close unavailable ({ex})")
        if v:
            self.vix_prev, self._vix_day = float(v), d
            self._log(now, "info", f"books: prior VIX close {self.vix_prev:.2f}")

    # ------------------------------------------------------------ entries
    def _gate(self, book: Book, now: float) -> tuple[bool, str]:
        st = self.e.risk.st
        if self.account.halted:
            return False, f"halted: {self.account.halt_reason}"
        if st.halted and st.flatten_all:
            return False, f"halted: {st.halt_reason}"
        if st.paused:
            return False, "paused"
        ok, why = book.can_enter()
        if not ok:
            return ok, why
        if self.e.risk.early_close(now) and ct_time(now) >= time(11, 20):
            return False, "early close: no entries after 11:20 CT"
        for b in st.blackouts:
            if b.start <= now < b.end:
                return False, f"blackout: {b.name}"
        return True, "ok"

    async def _enter(self, book: Book, it: OrderIntent, now: float) -> None:
        if book.entering:
            return
        ok, why = self._gate(book, now)
        if not ok:
            return self._skip(book, now, why)
        book.entering = True
        try:
            exp = str(session_date(now))
            cs = [await self.e.broker.resolve(Contract(self.e.symbol, exp, float(l.strike), l.right)) for l in it.legs]
            cq = await self._quote(it.legs, cs, now, opening=True)
            if cq.problem:
                book.strategy.entry_failed(now)
                return self._skip(book, now, f"quote: {cq.problem}")
            fair = paper_fair(cq, it.credit, True, self.f["model"], self.f["cents_per_leg"] / 100)
            if it.credit and fair <= 0:
                return self._skip(book, now, "no credit at the expected fill")
            per_lot = round(((it.width - fair) if it.credit else fair) * 100, 2)
            lots, size_why = self.account.size(per_lot, it.lots, it.budget, min(1.0, self.e.risk.st.size_mult))
            if lots < 1:
                return self._skip(book, now, size_why)
            ok, why = self.account.can_open(per_lot * lots, self.open_risk(), self.e.risk.st.day_pnl)
            if not ok:
                return self._skip(book, now, why)
            res = await self.exec.work(it.legs, cs, lots, it.credit, True, False, now)
            self._order_event(book, now, "open", lots, res, it.reason)
            if res.filled_qty <= 0:
                book.strategy.entry_failed(now)
                return self._skip(book, now, f"entry not filled ({res.message or res.status})")
            pos = ComboPosition(book.letter, book.strategy.name, list(it.legs), cs, res.filled_qty, res.avg_price,
                                it.credit, it.width, now, strike_reason=it.reason,
                                entry_reasons=[size_why] + list(it.meta.get("notes", [])), meta=dict(it.meta))
            pos.fees = self.fee * sum(l.ratio for l in it.legs) * res.filled_qty
            pos.fills.append(self._fill(now, "open", res, "entry"))
            c = book.c
            if "stop_debit_x_credit" in c:
                pos.stop = round(pos.entry * c["stop_debit_x_credit"], 2)
                pos.target = round(pos.entry * (1 - c["take_profit_pct"]), 2)
            pos.meta["plan"] = book.strategy.plan(pos)
            if res.status == "partial":
                book.blocked = f"partial fill {res.filled_qty}/{lots}: reconcile before new orders"
            book.on_open(pos)
            self.e.bus.emit("book_position", now, pos=pos.to_dict(), event="open", size_note=size_why)
            self._emit_books(now)
        finally:
            book.entering = False

    async def _quote(self, legs, contracts, now: float, opening: bool) -> ComboQuote:
        qs = await fetch_quotes(self.e.quotes, contracts)
        for c, q in zip(contracts, qs):
            p = leg_problem(q, now, self.f["max_quote_age_s"], self.f["max_leg_spread_pct"], self.f["tick_exempt"], opening)
            if p:
                return ComboQuote(legs, qs, f"{c.label}: {p}")
        return ComboQuote(legs, qs)

    # ------------------------------------------------------------ management
    async def _manage(self, book: Book, now: float) -> None:
        for pos in list(book.open):
            cq = await self._quote(pos.legs, pos.contracts, now, opening=False)
            if cq.problem:
                continue            # the engine watchdog halts everything if this lasts quote_stale_sec
            pos.last_quote_ts = now
            pos.mark = round(cq.mid(pos.credit), 3)
            pos.peak = min(pos.peak, pos.mark) if pos.credit else max(pos.peak, pos.mark)
            forced = self._forced(book, pos, now)
            it = ExitIntent(forced, urgent=True) if forced else book.strategy.on_quote(pos, cq, now, self._ctx(now, book))
            if it:
                await self._exit(book, pos, it, now)
            elif now - self._last_emit >= 2:
                self._last_emit = now
                self.e.bus.emit("book_position", now, pos=pos.to_dict(), event="update")

    def _forced(self, book: Book, pos, now: float) -> str | None:
        st = self.e.risk.st
        if self.account.halted and self.account.flatten:
            return self.account.halt_reason
        if st.halted and st.flatten_all:
            return st.halt_reason or "kill switch"
        if book.halted:
            return f"book halted: {book.halt_reason}"
        t = ct_time(now)
        if self.e.risk.early_close(now) and t >= time(11, 40):
            return "early close: flat 11:40 CT"
        fl = self.cfg["exits"]["flatten_at"]
        if t >= hhmm(fl):
            return f"flatten {fl} CT"
        so = min((c.sellout_ts for c in pos.contracts if getattr(c, "sellout_ts", None)), default=None)
        if so and now >= so - 300:
            return "5 min before Robinhood's sellout time"
        return None

    async def _exit(self, book: Book, pos, it: ExitIntent, now: float) -> None:
        if pos.status != "open" or pos.exiting or pos.qty <= 0:
            return
        pos.exiting = True
        try:
            res = await self.exec.work(pos.legs, pos.contracts, pos.qty, pos.credit, False, it.urgent, now)
            self._order_event(book, now, "close", pos.qty, res, it.reason)
            if res.filled_qty <= 0:
                self._log(now, "warn", f"book {book.letter} exit not filled ({it.reason}): {res.message or res.status}")
                return
            n = res.filled_qty
            pos.realized += pos.pnl_per_share(res.avg_price) * 100 * n
            pos.fees += self.fee * sum(l.ratio for l in pos.legs) * n
            pos.qty -= n
            pos.fills.append(self._fill(now, "close", res, it.reason))
            if pos.qty <= 0:
                self._close(book, pos, it.reason, now)
            else:
                book.blocked = f"partial exit, {pos.qty} left: reconcile before new orders"
        finally:
            pos.exiting = False

    def _close(self, book: Book, pos, reason: str, now: float) -> None:
        pos.status, pos.closed_ts, pos.exit_reason = "closed", now, reason
        net = pos.realized - pos.fees
        book.on_close(pos, net)
        self.account.on_closed(net)
        self.e.journal.record_trade(str(self.day), self.e.mode, pos, book=book.letter)
        book.strategy.on_closed(pos, now)
        self.e.bus.emit("book_closed", now, pos=pos.to_dict(), net=round(net, 2))
        self._emit_books(now)

    # ------------------------------------------------------------ events / snapshot
    def _fill(self, now, side, res, why) -> dict:
        r = res.raw or {}
        return {"ts": now, "side": side, "qty": res.filled_qty, "px": res.avg_price, "mid": r.get("mid"),
                "natural": r.get("natural"), "limit": r.get("limit"), "ref_id": r.get("ref_id"), "why": why}

    def _order_event(self, book, now, action, qty, res, why) -> None:
        self.e.bus.emit("book_order", now, book=book.letter, action=action, qty=qty, status=res.status,
                        filled=res.filled_qty, price=res.avg_price, limit=(res.raw or {}).get("limit"),
                        mid=(res.raw or {}).get("mid"), natural=(res.raw or {}).get("natural"), why=why,
                        review=res.review)

    def _skip(self, book: Book, now: float, why: str) -> None:
        if book.skips and book.skips[-1]["why"] == why:
            return                 # same reason as last time: already logged
        book.skips.append({"ts": now, "book": book.letter, "why": why})
        self.e.bus.emit("book_skip", now, book=book.letter, why=why)

    def _log(self, now: float, level: str, msg: str) -> None:
        self.e.bus.emit("log", now, level=level, msg=msg)

    def _a_summary(self) -> dict:
        st = self.e.risk.st
        return {"book": "A", "key": "A_macd_calls", "name": "MACD CALLS", "day_pnl": round(st.day_pnl, 2),
                "open_pnl": round(sum(p.unrealized for p, _ in self.e.open), 2), "trades": st.trades,
                "max_trades": self.cfg["risk"]["max_trades_per_day"], "wins": st.wins, "losses": st.losses,
                "halted": st.halted, "halt_reason": st.halt_reason, "blocked": None, "open": [], "closed": [],
                "last_skip": None}

    def summary(self) -> list[dict]:
        return [self._a_summary()] + [{k: v for k, v in b.to_dict().items() if k not in ("open", "closed")}
                                      for b in self.books]

    def _emit_books(self, now: float) -> None:
        self.e.bus.emit("books", now, books=self.summary(),
                        account=self.account.to_dict(self.open_risk(), self.e.risk.st.day_pnl))

    def snapshot(self) -> dict:
        return {"books": [self._a_summary()] + [b.to_dict() for b in self.books],
                "account": self.account.to_dict(self.open_risk(), self.e.risk.st.day_pnl)}
