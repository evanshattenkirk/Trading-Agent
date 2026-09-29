"""End-of-day ATM IV for book E's universe -> journal.iv_history (HANDOFF 7E "Data"; spec section 4).

For each name: the ATM call and put (nearest listed strike to spot) in up to four expiries: front (next after today),
pre and earn (the last expiry before and the first after the next report, when one is within the calendar's 31
days) and d30 (closest to 30 days). One get_option_quotes call per name. Read-only; runs inside the standalone
recorder (IVDay, below), or once by hand with `python -m agentdesk iv-snapshot`.
"""
from __future__ import annotations

import logging
import time
from datetime import date

from .clock import ct, session_date
from .config import hhmm
from .earnings import trading_days_between
from .iv import atm_strike, d30_expiry, front_expiry, post_expiry, pre_expiry

log = logging.getLogger("agentdesk.iv_recorder")
KINDS = ("front", "pre", "earn", "d30")
DEFAULTS = {"enabled": True, "list_from_ct": "13:30", "quote_ct": "14:50", "stop_ct": "15:00",
            "list_calls_per_s": 0.5, "quote_calls_per_s": 1.0, "cache_dir": "~/.agentdesk/cache/iv", "retry_sec": 30}


def settings(cfg) -> dict:
    return {**DEFAULTS, **((cfg.get("recorder") or {}).get("iv") or {})}


def next_events(calendar, today: date, universe) -> dict[str, dict]:
    """Each universe name's next report that isn't out yet: after today, or today after the close (pm)."""
    names, out = {s.upper() for s in universe}, {}
    for r in sorted(calendar, key=lambda r: r["date"]):
        s = r["symbol"]
        if s in names and s not in out and (r["date"] > today or (r["date"] == today and r["timing"] == "pm")):
            out[s] = r
    return out


def expiries_for(exps, today: date, ev: dict | None) -> dict[str, date]:
    fut = [e for e in exps if e > today]
    out = {"front": front_expiry(fut, today), "d30": d30_expiry(fut, today)}
    if ev:
        out["pre"] = pre_expiry(fut, ev["date"], ev["timing"])
        out["earn"] = post_expiry(fut, ev["date"], ev["timing"])
    return {k: v for k, v in out.items() if v}


class IVSnapshot:
    def __init__(self, chains, journal, universe, holidays):
        self.chains, self.journal, self.holidays = chains, journal, set(holidays)
        self.universe = [s.upper() for s in universe]

    async def warm(self, today: date, calendar) -> list[str]:
        """List the strikes of every expiry the quote pass will need, so it only quotes. Returns failed names."""
        evs, failed = next_events(calendar, today, self.universe), []
        for sym in self.universe:
            try:
                exps = await self.chains.expirations(sym)
                for exp in expiries_for(exps, today, evs.get(sym)).values():
                    await self.chains.strikes(sym, exp)
            except Exception as ex:
                log.warning("iv warm-up %s: %s", sym, ex)
                failed.append(sym)
        return failed

    async def snapshot(self, now: float, calendar, symbols=None) -> tuple[int, list[str]]:
        today = session_date(now)
        syms = [s.upper() for s in (symbols or self.universe)]
        evs = next_events(calendar, today, syms)
        spots = await self.chains.spots(syms)
        rows, failed = [], []
        for sym in syms:
            try:
                rows += await self._rows(sym, spots.get(sym), today, now, evs.get(sym))
            except Exception as ex:
                log.warning("iv snapshot %s: %s", sym, ex)
                failed.append(sym)
        self.journal.record_iv(rows)
        return len(rows), failed

    async def _rows(self, sym: str, spot, today: date, now: float, ev) -> list[dict]:
        if not spot:
            raise ValueError("no spot price")
        want = expiries_for(await self.chains.expirations(sym), today, ev)
        picks = {}
        for kind, exp in want.items():
            k = atm_strike([await self.chains.strikes(sym, exp)], spot)
            if k is not None:
                picks[kind] = (exp, k)
        if not picks:
            raise ValueError("no listed ATM strikes")
        cs = {}
        for exp, k in picks.values():
            for r in ("call", "put"):
                cs.setdefault((exp, k, r), self.chains.contract(sym, exp, k, r))
        keys = list(cs)
        qs = dict(zip(keys, await self.chains.quotes([cs[x] for x in keys])))
        T = trading_days_between(today, ev["date"], self.holidays) if ev else None
        out = []
        for kind, (exp, k) in picks.items():
            c, p = qs.get((exp, k, "call")), qs.get((exp, k, "put"))
            if c is None and p is None:
                continue
            ivs = [q.iv for q in (c, p) if q is not None and q.iv]
            out.append({"day": str(today), "ts": now, "symbol": sym, "kind": kind, "expiry": str(exp),
                        "dte": (exp - today).days, "strike": k, "spot": spot,
                        "call_bid": c.bid if c else None, "call_ask": c.ask if c else None, "call_iv": c.iv if c else None,
                        "put_bid": p.bid if p else None, "put_ask": p.ask if p else None, "put_iv": p.iv if p else None,
                        "atm_iv": sum(ivs) / len(ivs) if ivs else None,
                        "earnings_date": str(ev["date"]) if ev else None, "earnings_timing": ev["timing"] if ev else None,
                        "T": T})
        return out


