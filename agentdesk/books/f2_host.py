"""Runs book F2 (single-name call and put debit spreads, research/strategy_f2_prereg.md) next to the other hosts.

Paper only: F2 fills through its own PaperBroker at mid + fill_frac of the way to natural (single-name option markets
are wider than SPY's, so the 1-cent SPY model would flatter it). In shadow/live the first price of each order also goes
to review_option_order. F2 never touches engine.broker.
Signals: C arms from F1's 09:35 scan (F2's names, green, RVOL5 >= 2) and buys when the stock trades above the OR high
before 10:30 ET; P scans F2's names at 15:40 ET for a >= 3% up day on >= 1.8x average volume.
F2 holds for days: open positions live in f2_positions, are restored at startup, are left out of the engine's
10-second quote watchdog (F2 quotes them every poll_sec itself) and are NOT sold at shutdown. The kill switch, a
safety halt, an F2 halt and the dashboard Flatten button still sell them. A report inside the hold blocks the entry.
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import date

from ..brokers.base import RateLimited
from ..brokers.paper import PaperBroker
from ..clock import is_rth, session_date
from ..config import hhmm
from ..iv import atm_strike
from . import f2_spreads as S
from . import f_stocks_in_play as F
from .account import AccountRisk
from .base import ExitIntent
from .book import Book
from .combo import ComboPosition, ComboQuote, leg_problem, paper_fair
from .f2_journal import F2Journal
from .fills import ComboExecutor
from .host import FILLS

log = logging.getLogger("agentdesk.book_f2")
MAX_EXIT_TRIES = 3
LIVE_FIELDS = ("id", "book", "qty", "mark", "peak", "unrealized", "realized", "fees", "total_pnl", "pnl_pct",
               "max_loss", "status")


class DebitSpreads:
    name = "DEBIT SPREADS"

    def __init__(self, cfg: dict):
        self.c = cfg

    def new_day(self, day) -> None: ...

    def plan(self, pos) -> str:
        m = pos.meta
        first = f"; day 1 stop if {m['symbol']} trades below {m['or_low']:.2f}" if m.get("or_low") else ""
        return (f"take profit at {pos.target:.2f} (or {self.c['take_profit_width']:.0%} of the {pos.width:g} width), "
                f"stop at {pos.stop:.2f}{first}; time exit {m['exit_day']} {m['exit_et']} ET")


def realized_vol(daily: list[dict], n: int = 20) -> float | None:
    """Annualized close-to-close volatility over the last n daily bars."""
    cs = [b["c"] for b in daily[-(n + 1):]]
    if len(cs) < n + 1 or min(cs) <= 0:
        return None
    rs = [math.log(b / a) for a, b in zip(cs, cs[1:])]
    m = sum(rs) / len(rs)
    return math.sqrt(sum((r - m) ** 2 for r in rs) / (len(rs) - 1) * 252)


class F2Host:
    def __init__(self, engine, cfg, chains, data=None, fhost=None, account: AccountRisk | None = None,
                 j: F2Journal | None = None, reviewer=None, calendar_fn=None):
        from ..desks import holidays
        self.e, self.cfg, self.chains, self.data, self.fhost = engine, cfg, chains, data, fhost
        self.c = cfg["books"][S.KEY]
        self.book = Book(S.KEY, self.c, DebitSpreads(self.c))
        self.book.max_trades = int(self.c["max_new_day"])
        self.account = account or AccountRisk((cfg.get("books") or {}).get("account"))
        self.other_risk = lambda: sum(p.entry * 100 * p.qty for p, _ in self.e.open)     # A (+ others via HostGroup)
        self.books_changed = lambda now: None      # HostGroup: refresh the dashboard's book strip
        self.f = {**FILLS, **((cfg.get("books") or {}).get("fills") or {})}
        self.broker = PaperBroker(chains)
        self.broker.combo_model, self.broker.combo_frac = "mid_frac", float(self.c["fill_frac"])
        self.exec = ComboExecutor(self.broker, self.f, reviewer)
        self.fee = self.account.c["fee_per_leg"]
        self.j = j or F2Journal(engine.journal.db)
        self.calendar_fn = calendar_fn or self._crew_calendar
        self.holidays = holidays(cfg)
        self.universe = {s.upper() for s in self.c["universe"]}
        self.max_errors = (cfg["risk"].get("watchdog") or {}).get("max_consecutive_errors", 3)
        self.day = None
        self._busy, self._tasks = False, set()
        self._last_poll = self._last_emit = self._last_rate_log = -1e18
        self._exit_tries: dict = {}
        self._reset_day(None)

    def _reset_day(self, day) -> None:
        self.day = day
        self.armed: dict = {}              # symbol -> F.ScanRow, C setups waiting for the OR-high break
        self.c_armed = self.p_done = False
        self.cal, self.cal_ok = None, False
        self._last_trigger_poll = -1e18
        self.spot: dict = {}              # symbol -> (price, ts) from F2's own equity polls

    async def _crew_calendar(self, today):
        from ..desks import load_calendar
        if self.e.crew is None:
            return None
        cal, src = await load_calendar(self.e.crew, today)
        return None if src.startswith("Robinhood calendar unavailable") else cal

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

    def detail(self) -> dict:
        return {"armed": sorted(self.armed), "c_armed": self.c_armed, "p_done": self.p_done,
                "universe": sorted(self.universe)}

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        now = self.e.feed.now()
        self._reset_day(session_date(now))
        rows, bad = self.j.open_positions()
        for p in rows:
            p.last_quote_ts = now
            self.book.open.append(self._tag(p))
            self._log(now, "warn", f"book F2: restored {p.label}, {p.qty} @ {p.entry:.2f}; exit {p.meta.get('exit_day')}")
        if bad:
            self.book.halt(f"unreadable f2_positions rows: {', '.join(bad)}")
            self._log(now, "error", f"book F2 halted: {self.book.halt_reason}")

    async def on_bar(self, bar) -> None:
        return None

    async def on_second(self, now: float) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            d = session_date(now)
            if d != self.day:
                self._reset_day(d)
                self.book.reset_day()
            if S.is_session(d, self.holidays):
                await self._tick(now)
        finally:
            self._busy = False

    async def run(self, coro, inline: bool) -> None:
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
                self._log(now, "warn", f"book F2: Robinhood rate limit, calls paused and retried ({str(ex)[:120]})")
        except Exception as ex:
            b.failed(kind)
            log.exception("book F2 failed (%d in a row)", b.errors)
            self._log(now, "error", f"book F2: {ex} ({b.errors} in a row)")
            if b.errors >= self.max_errors and not b.halted:
                b.halt(f"{b.errors} errors in a row: {ex}")
                self._log(now, "error", f"book F2 halted and flattening: {b.halt_reason}")
                await self.flatten(f"book halted: {b.halt_reason}", now)

    async def _tick(self, now: float) -> None:
        if self.book.open:
            await self._manage(now)
        t = F.et_time(now)
        if not self.c_armed and self.fhost is not None and getattr(self.fhost, "scan", None) is not None:
            self._arm_calls(now)
        if self.armed:
            if t >= hhmm(self.c["entry_cutoff_et"]):
                self._log(now, "info", f"book F2: 10:30 ET cutoff, disarmed {', '.join(sorted(self.armed))}")
                self.armed = {}
            elif now - self._last_trigger_poll >= float(self.c["trigger_poll_sec"]):
                self._last_trigger_poll = now
                await self._triggers(now)
        p0 = hhmm(self.c["entry_p_et"])
        if not self.p_done and p0 <= t and (t.hour * 60 + t.minute) < p0.hour * 60 + p0.minute + 10:    # a 10-minute window
            self.p_done = True
            if self.e.risk.early_close(now):
                self._skip(now, "half day: no 15:40 ET put scan")
            else:
                await self._put_scan(now)

    # ------------------------------------------------------------ signals
    def _arm_calls(self, now: float) -> None:
        self.c_armed = True
        picks = S.call_signals(self.fhost.scan.rows, self.universe, self.c)
        self.armed = {r.symbol: r for r in picks}
        self._log(now, "info", "book F2 call setups armed: "
                               + (", ".join(f"{r.symbol} {r.rvol5:.1f}x OR high {r.or_high:.2f}" for r in picks) or "none"))

    async def _last_prices(self, syms: list[str], now: float) -> dict[str, float]:
        """Last trade prices: F1's fresh quotes when it has them, else one F2 poll for the rest."""
        out, missing = {}, []
        for s in syms:
            q = self.fhost._fresh(s, now) if self.fhost is not None else None
            if q is not None:
                out[s] = q.last if q.last is not None else q.ask
            else:
                missing.append(s)
        if missing and self.data is not None:
            for s, q in (await self.data.quotes(missing)).items():
                out[s] = q.last if q.last is not None else q.ask
        return out

    async def _triggers(self, now: float) -> None:
        px = await self._last_prices(sorted(self.armed), now)
        for s, r in list(self.armed.items()):
            p = px.get(s)
            if p is not None and p > r.or_high:
                self.armed.pop(s, None)
                await self._consider(s, S.CALL, now, p, {"rvol5": r.rvol5, "or_high": r.or_high, "or_low": r.or_low,
                                                         "gap_pct": r.gap_pct, "trigger": p})

    async def _put_scan(self, now: float) -> None:
        if self.data is None:
            return self._skip(now, "no equity data for the 15:40 ET put scan")
        syms = sorted(self.universe)
        daily = await self.data.daily_bars(syms, self.day)
        cur = F.et(now)
        bars = await self.data.minute_bars(syms, self.day, 570, cur.hour * 60 + cur.minute)
        stats = {s: S.day_stats(daily.get(s, []), sorted(bars.get(s, []), key=lambda b: b["t"])) for s in syms}
        picks = S.put_signals(stats, self.c)
        self._log(now, "info", "book F2 put scan: " + (", ".join(f"{s} +{x['chg_pct']:.1f}% {x['vol_ratio']:.1f}x vol"
                                                                 for s, x in picks) or "no name up 3% on 1.8x volume"))
        for s, x in picks:
            await self._consider(s, S.PUT, now, x["last"], {**x, "rv20": realized_vol(daily.get(s, []))})

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
        if self.book.blocked:
            return self.book.blocked
        if self.book.trades >= self.book.max_trades:
            return f"max {self.book.max_trades} new spreads today"
        if len(self.book.open) >= int(self.c["max_open"]):
            return f"max {self.c['max_open']} open"
        return None

    async def _calendar(self, now: float):
        if self.cal_ok:
            return self.cal
        try:
            cal = await self.calendar_fn(self.day)
        except Exception as ex:
            cal = None
            self._log(now, "warn", f"book F2: earnings calendar failed ({ex})")
        if cal is not None:
            self.cal, self.cal_ok = cal, True
        return cal

    async def _consider(self, sym: str, setup: str, now: float, spot: float, signal: dict) -> None:
        rec = {"day": str(self.day), "ts": now, "symbol": sym, "setup": setup, "spot": spot, "signal": signal}
        name = f"{sym} {S.SETUP[setup].lower()}"

        def skip(reason: str) -> None:
            self.j.decision(**rec, outcome="skipped", reason=reason)
            self._skip(now, f"{name}: {reason}")

        why = self._gate()
        if why is None and any(p.meta.get("symbol") == sym for p in self.book.open):
            why = f"already holding {sym}"
        if why is None and self.j.traded_today(str(self.day), sym):
            why = f"{sym} already traded today"
        if why:
            return skip(why)
        xd = S.exit_day(self.day, setup, self.holidays, self.c)
        cal = await self._calendar(now)
        if cal is None and self.c.get("require_calendar", True):
            return skip("earnings calendar unavailable: not trading blind into a report")
        why = S.earnings_conflict(sym, cal, self.day, xd)
        if why:
            return skip(why)
        exp = S.pick_expiry(await self.chains.expirations(sym), self.day, self.c)
        if exp is None:
            return skip(f"no expiry {self.c['dte'][0]}-{self.c['dte'][1]} days out")
        rec["expiry"] = str(exp)
        ks = await self.chains.strikes(sym, exp)
        k = atm_strike([ks], spot)
        if k is None:
            return skip("no ATM strike listed")
        age, spread, tick = self.f["max_quote_age_s"], float(self.c["max_leg_spread_pct"]), float(self.c["tick_exempt"])
        qc, qp = await self.chains.quotes([self.chains.contract(sym, exp, k, "call"), self.chains.contract(sym, exp, k, "put")])
        for label, q in (("ATM call", qc), ("ATM put", qp)):
            p = leg_problem(q, now, age, spread, tick, True)
            if p:
                return skip(f"{label} {k:g}: {p}")
        straddle = (qc.bid + qc.ask) / 2 + (qp.bid + qp.ask) / 2
        right = S.RIGHT[setup]
        short_k = S.spread_strikes(ks, k, right, straddle * float(self.c["width_straddle_x"]), int(self.c["min_steps"]))
        if short_k is None:
            return skip(f"chain too short for a {self.c['min_steps']}-strike spread")
        cs = [self.chains.contract(sym, exp, k, right), self.chains.contract(sym, exp, short_k, right)]
        q_long = qc if right == "call" else qp
        [q_short] = await self.chains.quotes([cs[1]])
        p = leg_problem(q_short, now, age, spread, tick, True)
        if p:
            return skip(f"short leg {short_k:g}: {p}")
        legs = S.legs_for(setup, k, short_k, (exp - self.day).days)
        fair = paper_fair(ComboQuote(legs, [q_long, q_short]), False, True, "mid_frac", 0.0, float(self.c["fill_frac"]))
        width = abs(short_k - k)
        iv_l, iv_s = getattr(q_long, "iv", None), getattr(q_short, "iv", None)
        rv = signal.get("rv20")
        if rv is None and self.data is not None:
            rv = realized_vol((await self.data.daily_bars([sym], self.day)).get(sym, []))
        atm_iv = [x for x in (getattr(qc, "iv", None), getattr(qp, "iv", None)) if x]
        rec.update(long_k=k, short_k=short_k, debit=fair, width=width, iv_long=iv_l, iv_short=iv_s,
                   skew=round(iv_s / iv_l, 3) if iv_l and iv_s else None,
                   iv_rv=round(sum(atm_iv) / len(atm_iv) / rv, 3) if atm_iv and rv else None)
        why = S.structure_problem(fair, width, self.c)
        if why:
            return skip(why)
        lots, size_why = S.lots_for(fair, self.c)
        if lots < 1:
            return skip(size_why)
        ok, why = self.account.can_open(round(fair * 100 * lots, 2), self._all_risk(), self.e.risk.st.day_pnl)
        if not ok:
            return skip(why)
        res = await self.exec.work(legs, cs, lots, False, True, False, now)
        self._order_event(now, sym, "open", lots, res, S.SETUP[setup])
        if res.filled_qty <= 0:
            return skip(f"entry not filled ({res.message or res.status})")
        half = self.e.risk.early_close(now)
        meta = {"symbol": sym, "setup_key": setup, "entry_day": str(self.day), "exit_day": str(xd),
                "exit_et": f"{S.exit_time_et(setup, self.c, half):%H:%M}", "expiry": str(exp), "spot": spot,
                "debit_width": round(fair / width, 3), "straddle": round(straddle, 2)}
        for k2 in ("iv_long", "iv_short", "skew", "iv_rv"):
            if rec.get(k2) is not None:
                meta[k2] = rec[k2]
        if setup == S.CALL:
            meta.update(or_low=signal["or_low"], or_high=signal["or_high"])
        pos = ComboPosition(S.BOOK, f"{sym} {S.SETUP[setup]}", legs, cs, res.filled_qty, res.avg_price, False, width, now,
                            strike_reason=f"{sym} {k:g}/{short_k:g} {right}s, expiry {exp}; width = straddle "
                                          f"{straddle:.2f} (spot {spot:.2f})",
                            entry_reasons=[size_why, _signal_note(setup, signal)], meta=meta,
                            id=f"F2-{uuid.uuid4().hex[:8]}")
        pos.fees = self.fee * len(legs) * res.filled_qty
        pos.fills.append(self._fill(now, "open", res, "entry"))
        pos.target = round(pos.entry * float(self.c["take_profit_x"]), 2)
        pos.stop = round(pos.entry * float(self.c["stop_x"]), 2)
        pos.meta["plan"] = self.book.strategy.plan(pos)
        if res.status == "partial":
            self.book.blocked = f"partial fill {res.filled_qty}/{lots}: reconcile before new orders"
        self.book.on_open(self._tag(pos))
        self.j.save(pos, now)
        self.j.decision(**rec, outcome="opened", reason=res.status, lots=res.filled_qty)
        self.e.bus.emit("book_position", now, pos=pos.to_dict(), event="open", size_note=size_why)
        self.books_changed(now)

    # ------------------------------------------------------------ management
    def _forced(self, p: ComboPosition, now: float, half: bool) -> str | None:
        st = self.e.risk.st
        if self.account.halted and self.account.flatten:
            return self.account.halt_reason
        if self.e.risk.account_flatten():
            return st.halt_reason or "kill switch"
        if self.book.halted:
            return f"book halted: {self.book.halt_reason}"
        m, today = p.meta, str(session_date(now))
        if any(c.expiry <= today for c in p.contracts):
            return "expiry day: never held into expiration"
        t = S.exit_time_et(m.get("setup_key", S.CALL), self.c, half)
        if today > m["exit_day"] or (today == m["exit_day"] and F.et_time(now) >= t):
            return f"time exit {m['exit_day']} {t:%H:%M} ET"
        return None

    async def _manage(self, now: float) -> None:
        if not is_rth(now):
            return
        half = self.e.risk.early_close(now)
        due = {p.id: self._forced(p, now, half) for p in self.book.open}
        new_due = any(r and pid not in self._exit_tries for pid, r in due.items())
        if not new_due and now - self._last_poll < float(self.c["poll_sec"]):
            return
        self._last_poll = now
        held = list(self.book.open)
        try:
            qs, quoted = await self.chains.quotes([c for p in held for c in p.contracts]), True
        except Exception as ex:
            if not any(due.values()):
                raise
            self._log(now, "warn", f"book F2: quotes failed during a forced exit ({ex})")
            qs, quoted = [None] * sum(len(p.contracts) for p in held), False
        first_day = [p for p in held if p.meta.get("or_low") and p.meta.get("entry_day") == str(self.day)]
        spots = await self.chains.spots(sorted({p.meta["symbol"] for p in first_day})) if first_day else {}
        i = 0
        for p in held:
            pq, i = qs[i:i + len(p.contracts)], i + len(p.contracts)
            usable = all(leg_problem(q, now, self.f["max_quote_age_s"], 1.0, self.f["tick_exempt"], False) is None for q in pq)
            if usable:
                p.last_quote_ts = now
                p.mark = round(ComboQuote(p.legs, pq).mid(False), 3)
                p.peak = max(p.peak, p.mark)
            reason = due[p.id]
            sp = spots.get(p.meta["symbol"]) if p in first_day else None
            if reason is None and sp is not None and sp <= p.meta["or_low"]:
                reason = f"thesis stop: {p.meta['symbol']} {sp:.2f} at or below the OR low {p.meta['or_low']:.2f}"
            if reason:
                tries = self._exit_tries[p.id] = self._exit_tries.get(p.id, 0) + 1
                if quoted:          # without fresh quotes a paper fill would use stale cached prices
                    await self._exit(p, ExitIntent(reason, urgent=True), now)
                if p.status == "open" and ((quoted and any(q is None for q in pq)) or tries >= MAX_EXIT_TRIES):
                    self._close_at_mark(p, reason, now)
                continue
            it = S.tp_stop(p.entry, p.mark, p.width, self.c) if usable else None
            if it:
                await self._exit(p, it, now)
                continue
            self.j.save(p, now)
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
                self._log(now, "warn", f"book F2 exit not filled ({it.reason}): {res.message or res.status}")
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
                self.j.save(pos, now)
        finally:
            pos.exiting = False

    def _close_at_mark(self, pos: ComboPosition, reason: str, now: float) -> None:
        """Paper only: a forced exit with a leg that has no quote closes at the last mark."""
        n = pos.qty
        pos.realized += pos.pnl_per_share(pos.mark) * 100 * n
        pos.fees += self.fee * len(pos.legs) * n
        pos.qty = 0
        why = f"{reason} (no quote on a leg: closed at last mark {pos.mark:.2f})"
        pos.fills.append({"ts": now, "side": "close", "qty": n, "px": pos.mark, "mid": None, "natural": None,
                          "limit": None, "ref_id": None, "why": why})
        self._log(now, "warn", f"book F2: {pos.label}: {why}")
        self._close(pos, why, now)

    def _close(self, pos: ComboPosition, reason: str, now: float) -> None:
        pos.status, pos.closed_ts, pos.exit_reason = "closed", now, reason
        self._exit_tries.pop(pos.id, None)
        net = pos.realized - pos.fees
        self.book.on_close(pos, net)
        self.account.on_closed(net)
        self.e.journal.record_trade(str(session_date(now)), self.e.mode, pos, book=S.BOOK)
        self.j.save(pos, now)
        self.e.bus.emit("book_closed", now, pos=pos.to_dict(), net=round(net, 2))
        lim = abs(float(self.c["daily_loss"]))
        if self.book.day_pnl <= -lim and not self.book.blocked:
            self.book.blocked = f"daily loss ${self.book.day_pnl:.0f} <= -${lim:.0f}: no new F2 spreads today"
            self._log(now, "warn", f"book F2: {self.book.blocked}")
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
                self._log(now, "info", f"book F2 keeps {len(self.book.open)} paper spread(s) overnight "
                                       "(saved in f2_positions)")
            return
        held = list(self.book.open)
        if not held:
            return
        try:
            await self.chains.quotes([c for p in held for c in p.contracts])
        except Exception as ex:
            self._log(now, "warn", f"book F2: quotes for flatten failed ({ex})")
        for p in held:
            try:
                await self._exit(p, ExitIntent(reason, urgent=True), now)
            except Exception as ex:
                log.exception("flatten %s failed", p.label)
                self._log(now, "error", f"book F2: flatten {p.label} failed: {ex}")

    # ------------------------------------------------------------ events
    def _fill(self, now, side, res, why) -> dict:
        r = res.raw or {}
        return {"ts": now, "side": side, "qty": res.filled_qty, "px": res.avg_price, "mid": r.get("mid"),
                "natural": r.get("natural"), "limit": r.get("limit"), "ref_id": r.get("ref_id"), "why": why}

    def _order_event(self, now, sym, action, qty, res, why) -> None:
        self.e.bus.emit("book_order", now, book=S.BOOK, action=action, qty=qty, status=res.status,
                        filled=res.filled_qty, price=res.avg_price, limit=(res.raw or {}).get("limit"),
                        mid=(res.raw or {}).get("mid"), natural=(res.raw or {}).get("natural"), symbol=sym,
                        why=f"{sym} {why}", review=res.review)

    def _skip(self, now: float, why: str) -> None:
        b = self.book
        if b.skips and b.skips[-1]["why"] == why:
            return
        b.skips.append({"ts": now, "book": S.BOOK, "why": why})
        self.e.bus.emit("book_skip", now, book=S.BOOK, why=why)

    def _log(self, now: float, level: str, msg: str) -> None:
        self.e.bus.emit("log", now, level=level, msg=msg)


