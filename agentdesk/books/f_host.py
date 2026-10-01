"""Runs book F1 (stocks in play, long shares) next to book A and the B/C/D BookHost.

The engine calls on_second / on_bar / kill / flatten / halt_all through HostGroup (group.py). F is paper only:
every fill is simulated (PaperEquityBroker); in shadow/live each order is also sent to review_equity_order.

Day, in ET (the engine clock is CT):
  08:30   prep: S&P list + extras -> daily bars -> section 3 universe -> tradability -> prior 14 sessions' 09:30-09:34
          volume (RVOL5 base). Retried every minute until it works.
  09:35:05 scan: today's 09:30-09:34 bars -> RVOL5 + first candle -> top 5 green armed; red -> shadow shorts; every row
          journaled; the news tag starts in the background (observe only). Skipped when a high-impact event falls
          09:35-10:30 ET.
  -> 10:30 quotes every second: last trade above the OR high triggers a limit buy at OR high x 1.0005 (at most 3 sends
          per name, one entry per name); each completed 1-minute bar above the OR high also triggers.
  open    stop = fill - 0.10 x ATR14, on every quote (bid <= stop) and every 1-minute bar (gap -> bar open; entry and
          stop in the same bar -> stop). Exit 15:55 ET (12:55 on half-days), kill switch, safety halt, F halt.
F's own limits: $25 risk, $1,000 notional, 5 open, -$75 day halts F only (and flattens it). The account open-risk
cap and paper equity are shared with every book.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging

from ..brokers.base import RateLimited
from ..clock import session_date
from ..config import hhmm
from . import f_stocks_in_play as F
from .account import AccountRisk
from .book import Book
from .f_journal import FJournal, TradeRow

log = logging.getLogger("agentdesk.book_f")
KEY = "F1_stocks_in_play"
BOOK = "F1"
_ids = itertools.count(1)


class StocksInPlay:
    """The strategy object the Book holds (for its name and plan text); the rules live in f_stocks_in_play."""
    name = "STOCKS IN PLAY"

    def __init__(self, cfg: dict):
        self.c = cfg

    def new_day(self, day) -> None: ...

    def plan(self, pos) -> str:
        return f"stop {pos.stop:.2f} (0.10 x ATR {pos.atr:.2f}); exit {self.c['exit_et']} ET; no target, no trail"


class FHost:
    def __init__(self, engine, cfg, data, broker=None, account: AccountRisk | None = None, news=None,
                 fj: FJournal | None = None, reviewer=None):
        self.e, self.cfg, self.data = engine, cfg, data
        self.c = cfg["books"][KEY]
        self.book = Book(KEY, self.c, StocksInPlay(self.c))
        self.book.max_trades = int(self.c["max_positions"])
        self.account = account or AccountRisk((cfg.get("books") or {}).get("account"))
        self.other_risk = lambda: sum(p.entry * 100 * p.qty for p, _ in self.e.open)     # A (+ B/C/D via HostGroup)
        self.books_changed = lambda now: None      # HostGroup: refresh the dashboard's book strip
        if broker is None:
            from ..brokers.paper_equity import PaperEquityBroker
            broker = PaperEquityBroker(self, max_age=self.c.get("max_quote_age_s", 5))
        self.broker = broker
        self.fj = fj or FJournal(engine.journal.db)
        if news is None:
            from .f_news import NewsTagger
            news = NewsTagger(engine, cfg)
        self.news = news
        self.max_errors = (cfg["risk"].get("watchdog") or {}).get("max_consecutive_errors", 3)
        self.max_age = float(self.c.get("max_quote_age_s", 5))
        self.q: dict[str, F.Q] = {}
        self.day = None
        self._busy = False
        self._tasks: set = set()
        self._reset_day(None)

    # ------------------------------------------------------------ state
    def _reset_day(self, d) -> None:
        self.day = d
        self.book.reset_day()
        self.uni: dict[str, dict] = {}
        self.hist: list[dict] = []
        self.prepared = self.scanned = False
        self._prep_try = -1e18
        self.scan: F.ScanResult | None = None
        self.rows: dict[str, F.ScanRow] = {}
        self.armed: dict[str, F.ScanRow] = {}
        self.sends: dict[str, int] = {}
        self.status: dict[str, str] = {}
        self.shadows: dict[str, dict] = {}
        self.seen: dict[str, int] = {}          # last processed 1-minute bar per symbol
        self._last_poll = self._last_bar_min = -1e18
        self._last_rate_log = -1e18

    @property
    def enabled(self) -> bool:
        return True

    def positions(self) -> list:
        return list(self.book.open)

    def open_risk(self) -> float:
        return sum(p.risk for p in self.book.open)

    def half_day(self, now: float) -> bool:
        return self.e.risk.early_close(now)

    async def quote(self, sym: str):
        """The quote source the paper broker fills against: the latest 1-second poll."""
        return self.q.get(sym)

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        now = self.e.feed.now()
        today = session_date(now)
        stale, keep = [], []
        for r in self.fj.open_rows():
            d = r["session"]
            exit_ts = F.at_et(_date(d), F.exit_time_et(self.c, self.e.risk.early_close(F.at_et(_date(d), hhmm("12:00")))))
            (stale if d < str(today) or now >= exit_ts else keep).append(r)
        self._reset_day(today)
        if stale:
            names = ", ".join(f"{r['symbol']} ({r['session']})" for r in stale)
            self.book.halt(f"F1 shares never closed: {names}. Check them, then run `python -m agentdesk f-clear-stale`")
            self._log(now, "error", f"book F1 halted: {self.book.halt_reason}")
        for r in keep:
            p = F.FPos(r["symbol"], r["qty"], r["entry"], r["stop"], r["opened_ts"], atr=r["atr"] or 0.0,
                       or_high=r["or_high"] or 0.0, rvol5=r["rvol5"], rank=r["rank"], id=r["id"])
            p.shadow_stop, p.shadow_hit = r.get("shadow_stop"), bool(r.get("shadow_hit"))
            p.tags = json.loads(r["tags"]) if r.get("tags") else {}
            p.last_quote_ts = now
            self.book.open.append(p)
            self.book.trades += 1
            self.status[p.symbol] = "filled"
            self._log(now, "warn", f"book F1: restored open paper position {p.symbol} {p.qty} @ {p.entry:.2f}, stop {p.stop:.2f}")
        await self._check_account(now)

    async def _check_account(self, now: float) -> None:
        """Startup reconciliation (handoff 4.1): equity positions in the agentic account that F doesn't track."""
        get = getattr(self.broker, "positions", None)
        if get is None:
            return
        try:
            held = await get()
        except Exception as ex:
            self._log(now, "warn", f"book F1: could not read equity positions ({ex})")
            return
        extra = held                        # F is paper only: any real equity position is unexpected
        if not extra:
            return
        names = ", ".join(str(p.get("symbol") or "?") for p in extra)
        msg = f"unexpected equity position(s) in the agentic account: {names}. Close or move them, then restart"
        self.account.halt(msg)
        self.e.risk.halt(msg, sticky=False)         # a startup check: it re-runs on the next start, like the engine's own
        self._log(now, "error", msg)

    async def on_bar(self, bar) -> None:
        return None                 # F reads its own symbols' bars; SPY bars don't drive it

    async def on_second(self, now: float) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            d = session_date(now)
            if d != self.day:
                self._reset_day(d)
            if F.et(now).weekday() >= 5:
                return
            await self._safe(self._tick(now), now)
        finally:
            self._busy = False

    async def run(self, coro, inline: bool) -> None:
        """Run a hook inline (sim) or as a background task, under F's own error count (like BookHost.run)."""
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
                self._log(now, "warn", f"book F1: Robinhood rate limit, calls paused and retried ({str(ex)[:120]})")
        except Exception as ex:
            b.failed(kind)
            log.exception("book F1 failed (%d in a row)", b.errors)
            self._log(now, "error", f"book F1: {ex} ({b.errors} in a row)")
            if b.errors >= self.max_errors and not b.halted:
                b.halt(f"{b.errors} errors in a row: {ex}")
                self._log(now, "error", f"book F1 halted and flattening: {b.halt_reason}")
                await self.flatten(f"book halted: {b.halt_reason}", now)

    async def _tick(self, now: float) -> None:
        t = F.et_time(now)
        exit_t = F.exit_time_et(self.c, self.half_day(now))
        if not self.prepared and hhmm(self.c.get("prep_et", "08:30")) <= t < exit_t and now - self._prep_try >= 60:
            self._prep_try = now
            await self._prepare(now)
        if self.prepared and not self.scanned and t >= _scan_time(self.c):
            await self._scan(now)
        active = set(self.armed) | {p.symbol for p in self.book.open}
        if active and now - self._last_poll >= 1.0:
            self._last_poll = now
            await self._poll(sorted(active), now)
            await self._manage(now)
            await self._triggers(now)
        minute = int(now // 60)
        bar_syms = set(self.armed) | {p.symbol for p in self.book.open} | {s for s, x in self.shadows.items() if not x.get("done")}
        if bar_syms and minute != self._last_bar_min and t >= _scan_time(self.c):
            self._last_bar_min = minute
            await self._bars(sorted(bar_syms), now)

    # ------------------------------------------------------------ prep and scan
    async def _prepare(self, now: float) -> None:
        u = self.c["universe"]
        daily, sp, broad = {}, set(), False
        if u.get("source") == "all_us" and hasattr(self.data, "us_daily"):
            daily = await self.data.us_daily(self.day)
            broad = bool(daily)
            if not broad:
                self._log(now, "warn", "book F1: no broad US universe today (Alpaca keys or symbol list missing); "
                                       "using the S&P universe")
        if not broad:
            sp = set(await self.data.sp500())
            daily = await self.data.daily_bars(sorted(sp | set(u.get("extra", []))), self.day)
        # the 09:35 scan costs one call per 10 names on Robinhood, so its universe is capped; SIP bars lift the cap
        cap = u.get("max_names") if self.c.get("bars_source", "robinhood") == "robinhood" else None
        uni = F.universe(daily, sp, self.c, broad=broad, cap=cap)
        ok = await self.data.tradable(sorted(uni))
        dropped = sorted(set(uni) - set(ok))
        self.uni = {s: v for s, v in uni.items() if s in ok}
        dates = sorted({b["d"] for bs in daily.values() for b in bs if b["d"] < self.day})[-14:]
        vols = await self.data.or_volumes(sorted(self.uni), dates)
        self.hist = [vols.get(d, {}) for d in dates]
        self.prepared = True
        self._log(now, "info", f"book F1: universe {len(self.uni)} names "
                               + (f"(all US, top {cap} by dollar volume + extras)" if broad and cap else
                                  "(all US + extras)" if broad else f"(S&P {len(sp)} + extras)")
                               + (f"; not tradable: {', '.join(dropped)}" if dropped else "")
                               + f"; RVOL5 base from {len(dates)} sessions")

    def _event_in_window(self, now: float) -> str | None:
        before = self.cfg["risk"]["event_blackout"]["before_min"] * 60
        lo, hi = F.at_et(self.day, _scan_time(self.c)), F.at_et(self.day, hhmm(self.c["entry_cutoff_et"]))
        for b in self.e.risk.st.blackouts:
            ts = b.start + before
            if lo <= ts <= hi:
                return b.name
        return None

    async def _scan(self, now: float) -> None:
        self.scanned = True
        if F.et_time(now) >= hhmm(self.c["entry_cutoff_et"]):
            return self._skip(now, "started after the 10:30 ET entry cutoff: no scan today")
        ev = self._event_in_window(now)
        if ev:
            self.e.bus.emit("f_scan", now, skipped=f"high-impact event {ev} between 09:35 and 10:30 ET", rows=[])
            return self._skip(now, f"scan skipped: high-impact event {ev} between 09:35 and 10:30 ET")
        bars = await self.data.minute_bars(sorted(self.uni), self.day, 570, 575)
        rows = []
        for s, info in self.uni.items():
            r = F.scan_row(s, info, bars.get(s, []), [h[s] for h in self.hist if s in h])
            if r is not None:
                rows.append(r)
        res = F.rank_candidates(rows, self.c)
        self.scan, self.rows = res, {r.symbol: r for r in res.rows}
        self.armed = {r.symbol: r for r in res.picks if self.status.get(r.symbol) != "filled"}
        for r in res.picks:
            self.status.setdefault(r.symbol, "armed")
        for r in res.shorts:
            self.shadows[r.symbol] = {"row": r, "pos": None, "done": False}
        self.fj.record_scan(str(self.day), now, res.rows)
        missing = len(self.uni) - len(rows)
        self._log(now, "info", f"book F1 scan: {len(rows)} names with data ({missing} dropped), picks "
                               f"{', '.join(f'{r.symbol} {r.rvol5:.1f}x' for r in res.picks) or 'none'}; "
                               f"shadow shorts {', '.join(r.symbol for r in res.shorts) or 'none'}")
        self._emit_scan(now)
        if res.picks:
            self.e.bus.emit("crew", now, desk="tape", phase="say", who="tape",
                            text="In play: " + " ".join(r.symbol for r in res.picks))
        greens = [r for r in res.rows if r.direction == "green" and r.rank]
        runners = [r for r in greens if not r.picked][:5]
        self.news.start(now, list(res.picks), runners, lambda tags, late: self._on_news(tags, late, now))

    def _on_news(self, tags: dict, late: bool, started: float) -> None:
        now = self.e.feed.now()
        for sym, tag in (tags or {}).items():
            r = self.rows.get(sym)
            if r is not None:
                r.news = tag
            for p in self.book.open:
                if p.symbol == sym:
                    p.news = tag
            self.fj.set_news(str(self.day), sym, tag, now, late)
        if late:
            self._log(now, "warn", f"book F1: news tag landed late ({now - started:.0f}s); stored, never waited on")
        self._emit_scan(now)

    # ------------------------------------------------------------ quotes, entries, exits
    async def _poll(self, syms: list[str], now: float) -> None:
        got = await self.data.quotes(syms)
        for s, q in got.items():
            if q is not None:
                self.q[s] = q

    def _fresh(self, sym: str, now: float):
        q = self.q.get(sym)
        return q if q is not None and now - q.ts <= self.max_age else None

    async def _manage(self, now: float) -> None:
        half = self.half_day(now)
        for p in list(self.book.open):
            q = self._fresh(p.symbol, now)
            if q is not None:
                p.mark, p.last_quote_ts = round((q.bid + q.ask) / 2, 4), now
                if p.shadow_stop is not None and q.bid <= p.shadow_stop:
                    p.shadow_hit = True
            forced = self._forced(now)
            why = forced or (F.should_exit(now, p, q, self.c, half) if q is not None else None)
            if why is None and q is None and F.et_time(now) >= F.exit_time_et(self.c, half):
                why = F.should_exit(now, p, None, self.c, half)
            if why:
                await self._exit(p, why, now)
        await self._flatten_if_halted(now)
        if self.book.open:
            self.e.bus.emit("f_positions", now, positions=[p.to_dict() for p in self.book.open])

    def _forced(self, now: float) -> str | None:
        st = self.e.risk.st
        if self.account.halted and self.account.flatten:
            return self.account.halt_reason
        if st.halted and st.flatten_all:
            return st.halt_reason or "kill switch"
        if self.book.halted:
            return f"book halted: {self.book.halt_reason}"
        return None

    async def _triggers(self, now: float) -> None:
        cutoff = hhmm(self.c["entry_cutoff_et"])
        if F.et_time(now) >= cutoff:
            for s in list(self.armed):
                self._expire(s, now)
            return
        for s, r in list(self.armed.items()):
            q = self._fresh(s, now)
            if q is not None and (q.last or 0) > r.or_high:
                await self._enter(r, now, f"trade {q.last:.2f} above OR high {r.or_high:.2f}")

    def _expire(self, sym: str, now: float) -> None:
        self.armed.pop(sym, None)
        self.status[sym] = "expired"
        self.fj.set_status(str(self.day), sym, "expired")
        self._emit_scan(now)

    def _gate(self, now: float) -> tuple[bool, str]:
        st = self.e.risk.st
        if self.account.halted:
            return False, f"halted: {self.account.halt_reason}"
        if st.halted and st.flatten_all:
            return False, f"halted: {st.halt_reason}"
        if st.paused:
            return False, "paused"
        if self.book.halted:
            return False, f"book halted: {self.book.halt_reason}"
        if len(self.book.open) >= int(self.c["max_positions"]):
            return False, f"max {self.c['max_positions']} open"
        if F.et_time(now) >= hhmm(self.c["entry_cutoff_et"]):
            return False, "after the 10:30 ET entry cutoff"
        return True, "ok"

    async def _enter(self, r: F.ScanRow, now: float, why: str) -> None:
        ok, reason = self._gate(now)
        if not ok:
            return self._skip(now, f"{r.symbol}: {reason}")
        limit = F.entry_limit(r.or_high)
        stop0 = F.stop_price(limit, r.atr, self.c, r.or_low)
        qty = F.shares_for(limit, stop0, self.c)
        if qty < 1:
            self.armed.pop(r.symbol, None)
            self.status[r.symbol] = "skipped"
            return self._skip(now, f"{r.symbol}: 1 share breaks the ${self.c['risk_per_trade']} risk or "
                                   f"${self.c['max_notional']} notional cap")
        risk = qty * (limit - stop0)
        other = self.other_risk()
        ok, reason = self.account.can_open(risk, other + self.open_risk(), self.e.risk.st.day_pnl)
        if ok:
            held = sum(p.qty * p.entry for p in self.book.open)
            eq = self.account.equity(self.e.risk.st.day_pnl) - other
            if held + qty * limit > eq:
                ok, reason = False, f"no margin: ${held + qty * limit:.0f} of shares > ${eq:.0f} free paper equity"
        if not ok:
            return self._skip(now, f"{r.symbol}: {reason}")
        self.sends[r.symbol] = self.sends.get(r.symbol, 0) + 1
        res = await self.broker.buy(r.symbol, qty, limit, r.or_high, now)
        self._order_event(now, r.symbol, "buy", qty, limit, res, why)
        if res.filled_qty <= 0:
            if self.sends[r.symbol] >= int(self.c.get("max_sends", 3)):
                self.armed.pop(r.symbol, None)
                self.status[r.symbol] = "missed"
                self.fj.set_status(str(self.day), r.symbol, "missed")
            return self._skip(now, f"{r.symbol}: buy not filled ({res.message or res.status})")
        self.armed.pop(r.symbol, None)
        fill = res.avg_price
        p = F.FPos(r.symbol, res.filled_qty, fill, F.stop_price(fill, r.atr, self.c, r.or_low), now, atr=r.atr,
                   or_high=r.or_high, rvol5=r.rvol5, rank=r.rank, news=r.news, id=f"F1-{self.day}-{r.symbol}-{next(_ids)}")
        if self.c.get("stop_mode") == "or_low":
            p.shadow_stop = F.paper_rule_stop(fill, r.atr, self.c)
        p.tags = self._tags(r, now)
        p.last_quote_ts, p.mark = now, fill
        p.fills.append({"ts": now, "side": "buy", "qty": p.qty, "px": fill, "limit": limit, "why": why,
                        "review": res.review})
        self.book.on_open(p)
        self.status[r.symbol] = "filled"
        self.fj.open_position(str(self.day), self.e.mode, p)
        self.fj.set_status(str(self.day), r.symbol, "filled")
        self.e.bus.emit("f_position", now, pos=p.to_dict(), event="open")
        self.books_changed(now)
        self._emit_scan(now)

    def _tags(self, r: F.ScanRow, now: float) -> dict:
        """Observe-only context at entry (research/strategy_f1_prereg.md): logged for later out-of-sample tests,
        never used to gate or size."""
        spy, vwap = getattr(self.e, "price", None), getattr(getattr(self.e, "vwap", None), "value", None)
        return {"gap_pct": r.gap_pct, "or_atr": r.or_atr, "rvol5": r.rvol5,
                "min_after_scan": round((now - F.at_et(self.day, _scan_time(self.c))) / 60, 1),
                "spy_vs_vwap_bp": round((spy / vwap - 1) * 1e4, 1) if spy and vwap else None,
                "catalyst": (r.news or {}).get("catalyst")}

    async def _exit(self, p: F.FPos, why: str, now: float, bar_px: float | None = None) -> None:
        if p.status != "open" or p.exiting:
            return
        p.exiting = True
        try:
            if bar_px is not None:
                px, review = bar_px, None
            else:
                q = self._fresh(p.symbol, now)
                if q is None:
                    await self._poll([p.symbol], now)
                    q = self._fresh(p.symbol, now)
                if q is None:
                    self._log(now, "warn", f"book F1: can't exit {p.symbol} ({why}): no fresh quote")
                    return
                stop = p.stop if why == "stop" else None
                res = None
                for i in range(3):                 # the first limit plus at most 2 reprices
                    limit = round(q.bid * (1 - 0.002 * (i + 1)), 2)
                    res = await self.broker.sell(p.symbol, p.qty, limit, now, stop)
                    self._order_event(now, p.symbol, "sell", p.qty, limit, res, why)
                    if res.filled_qty > 0 or res.status == "rejected":
                        break
                if res is None or res.filled_qty <= 0:
                    self._log(now, "warn", f"book F1: exit {p.symbol} not filled ({why}): {res and (res.message or res.status)}")
                    return
                px, review = res.avg_price, res.review
            p.fills.append({"ts": now, "side": "sell", "qty": p.qty, "px": px, "why": why, "review": review})
            p.status, p.closed_ts, p.exit_px, p.exit_reason = "closed", now, px, why
            self._closed(p, now)
        finally:
            p.exiting = False

    def _closed(self, p: F.FPos, now: float) -> None:
        self.book.on_close(p, p.pnl)
        self.account.on_closed(p.pnl)
        self.fj.close_position(p)
        self.e.journal.record_trade(str(self.day), self.e.mode, TradeRow(p), book=BOOK)
        self.status[p.symbol] = "stopped" if p.exit_reason.startswith("stop") else "closed"
        self.fj.set_status(str(self.day), p.symbol, self.status[p.symbol])
        self.e.bus.emit("f_position", now, pos=p.to_dict(), event="closed")
        self.books_changed(now)
        self._emit_scan(now)
        lim = abs(self.c["daily_loss"])
        if not self.book.halted and self.book.day_pnl <= -lim:
            self.book.halt(f"daily loss -${lim:g} (F only)")
            self._log(now, "warn", f"book F1 halted for the day: P&L ${self.book.day_pnl:.2f}")

    async def _flatten_if_halted(self, now: float) -> None:
        if self.book.halted:
            for p in list(self.book.open):
                await self._exit(p, f"book halted: {self.book.halt_reason}", now)

    # ------------------------------------------------------------ 1-minute bars
    async def _bars(self, syms: list[str], now: float) -> None:
        cur = F.et(now)
        cur_min = cur.hour * 60 + cur.minute
        start = min(self.seen.get(s, 574) for s in syms) + 1      # only bars not processed yet
        got = await self.data.minute_bars(syms, self.day, start, cur_min)
        cutoff = _mins(hhmm(self.c["entry_cutoff_et"]))
        half = self.half_day(now)
        exit_m = _mins(F.exit_time_et(self.c, half))
        for s in syms:
            for b in sorted(got.get(s, []), key=lambda b: b["t"]):
                if b["t"] <= self.seen.get(s, 574) or b["t"] >= cur_min:
                    continue
                self.seen[s] = b["t"]
                for p in [p for p in self.book.open if p.symbol == s]:
                    if b["t"] < _mins(F.et_time(p.opened_ts)):
                        continue
                    if p.shadow_stop is not None and b["l"] <= p.shadow_stop:
                        p.shadow_hit = True
                    px = self.broker.bar_stop(b, p.stop)
                    if px is not None:
                        await self._exit(p, "stop (bar)", now, bar_px=px)
                r = self.armed.get(s)
                if r is not None and b["t"] < cutoff and b["h"] > r.or_high and self._fresh(s, now) is not None:
                    await self._enter(r, now, f"1-minute bar high {b['h']:.2f} above OR high {r.or_high:.2f}")
                sh = self.shadows.get(s)
                if sh is not None and not sh["done"]:
                    self._shadow_bar(sh, b, cutoff, exit_m, now)
        await self._flatten_if_halted(now)
        for s, sh in self.shadows.items():          # shadow shorts still open at the exit time close there
            if sh["pos"] and not sh["done"] and cur_min >= exit_m:
                self._shadow_close(sh, sh["pos"]["last"], f"exit {F.exit_time_et(self.c, half):%H:%M} ET", now)
            elif not sh["pos"] and not sh["done"] and cur_min >= cutoff:
                sh["done"] = True

    def _shadow_bar(self, sh: dict, b: dict, cutoff: int, exit_m: int, now: float) -> None:
        r, pos = sh["row"], sh["pos"]
        if pos is None:
            if b["t"] >= cutoff:
                sh["done"] = True
                return
            e = F.bar_short_entry_fill(b, r.or_low, 1.0)
            if e is None:
                return
            stop = round(e + self.c["stop_atr_frac"] * r.atr, 4)
            qty = max(0, int(min(self.c["risk_per_trade"] / (stop - e), self.c["max_notional"] / e))) if stop > e else 0
            if qty < 1:
                sh["done"] = True
                return
            sh["pos"] = pos = {"entry": e, "stop": stop, "qty": qty, "entry_ts": now, "last": b["c"]}
            if b["h"] >= stop:
                return self._shadow_close(sh, F._up(stop, 1.0), "stop", now)
            return
        pos["last"] = b["c"]
        if b["t"] >= exit_m:
            return self._shadow_close(sh, F._up(b["o"], 1.0), "exit", now)
        px = F.bar_short_stop_fill(b, pos["stop"], 1.0)
        if px is not None:
            self._shadow_close(sh, px, "stop", now)

    def _shadow_close(self, sh: dict, px: float, why: str, now: float) -> None:
        r, pos = sh["row"], sh["pos"]
        pnl = (pos["entry"] - px) * pos["qty"]
        rec = {"symbol": r.symbol, "rvol5": r.rvol5, "rank": r.rank, "or_low": r.or_low, "atr": r.atr,
               "entry_ts": pos["entry_ts"], "entry": pos["entry"], "stop": pos["stop"], "qty": pos["qty"],
               "exit_ts": now, "exit_px": px, "exit_reason": why, "pnl": round(pnl, 2),
               "r": round((pos["entry"] - px) / (pos["stop"] - pos["entry"]), 3)}
        sh["done"] = True
        self.fj.record_shadow(str(self.day), rec)
        self.e.bus.emit("f_shadow", now, short=rec)

    # ------------------------------------------------------------ engine hooks
    def halt_all(self, reason: str, flatten: bool = False) -> None:
        self.account.halt(reason, flatten)

    async def kill(self, now: float) -> None:
        self.book.halt("KILL switch")
        await self.flatten("KILL switch", now)

    async def flatten(self, reason: str, now: float) -> None:
        syms = sorted({p.symbol for p in self.book.open})
        if syms:
            try:
                await self._poll(syms, now)
            except Exception as ex:
                self._log(now, "error", f"book F1: quotes for flatten failed: {ex}")
        for p in list(self.book.open):
            try:
                await self._exit(p, reason, now)
            except Exception as ex:
                log.exception("F1 flatten %s failed", p.symbol)
                self._log(now, "error", f"book F1: flatten {p.symbol} failed: {ex}")

    # ------------------------------------------------------------ events / snapshot
    def _order_event(self, now, sym, side, qty, limit, res, why) -> None:
        self.e.bus.emit("book_order", now, book=BOOK, action=side, symbol=sym, qty=qty, status=res.status,
                        filled=res.filled_qty, price=res.avg_price, limit=limit, why=why, review=res.review)

    def _skip(self, now: float, why: str) -> None:
        b = self.book
        if b.skips and b.skips[-1]["why"] == why:
            return
        b.skips.append({"ts": now, "book": BOOK, "why": why})
        self.e.bus.emit("book_skip", now, book=BOOK, why=why)

    def _log(self, now: float, level: str, msg: str) -> None:
        self.e.bus.emit("log", now, level=level, msg=msg)

    def scan_rows(self) -> list[dict]:
        out = []
        for r in (self.scan.rows if self.scan else []):
            d = r.to_dict()
            d["status"] = self.status.get(r.symbol) or ("shadow short" if r.symbol in self.shadows else "")
            pos = next((p for p in self.book.open + self.book.closed if p.symbol == r.symbol), None)
            d["r"] = pos.r_multiple if pos else None
            out.append(d)
        return sorted(out, key=lambda d: (not d["picked"], d["symbol"] not in self.shadows, -(d["rvol5"] or 0)))

    def _emit_scan(self, now: float) -> None:
        self.e.bus.emit("f_scan", now, rows=self.scan_rows()[:20], book=self.summary())

    def summary(self) -> dict:
        d = self.book.to_dict()
        return {k: v for k, v in d.items() if k not in ("open", "closed")}

    def detail(self) -> dict:
        return {"book": self.book.to_dict(), "scan": self.scan_rows(), "prepared": self.prepared,
                "universe": len(self.uni), "shadows": [s for s, x in self.shadows.items()]}


def _scan_time(c: dict):
    from datetime import time
    h, m = hhmm(c["scan_et"]).hour, hhmm(c["scan_et"]).minute
    return time(h, m, 5)


def _mins(t) -> int:
    return t.hour * 60 + t.minute


def _date(s: str):
    from datetime import date
    return date.fromisoformat(s)


def build_f(engine, cfg, rh=None, mode: str = "paper", provider: str = "sim", feed=None, data=None):
    """FHost for `books.F1_stocks_in_play`, or None when it is off. F1 is paper_only (enforced): paper fills always;
    in shadow/live each order is also sent to review_equity_order, never placed."""
    bc = (cfg.get("books") or {}).get(KEY)
    if not isinstance(bc, dict) or not bc.get("enabled"):
        return None
    if bc.get("paper_only") is not True:
        raise SystemExit(f"books.{KEY}: only a paper path exists for book F1; set paper_only: true")
    if data is None:
        if provider == "sim":
            from ..feeds.f_data import SimEquityData
            data = SimEquityData(feed)
        elif rh is not None:
            from ..feeds.f_data import RobinhoodEquityData
            data = RobinhoodEquityData(rh, bc)
        else:
            log.warning("book F1 needs Robinhood equity data (quotes_source robinhood/auto); F is off this run")
            return None
    host = FHost(engine, cfg, data)
    if mode in ("shadow", "live") and rh is not None:
        from ..brokers.robinhood_equity import RobinhoodEquityBroker
        host.broker = RobinhoodEquityBroker(rh, host.broker, live=False, cfg=cfg)
    return host
