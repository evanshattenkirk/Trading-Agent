"""Book E rules (HANDOFF 7E; spec docs/superpowers/specs/2026-09-29-book-e-design.md section 3). Pure functions.

E1 straddle: at T-3, buy the ATM call + put in the first expiry after the report (4-10 DTE). TP +20%, stop -30%.
E2 calendar: at T-10..T-8, sell the ATM call expiring before the report, buy the same strike after it. TP +15%,
stop -30%. Both exit on the close before the report (T-1; T-0 for after-close reporters) at exit_ct, and E2 leaves
by expiry_day_exit_ct on its short leg's expiry day if that comes first. Never held through the announcement.
"""
from __future__ import annotations

from datetime import date, time

from ..clock import ct_time, session_date
from ..config import hhmm
from ..earnings import previous_trading_day
from ..iv import post_expiry, pre_expiry
from .base import ExitIntent, Leg

E1, E2 = "straddle_t3", "calendar_t10"
SETUP = {E1: "E1 STRADDLE", E2: "E2 CALENDAR"}


def structure_for(flag: str | None) -> str | None:
    return {"E1": E1, "E2": E2}.get(flag or "")


def entry_time(c: dict, half_day: bool) -> time:
    return hhmm(c["half_day_ct"] if half_day else c["entry_ct"])


def close_time(c: dict, half_day: bool) -> time:
    return hhmm(c["half_day_ct"] if half_day else c["exit_ct"])


def exit_day(ev_date: date, timing: str, holidays) -> date:
    return ev_date if timing == "pm" else previous_trading_day(ev_date, set(holidays))


def announced(ev_date: date, timing: str, now: float) -> bool:
    """True once the report may be out: from the 15:00 CT close on D for pm, from D itself otherwise."""
    d = session_date(now)
    if timing == "pm":
        return d > ev_date or (d == ev_date and ct_time(now) >= time(15, 0))
    return d >= ev_date


def pick_expiries(structure: str, exps, today: date, ev_date: date, timing: str, c: dict) -> tuple[dict | None, str]:
    post = post_expiry(exps, ev_date, timing)
    if post is None:
        return None, "no expiry after the report"
    if structure == E1:
        lo, hi = c["e1_dte"]
        dte = (post - today).days
        if not lo <= dte <= hi:
            return None, f"first expiry after the report is {dte} DTE (needs {lo}-{hi})"
        return {"long": post, "short": None}, "ok"
    pre = pre_expiry(exps, ev_date, timing)
    if pre is None or pre <= today:
        return None, "no weekly expiring between today and the report"
    return {"long": post, "short": pre}, "ok"


def legs_for(structure: str, strike: float, today: date, exp: dict) -> list[Leg]:
    k, long_dte = float(strike), (exp["long"] - today).days
    if structure == E1:
        return [Leg("call", k, "buy", dte=long_dte), Leg("put", k, "buy", dte=long_dte)]
    return [Leg("call", k, "sell", dte=(exp["short"] - today).days), Leg("call", k, "buy", dte=long_dte)]


def max_debit(structure: str, c: dict) -> float:
    return float(c["max_debit"] if structure == E1 else c["max_debit_e2"])


def lots_for(debit: float, cap: float, mult: float = 1.0) -> tuple[int, str]:
    per = round(debit * 100, 2)
    if per <= 0:
        return 0, "no debit"
    n = int(cap // per)
    if n < 1:
        return 0, f"1 lot costs ${per:.0f} > ${cap:.0f} max debit"
    if mult < 1.0:
        n = max(1, int(n * mult))
    return n, f"{n} x ${per:.0f} = ${n * per:.0f} (max debit ${cap:.0f})"


def sector_of(sym: str, sectors: dict) -> str | None:
    return next((sec for sec, names in (sectors or {}).items() if sym in (names or [])), None)


def limit_problem(sym: str, open_syms: list, sectors: dict, max_open: int) -> str | None:
    sec = sector_of(sym, sectors)
    if sec is None:
        return f"{sym} has no sector in config"
    if sym in open_syms:
        return f"{sym} already open"
    if len(open_syms) >= max_open:
        return f"max {max_open} E positions open"
    if any(sector_of(s, sectors) == sec for s in open_syms):
        return f"{sec} sector already open"
    return None


def vix_problem(vix: float | None, c: dict) -> str | None:
    if vix is None:
        return "no prior VIX close"                    # as book D does: no VIX, no entry
    return f"VIX {vix:.1f} > {c['vix_max']}" if vix > c["vix_max"] else None


def iv_check(iv: float | None, history: list, c: dict) -> tuple[bool, str, float | None]:
    """Skip when today's earnings-expiry IV ranks above iv_pct_max among earlier cycles at the same T.
    Off until iv_min_cycles earlier cycles exist."""
    need = int(c["iv_min_cycles"])
    if iv is None:
        return True, "IV filter: no IV today", None
    if len(history) < need:
        return True, f"IV filter: {len(history)}/{need} cycles", None
    pct = sum(h <= iv for h in history) / len(history)
    if pct > c["iv_pct_max"]:
        return False, f"IV {iv:.0%} at the {pct:.0%} percentile > {c['iv_pct_max']:.0%}", pct
    return True, f"IV at the {pct:.0%} percentile", pct


def tp_stop(structure: str, entry: float, mid: float, c: dict) -> ExitIntent | None:
    tp = c["take_profit"][structure]
    if mid >= entry * (1 + tp) - 1e-9:
        return ExitIntent(f"take profit +{tp:.0%}")
    if mid <= entry * (1 - c["stop_pct"]) + 1e-9:
        return ExitIntent(f"stop -{c['stop_pct']:.0%}", urgent=True)
    return None


def exit_reason(meta: dict, now: float, c: dict, half_day: bool, holidays) -> tuple[str | None, bool]:
    ev, timing = date.fromisoformat(meta["earnings_date"]), meta.get("timing") or ""
    if announced(ev, timing, now):
        return "held through the report (engine was down at the exit)", True
    d, t, close = session_date(now), ct_time(now), close_time(c, half_day)
    se = meta.get("short_expiry")
    if se:
        se = date.fromisoformat(se)
        if d > se:
            return "short leg expired while the engine was down", True
        if d == se:
            cut = min(hhmm(c["expiry_day_exit_ct"]), close)
            if t >= cut:
                return f"short leg expires today: out by {cut:%H:%M} CT", False
    xd = exit_day(ev, timing, holidays)
    if d > xd or (d == xd and t >= close):
        return f"exit {'T-0' if timing == 'pm' else 'T-1'}: close before the report", False
    return None, False