def _signal_note(setup: str, s: dict) -> str:
    if setup == S.CALL:
        return (f"breakout {s.get('trigger', 0):.2f} > OR high {s.get('or_high', 0):.2f}, RVOL5 {s.get('rvol5') or 0:.1f}x"
                + (f", gap {s['gap_pct']:+.1f}%" if s.get("gap_pct") is not None else ""))
    return f"up {s.get('chg_pct', 0):.1f}% on {s.get('vol_ratio', 0):.1f}x average volume: fading into tomorrow"


F2_CALLS_PER_S = 1.0


def build_f2(engine, cfg, rh=None, mode: str = "paper", provider: str = "sim", fhost=None, chains=None, data=None):
    """F2Host for `books.F2_debit_spreads`, or None when it is off. Paper only (enforced): F2 fills through its own
    PaperBroker; in shadow/live `rh` only reviews each order (review_option_order), it never places one."""
    bc = (cfg.get("books") or {}).get(S.KEY)
    if not isinstance(bc, dict) or not bc.get("enabled"):
        return None
    if bc.get("paper_only") is not True:
        raise SystemExit(f"books.{S.KEY}: only a paper path exists for book F2; set paper_only: true")
    if chains is None:
        if provider == "sim":
            log.warning("book F2: no single-stock option data in sim; F2 is idle this run")
            return None
        if rh is None:
            log.warning("book F2 needs Robinhood option data (quotes_source robinhood/auto); F2 is off this run")
            return None
        from ..iv import Pacer, RobinhoodChains
        from ..iv_recorder import settings as iv_settings
        chains = RobinhoodChains(rh, iv_settings(cfg)["cache_dir"], Pacer(F2_CALLS_PER_S))
    if data is None:
        data = getattr(fhost, "data", None)
    if data is None and rh is not None:
        from ..feeds.f_data import RobinhoodEquityData
        data = RobinhoodEquityData(rh, cfg["books"].get("F1_stocks_in_play") or {})
    return F2Host(engine, cfg, chains, data=data, fhost=fhost, reviewer=rh if mode in ("shadow", "live") else None)
