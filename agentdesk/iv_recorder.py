"""End-of-day ATM IV for book E's universe -> journal.iv_history (HANDOFF 7E "Data"; spec section 4).

For each name: the ATM call and put (nearest listed strike to spot) in up to four expiries: front (next after today),
pre and earn (the last expiry before and the first after the next report, when one is within the calendar's 31
days) and d30 (closest to 30 days). One get_option_quotes call per name. Read-only; runs inside the standalone
recorder (IVDay, below), or once by hand with `python -m agentdesk iv-snapshot`.
"""
from __future__ import annotations

import logging
from datetime import date

from .clock import session_date
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