def phase(now: float, s: dict) -> str:
    d = ct(now)
    if d.weekday() >= 5:
        return "closed"
    t = d.time()
    if t < hhmm(s["list_from_ct"]):
        return "wait"
    if t < hhmm(s["quote_ct"]):
        return "list"
    if t < hhmm(s["stop_ct"]):
        return "quote"
    return "closed"


class IVDay:
    """One weekday's IV pass. The recorder calls step(now) every few seconds: from list_from_ct it lists strikes once
    (list pacer), from quote_ct it records every name (quote pacer), retries failed names every retry_sec until
    stop_ct, then logs the day's summary once."""

    def __init__(self, snap, calendar_fn, s: dict, list_pacer=None, quote_pacer=None, log_fn=None):
        self.snap, self.calendar_fn, self.s = snap, calendar_fn, s
        self.list_pacer, self.quote_pacer = list_pacer, quote_pacer
        self.log_fn = log_fn or (lambda summary: None)
        self.day = None
        self._reset(None)

    def _reset(self, d) -> None:
        self.day, self.listed, self.left, self.done, self.rows = d, False, None, False, 0
        self._cal, self._last_try = None, -1e18

    async def _calendar(self, today):
        if self._cal is None:
            self._cal = await self.calendar_fn(today)
        return self._cal

    async def step(self, now: float) -> str:
        today = session_date(now)
        if today != self.day:
            self._reset(today)
        ph = phase(now, self.s)
        if self.done:
            return ph
        if ph == "list" and not self.listed:
            self.snap.chains.pacer = self.list_pacer
            await self.snap.warm(today, await self._calendar(today))
            self.listed = True
        elif ph == "quote" and now - self._last_try >= float(self.s["retry_sec"]):
            self._last_try = now
            self.snap.chains.pacer = self.quote_pacer
            n, failed = await self.snap.snapshot(now, await self._calendar(today), self.left)
            self.rows += n
            self.left = failed
            if not failed:
                self._finish(today)
        elif ph == "closed" and self.left:
            self._finish(today)
        return ph

    def _finish(self, today) -> None:
        self.done = True
        self.log_fn({"iv_day": str(today), "rows": self.rows, "failed": list(self.left or [])})


async def recorder_calendar(rh, cfg, today: date) -> list[dict]:
    """The 31-day earnings calendar (the Earnings desk's arguments); config's list if Robinhood fails."""
    from .earnings import parse_calendar
    try:
        return parse_calendar(await rh.call("get_earnings_calendar", {
            "start_date": str(today), "days": 31, "filter": "high_market_cap"}))
    except Exception as ex:
        log.warning("iv: earnings calendar unavailable (%s); using config", ex)
        return parse_calendar(((cfg.get("crew") or {}).get("earnings") or {}).get("calendar") or [])


async def run_once(cfg) -> str:
    """`python -m agentdesk iv-snapshot`: list and record every name now, with the recorder's read-only grant."""
    from . import recorder
    from .config import expand
    from .desks import holidays
    from .iv import Pacer, RobinhoodChains
    from .journal import Journal
    s, ivs = recorder.settings(cfg), settings(cfg)
    db = expand(cfg["journal_path"])
    meter = recorder.CallMeter(db, tag="iv")
    rh = recorder.MeteredRobinhoodMCP(recorder.recorder_cfg(cfg, s), meter)
    await rh.start()
    try:
        chains = RobinhoodChains(rh, ivs["cache_dir"], Pacer(float(ivs["quote_calls_per_s"])))
        snap = IVSnapshot(chains, Journal(db), ((cfg.get("crew") or {}).get("earnings") or {}).get("universe") or [],
                          holidays(cfg))
        now = time.time()
        today = session_date(now)
        cal = await recorder_calendar(rh, cfg, today)
        await snap.warm(today, cal)
        n, failed = await snap.snapshot(now, cal)
        return f"{n} iv_history rows for {today}; failed: {', '.join(failed) or 'none'}"
    finally:
        meter.flush()
        try:
            await rh.close()
        except BaseException:
            pass
