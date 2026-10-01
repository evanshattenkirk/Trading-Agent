"""Runs book E (pre-earnings IV run-up, HANDOFF 7E) next to BookHost (B/C/D/G) and FHost (F), through HostGroup.

Paper only: E fills through its own PaperBroker at mid -1c per leg (never worse than natural). In shadow/live the
first price of each order also goes to review_option_order. E never touches engine.broker.
E holds for days (spec E-Q1): open positions live in e_positions, are restored at startup, and are NOT sold at
shutdown. The kill switch, a safety halt, an E halt and the dashboard Flatten button still sell them. E positions
are left out of the engine's 10-second quote watchdog (watchdog_exempt); E quotes them every poll_sec itself.
Day, CT: entries at entry_ct (11:20 on half-days) from the Earnings desk's screen; take profit / stop on the combo
mid during regular hours; exits per earnings_iv.exit_reason. Nothing trades outside 08:30-15:00 CT.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, time

from ..brokers.base import RateLimited
from ..brokers.paper import PaperBroker
from ..clock import ct_time, is_rth, session_date
from ..earnings import is_trading_day, screen
from ..iv import atm_strike
from . import earnings_iv as R
from .account import AccountRisk
from .base import ExitIntent
from .book import Book, todays_trades
from .combo import ComboQuote, ComboPosition, leg_problem, paper_fair
from .e_journal import EJournal
from .fills import ComboExecutor
from .host import FILLS

log = logging.getLogger("agentdesk.book_e")
KEY = "E_earnings_iv"
ENTRY_RETRY_SEC = 60        # a failed calendar read is retried inside the entry window
MAX_EXIT_TRIES = 3          # forced-exit tries without usable quotes before closing at the last mark
LIVE_FIELDS = ("id", "book", "qty", "mark", "peak", "unrealized", "realized", "fees", "total_pnl", "pnl_pct",
               "max_loss", "status")


class EarningsIV:
    """The strategy object the Book holds (name and plan text); the rules live in earnings_iv."""
    name = "EARNINGS IV"

    def __init__(self, cfg: dict):
        self.c = cfg

    def new_day(self, day) -> None: ...

    def plan(self, pos) -> str:
        m = pos.meta
        short = f", short leg out by {self.c['expiry_day_exit_ct']} CT on {m['short_expiry']}" if m.get("short_expiry") else ""
        return (f"take profit at {pos.target:.2f}, stop at {pos.stop:.2f}, exit {m.get('exit_day')} "
                f"{self.c['exit_ct']} CT{short}; {m.get('symbol')} reports {m.get('earnings_date')} "
                f"({m.get('timing') or 'time n/a'})")


class EHost:
    def __init__(self, engine, cfg, chains, account: AccountRisk | None = None, ej: EJournal | None = None, vix=None,
                 reviewer=None, calendar_fn=None):
        from ..desks import holidays
        self.e, self.cfg, self.chains, self.vix = engine, cfg, chains, vix
        self.c = cfg["books"][KEY]
        self.book = Book(KEY, self.c, EarningsIV(self.c))
        self.account = account or AccountRisk((cfg.get("books") or {}).get("account"))
        self.other_risk = lambda: sum(p.entry * 100 * p.qty for p, _ in self.e.open)     # A (+ others via HostGroup)
        self.books_changed = lambda now: None      # HostGroup: refresh the dashboard's book strip
        self.f = {**FILLS, **((cfg.get("books") or {}).get("fills") or {})}
        self.broker = PaperBroker(chains)
        self.broker.combo_model, self.broker.combo_cents = self.f["model"], self.f["cents_per_leg"] / 100
        self.exec = ComboExecutor(self.broker, self.f, reviewer)
        self.fee = self.account.c["fee_per_leg"]
        self.ej = ej or EJournal(engine.journal.db)
        self.calendar_fn = calendar_fn or self._crew_calendar
        self.holidays = holidays(cfg)
        self.universe = [s.upper() for s in ((cfg.get("crew") or {}).get("earnings") or {}).get("universe") or []]
        self.max_errors = (cfg["risk"].get("watchdog") or {}).get("max_consecutive_errors", 3)
        self.day = self.entered = self.refreshed = None
        self._busy, self._tasks, self._flagged = False, set(), set()
        self._last_poll, self._last_emit = -1e18, -1e18
        self._last_rate_log = -1e18
        self.vix_prev, self._vix_day, self._vix_try = None, None, -1e18
        self._entry_try, self._exit_tries, self._refresh_try = -1e18, {}, -1e18

    async def _crew_calendar(self, today):
        from ..desks import load_calendar
        if self.e.crew is None:
            return []
        cal, src = await load_calendar(self.e.crew, today)
        return None if src.startswith("Robinhood calendar unavailable") else cal     # retry, don't trade blind

    # ------------------------------------------------------------ state
    @property
    def enabled(self) -> bool:
        return True

    def positions(self) -> list:
        return list(self.book.open)

    def open_risk(self) -> float:
        return sum(p.max_loss for p in self.book.open)

    def _all_risk(self) -> float:
        return self.other_risk() + self.open_risk()

    @staticmethod
    def _tag(pos: ComboPosition) -> ComboPosition:
        pos.watchdog_exempt = pos.overnight = True
        return pos

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        now = self.e.feed.now()
        self.day = session_date(now)
        rows, bad = self.ej.open_positions()
        for p in rows:
            p.last_quote_ts = now
            self.book.open.append(self._tag(p))
            self._log(now, "warn", f"book E: restored {p.label}, {p.qty} @ {p.entry:.2f}; exit {p.meta.get('exit_day')}")
        if bad:
            self.book.halt(f"unreadable e_positions rows: {', '.join(bad)}")
            self._log(now, "error", f"book E halted: {self.book.halt_reason}")
        today = str(self.day)                                          # a restart mid-day keeps today's counts
        self.account.on_closed(self.book.restore_day(todays_trades(self.e.journal, today, "E"), today))

    async def on_bar(self, bar) -> None:
        return None

    async def on_second(self, now: float) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            d = session_date(now)
            if d != self.day:
                self.day = d
                self.book.reset_day()
            if is_trading_day(d, self.holidays):
                await self._tick(now)
        finally:
            self._busy = False

    async def run(self, coro, inline: bool) -> None:
        """Run a hook inline (sim, tests) or as a background task, under E's own error count."""
        if inline:
            await self._safe(coro, self.e.feed.now())
        else:
            t = asyncio.create_task(self._safe(coro, self.e.feed.now()))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _safe(self, coro, now: float) -> None:
        b, kind = self.book, getattr(coro, "__qualname__", "")
        try:
            await coro
            b.succeeded(kind)
        except RateLimited as ex:           # Robinhood is pacing the account: skip this round, never halt over it
            if now - self._last_rate_log >= 60:
                self._last_rate_log = now
                self._log(now, "warn", f"book E: Robinhood rate limit, calls paused and retried ({str(ex)[:120]})")
        except Exception as ex:
            b.failed(kind)
            log.exception("book E failed (%d in a row)", b.errors)
            self._log(now, "error", f"book E: {ex} ({b.errors} in a row)")
            if b.errors >= self.max_errors and not b.halted:
                b.halt(f"{b.errors} errors in a row: {ex}")
                self._log(now, "error", f"book E halted and flattening: {b.halt_reason}")
                await self.flatten(f"book halted: {b.halt_reason}", now)

    async def _tick(self, now: float) -> None:
        await self._ensure_vix(now)
        if self.book.open and self.refreshed != self.day and is_rth(now) and now - self._refresh_try >= ENTRY_RETRY_SEC:
            self._refresh_try = now                 # a failed calendar read is retried, never skipped for the day
            if await self._refresh_events(now):
                self.refreshed = self.day
        if self.book.open:
            await self._manage(now)
        half = self.e.risk.early_close(now)
        t = ct_time(now)
        if self.entered != self.day and is_rth(now) and R.entry_time(self.c, half) <= t < (time(12) if half else time(15)) \
                and now - self._entry_try >= ENTRY_RETRY_SEC:
            self._entry_try = now
            if await self._entries(now):
                self.entered = self.day

    async def _ensure_vix(self, now: float) -> None:
        d = session_date(now)
        if self.vix is None or self._vix_day == d or now - self._vix_try < 60:
            return
        self._vix_try = now
        try:
            v = await self.vix.prior_close(d)
        except Exception as ex:
            v = None
            self._log(now, "warn", f"book E: prior VIX close unavailable ({ex})")
        if v:
            self.vix_prev, self._vix_day = float(v), d

    # ------------------------------------------------------------ entries
    def _gate(self) -> str | None:
        st = self.e.risk.st
        if self.account.halted:
            return f"halted: {self.account.halt_reason}"
        if self.e.risk.account_flatten():
            return f"halted: {st.halt_reason}"
        if st.paused:
            return "paused"
        if self.book.halted:
            return f"book halted: {self.book.halt_reason}"
        return self.book.blocked

    async def _calendar(self, now: float) -> list | None:
        try:
            cal = await self.calendar_fn(self.day)
        except Exception as ex:
            cal, why = None, str(ex)
        else:
            why = "no Robinhood calendar"
        if cal is None:
            self._log(now, "warn", f"book E: earnings calendar unavailable ({why}); retrying in {ENTRY_RETRY_SEC}s")
        return cal

    async def _entries(self, now: float) -> bool:
        """Evaluate today's candidates; False when the calendar couldn't be read (the entry window retries)."""
        cal = await self._calendar(now)
        if cal is None:
            return False
        allowed = set(self.c.get("structures") or [R.E1, R.E2])
        for row in screen(cal, self.day, self.universe, self.holidays):
            st = R.structure_for(row["flag"])
            if st is None or st not in allowed or self.ej.traded(row["symbol"], str(row["date"]), st):
                continue
            try:
                await self._consider(row, st, now)
            except Exception as ex:                 # one name's bad data never costs the others their entry
                log.exception("book E: %s failed", row["symbol"])
                self.ej.decision(day=str(self.day), ts=now, symbol=row["symbol"], structure=st, T=row["T"],
                                 earnings_date=str(row["date"]), timing=row["timing"], outcome="error",
                                 reason=str(ex)[:300], vix=self.vix_prev)
                self._log(now, "error", f"book E: {row['symbol']} {R.SETUP[st]}: {ex}")
        return True

    async def _consider(self, row: dict, st: str, now: float) -> None:
        sym, ev, timing, T = row["symbol"], row["date"], row["timing"], row["T"]
        vix = self.vix_prev if self._vix_day == self.day else None            # never yesterday's close
        rec = {"day": str(self.day), "ts": now, "symbol": sym, "structure": st, "T": T, "earnings_date": str(ev),
               "timing": timing, "vix": vix, "debit": None, "iv": None, "iv_pct": None}

        def skip(reason: str) -> None:
            self.ej.decision(**rec, outcome="skipped", reason=reason, lots=None)
            self._skip(now, f"{sym} {R.SETUP[st]}: {reason}")

        why = self._gate() or R.limit_problem(sym, [p.meta.get("symbol") for p in self.book.open], self.c["sectors"],
                                              int(self.c["max_open"])) or R.vix_problem(vix, self.c)
        if why:
            return skip(why)
        exp, why = R.pick_expiries(st, await self.chains.expirations(sym), self.day, ev, timing, self.c)
        if exp is None:
            return skip(why)
        spot = (await self.chains.spots([sym])).get(sym)
        exps = [e for e in (exp["short"], exp["long"]) if e]
        k = atm_strike([await self.chains.strikes(sym, e) for e in exps], spot)
        if k is None:
            return skip("no ATM strike listed in every leg's expiry" if spot else "no spot price")
        legs = R.legs_for(st, k, self.day, exp)
        cs = [self.chains.contract(sym, exp["short"] if l.side == "sell" else exp["long"], k, l.right) for l in legs]
        extra = [] if st == R.E1 else [self.chains.contract(sym, exp["long"], k, "put")]    # for the earnings IV
        qs = await self.chains.quotes(cs + extra)
        for c, q in zip(cs, qs):
            p = leg_problem(q, now, self.f["max_quote_age_s"], float(self.c["max_leg_spread_pct"]), self.f["tick_exempt"], True)
            if p:
                return skip(f"quote: {c.label}: {p}")
        earn = [q.iv for q in (qs if st == R.E1 else qs[1:]) if q is not None and getattr(q, "iv", None)]
        iv = sum(earn) / len(earn) if earn else None
        ok, iv_note, pct = R.iv_check(iv, self.e.journal.iv_history(sym, "earn", T, str(ev)), self.c)
        rec.update(iv=iv, iv_pct=pct)
        if not ok:
            return skip(iv_note)
        cq = ComboQuote(legs, qs[:len(legs)])
        fair = paper_fair(cq, False, True, self.f["model"], self.f["cents_per_leg"] / 100)
        rec["debit"] = fair
        lots, size_why = R.lots_for(fair, R.max_debit(st, self.c), self.e.risk.book_mult("E"))
        if lots < 1:
            return skip(size_why)
        ok, why = self.account.can_open(round(fair * 100 * lots, 2), self._all_risk(), self.e.risk.st.day_pnl)
        if not ok:
            return skip(why)
        res = await self.exec.work(legs, cs, lots, False, True, False, now)
        self._order_event(now, sym, "open", lots, res, R.SETUP[st])
        if res.filled_qty <= 0:
            return skip(f"entry not filled ({res.message or res.status})")
        meta = {"symbol": sym, "structure": st, "earnings_date": str(ev), "timing": timing, "T": T,
                "exit_day": str(R.exit_day(ev, timing, self.holidays)),
                "short_expiry": str(exp["short"]) if exp["short"] else ""}
        vix_note = f"VIX {vix:.1f}"
        pos = ComboPosition("E", R.SETUP[st], legs, cs, res.filled_qty, res.avg_price, False, 0.0, now,
                            strike_reason=f"{sym} {k:g} ATM (spot {spot:.2f}); reports {ev} {timing or 'time n/a'}, T-{T}",
                            entry_reasons=[size_why, iv_note, vix_note], meta=meta, id=f"E-{uuid.uuid4().hex[:8]}")
        if self.e.crew is not None:
            pos.meta["crew"] = self.e.crew.effect("E", qty=lots, qty_1x=R.lots_for(fair, R.max_debit(st, self.c), 1.0)[0])
        pos.fees = self.fee * len(legs) * res.filled_qty
        pos.fills.append(self._fill(now, "open", res, "entry"))
        pos.target = round(pos.entry * (1 + self.c["take_profit"][st]), 2)
        pos.stop = round(pos.entry * (1 - self.c["stop_pct"]), 2)
        pos.meta["plan"] = self.book.strategy.plan(pos)
        if res.status == "partial":
            self.book.blocked = f"partial fill {res.filled_qty}/{lots}: reconcile before new orders"
        self.book.on_open(self._tag(pos))
        self.ej.save(pos, now)
        self.ej.decision(**rec, outcome="opened", reason=res.status, lots=res.filled_qty)
        self.e.bus.emit("book_position", now, pos=pos.to_dict(), event="open", size_note=size_why)
        self.books_changed(now)

    async def _refresh_events(self, now: float) -> bool:
        """Move an open position's exit when the report date moved (spec E-Q7): the nearest calendar row wins.
        False when the calendar couldn't be read (the caller retries)."""
        cal = await self._calendar(now)
        if cal is None:
            return False
        for p in self.book.open:
            m = p.meta
            rows = [r for r in cal if r["symbol"] == m.get("symbol")]
            if not rows:
                continue
            held = date.fromisoformat(m["earnings_date"])
            r = min(rows, key=lambda r: abs((r["date"] - held).days))
            if r["date"] != held or r["timing"] != m.get("timing"):
                old = f"{m['earnings_date']} {m.get('timing') or 'n/a'}"
                m.update(earnings_date=str(r["date"]), timing=r["timing"],
                         exit_day=str(R.exit_day(r["date"], r["timing"], self.holidays)))
                m["plan"] = self.book.strategy.plan(p)
                self.ej.save(p, now)
                self._log(now, "warn", f"book E: {m['symbol']} report moved {old} -> {r['date']} {r['timing'] or 'n/a'}; "
                                       f"exit now {m['exit_day']}")
        return True

    # ------------------------------------------------------------ management
    def _forced(self, p: ComboPosition, now: float, half: bool) -> str | None:
        st = self.e.risk.st
        if self.account.halted and self.account.flatten:
            return self.account.halt_reason
        if self.e.risk.account_flatten():
            return st.halt_reason or "kill switch"
        if self.book.halted:
            return f"book halted: {self.book.halt_reason}"
        why, err = R.exit_reason(p.meta, now, self.c, half, self.holidays)
        if why and err and p.id not in self._flagged:
            self._flagged.add(p.id)
            self._log(now, "error", f"book E: {p.label}: {why}")
        return why

    async def _manage(self, now: float) -> None:
        half = self.e.risk.early_close(now)
        due = {p.id: self._forced(p, now, half) for p in self.book.open}
        if not is_rth(now):
            return
        new_due = any(r and pid not in self._exit_tries for pid, r in due.items())
        if not new_due and now - self._last_poll < float(self.c["poll_sec"]):
            return                  # retries of a forced exit wait poll_sec too, so a dead quote can't spin
        self._last_poll = now
        held = list(self.book.open)
        try:
            qs, quoted = await self.chains.quotes([c for p in held for c in p.contracts]), True
        except Exception as ex:
            if not any(due.values()):
                raise
            self._log(now, "warn", f"book E: quotes failed during a forced exit ({ex})")
            qs, quoted = [None] * sum(len(p.contracts) for p in held), False
        i = 0
        for p in held:
            pq, i = qs[i:i + len(p.contracts)], i + len(p.contracts)
            usable = all(leg_problem(q, now, self.f["max_quote_age_s"], 1.0, self.f["tick_exempt"], False) is None for q in pq)
            if usable:
                p.last_quote_ts = now
                p.mark = round(ComboQuote(p.legs, pq).mid(False), 3)
                p.peak = max(p.peak, p.mark)
            reason = due[p.id]
            if reason:
                tries = self._exit_tries[p.id] = self._exit_tries.get(p.id, 0) + 1
                if quoted:          # without fresh quotes a paper fill would use stale cached prices
                    await self._exit(p, ExitIntent(reason, urgent=True), now)
                if p.status == "open" and ((quoted and any(q is None for q in pq)) or tries >= MAX_EXIT_TRIES):
                    self._close_at_mark(p, reason, now)
                continue
            it = R.tp_stop(p.meta["structure"], p.entry, p.mark, self.c) if usable else None
            if it:
                await self._exit(p, it, now)
                continue
            self.ej.save(p, now)
            if now - self._last_emit >= 2:
                self._last_emit = now
                d = p.to_dict()
                self.e.bus.emit("book_position", now, event="update", pos={k: d[k] for k in LIVE_FIELDS})

    async def _exit(self, pos: ComboPosition, it: ExitIntent, now: float) -> None:
        if pos.status != "open" or pos.exiting or pos.qty <= 0:
            return
        pos.exiting = True
        try:
            res = await self.exec.work(pos.legs, pos.contracts, pos.qty, False, False, it.urgent, now)
            self._order_event(now, pos.meta.get("symbol"), "close", pos.qty, res, it.reason)
            if res.filled_qty <= 0:
                self._log(now, "warn", f"book E exit not filled ({it.reason}): {res.message or res.status}")
                return
            n = res.filled_qty
            pos.realized += pos.pnl_per_share(res.avg_price) * 100 * n
            pos.fees += self.fee * len(pos.legs) * n
            pos.qty -= n
            pos.fills.append(self._fill(now, "close", res, it.reason))
            if pos.qty <= 0:
                self._close(pos, it.reason, now)
            else:
                self.book.blocked = f"partial exit, {pos.qty} left: reconcile before new orders"
                self.ej.save(pos, now)
        finally:
            pos.exiting = False

    def _close_at_mark(self, pos: ComboPosition, reason: str, now: float) -> None:
        """Paper only: a forced exit with a leg that has no quote (expired, delisted) closes at the last mark."""
        n = pos.qty
        pos.realized += pos.pnl_per_share(pos.mark) * 100 * n
        pos.fees += self.fee * len(pos.legs) * n
        pos.qty = 0
        why = f"{reason} (no quote on a leg: closed at last mark {pos.mark:.2f})"
        pos.fills.append({"ts": now, "side": "close", "qty": n, "px": pos.mark, "mid": None, "natural": None,
                          "limit": None, "ref_id": None, "why": why})
        self._log(now, "warn", f"book E: {pos.label}: {why}")
        self._close(pos, why, now)

    def _close(self, pos: ComboPosition, reason: str, now: float) -> None:
        pos.status, pos.closed_ts, pos.exit_reason = "closed", now, reason
        self._exit_tries.pop(pos.id, None)
        net = pos.realized - pos.fees
        self.book.on_close(pos, net)
        self.account.on_closed(net)
        self.e.journal.record_trade(str(session_date(now)), self.e.mode, pos, book="E")
        self.ej.save(pos, now)
        self.e.bus.emit("book_closed", now, pos=pos.to_dict(), net=round(net, 2))
        self.books_changed(now)

    # ------------------------------------------------------------ engine controls
    def halt_all(self, reason: str, flatten: bool = False) -> None:
        self.account.halt(reason, flatten)

    async def kill(self, now: float) -> None:
        self.account.halt("KILL switch", flatten=True)
        await self.flatten("KILL switch", now)

    async def flatten(self, reason: str, now: float) -> None:
        if reason == "shutdown":
            if self.book.open:
                self._log(now, "info", f"book E keeps {len(self.book.open)} paper position(s) overnight "
                                       "(saved in e_positions; sold before each report)")
            return
        held = list(self.book.open)
        if not held:
            return
        try:
            await self.chains.quotes([c for p in held for c in p.contracts])       # fresh prices for the fills
        except Exception as ex:
            self._log(now, "warn", f"book E: quotes for flatten failed ({ex})")
        for p in held:
            try:
                await self._exit(p, ExitIntent(reason, urgent=True), now)
            except Exception as ex:                  # keep going: the other positions still matter
                log.exception("flatten %s failed", p.label)
                self._log(now, "error", f"book E: flatten {p.label} failed: {ex}")

    # ------------------------------------------------------------ events
    def _fill(self, now, side, res, why) -> dict:
        r = res.raw or {}
        return {"ts": now, "side": side, "qty": res.filled_qty, "px": res.avg_price, "mid": r.get("mid"),
                "natural": r.get("natural"), "limit": r.get("limit"), "ref_id": r.get("ref_id"), "why": why}

    def _order_event(self, now, sym, action, qty, res, why) -> None:
        self.e.bus.emit("book_order", now, book="E", action=action, qty=qty, status=res.status, filled=res.filled_qty,
                        price=res.avg_price, limit=(res.raw or {}).get("limit"), mid=(res.raw or {}).get("mid"),
                        natural=(res.raw or {}).get("natural"), why=f"{sym} {why}", review=res.review)

    def _skip(self, now: float, why: str) -> None:
        self.book.skips.append({"ts": now, "book": "E", "why": why})
        self.e.bus.emit("book_skip", now, book="E", why=why)

    def _log(self, now: float, level: str, msg: str) -> None:
        self.e.bus.emit("log", now, level=level, msg=msg)


E_CALLS_PER_S = 1.0


def build_e(engine, cfg, rh=None, mode: str = "paper", provider: str = "sim", vix=None, chains=None):
    """EHost for `books.E_earnings_iv`, or None when it is off. Paper only (enforced): E fills through its own
    PaperBroker; in shadow/live `rh` only reviews each order (review_option_order), it never places one."""
    bc = (cfg.get("books") or {}).get(KEY)
    if not isinstance(bc, dict) or not bc.get("enabled"):
        return None
    if bc.get("paper_only") is not True:
        raise SystemExit(f"books.{KEY}: only a paper path exists for book E; set paper_only: true")
    if chains is None:
        if provider == "sim":
            log.warning("book E: no single-stock option data in sim; E is idle this run")
            return None
        if rh is None:
            log.warning("book E needs Robinhood option data (quotes_source robinhood/auto); E is off this run")
            return None
        from ..iv import Pacer, RobinhoodChains
        from ..iv_recorder import settings as iv_settings
        chains = RobinhoodChains(rh, iv_settings(cfg)["cache_dir"], Pacer(E_CALLS_PER_S))
    return EHost(engine, cfg, chains, vix=vix, reviewer=rh if mode in ("shadow", "live") else None)
