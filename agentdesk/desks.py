"""The restrict-only desks: Ops (pre-flight), Earnings (book E's screen) and Post-mortem (rule audit).

They add information and can only cut size. crew.py enforces that for every desk in RESTRICT_ONLY: votes are
clamped to <= 1.0, proposals are dropped and they can't add event blackouts. Each brief here is deterministic;
the only outside call is Robinhood's read-only get_earnings_calendar.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path

from .clock import at_ct, ct, ct_time, session_date
from .config import expand, hhmm
from .earnings import heavyweights_overnight, parse_calendar, previous_trading_day, screen

log = logging.getLogger("agentdesk.desks")

RESTRICT_ONLY = ("ops", "earnings", "postmortem")
INFO_ONLY = ("rates", "fed")        # folded into Macro (crew review, 2026-09-29): derived briefs, vote fixed at 1.0
WATCHDOG_KEYS = ("quote_stale_sec", "max_consecutive_errors", "reconcile_sec")
FLAG_TEXT = {"E2": "E2 calendar window", "E1": "E1 straddle at the close", "exit": "exit at the close"}


def holidays(cfg) -> set:
    from .earnings import _day
    return {d for d in (_day(x) for x in (cfg.get("calendar") or {}).get("holidays") or []) if d}


# ----------------------------------------------------------------------------- Ops
def ops_checks(crew, slot: str) -> list[dict]:
    e, cfg = crew.e, crew.e.cfg
    sim = e.feed.is_sim
    oc = crew.cfg.get("ops") or {}
    out = []

    def add(name, ok, detail=""):
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    mode = getattr(e, "mode", "sim")
    add("Mode", mode != "live" or cfg.get("live_enabled"), f"{mode}, live_enabled {bool(cfg.get('live_enabled'))}")
    # a book is a books.* entry with "enabled" (books.account, books.fills etc. are settings, not books)
    live_books = [k for k, b in (cfg.get("books") or {}).items()
                  if isinstance(b, dict) and "enabled" in b and b.get("paper_only") is not True]
    add("Books paper-only", not live_books, ", ".join(live_books) or "all paper")
    r = cfg["risk"]
    wd = r.get("watchdog") or {}
    armed = r.get("max_daily_loss", 0) > 0 and r.get("max_trades_per_day", 0) > 0 and all(k in wd for k in WATCHDOG_KEYS)
    add("Risk limits armed", armed, f"loss -${r.get('max_daily_loss')}, {r.get('max_trades_per_day')} trades, watchdog "
        + ("on" if all(k in wd for k in WATCHDOG_KEYS) else "missing keys"))
    st = e.risk.st
    add("Not halted", not st.halted, st.halt_reason or "")
    sig = getattr(e, "sig", None)
    if sig is not None:
        need = int(oc.get("min_warm_bars", 50))
        cold = [f"{tf} {sig.tf[tf].bars} bars" for tf in ("15m", "5m")
                if tf in sig.tf and (sig.tf[tf].last is None or sig.tf[tf].bars < need)]
        add("Signal history warm", not cold, ", ".join(cold) or f"15m/5m have {need}+ bars")
    add("SPY price", e.price is not None, f"{e.price}" if e.price is not None else "no print yet")
    if not sim:
        add("Robinhood", getattr(e, "l2_rh", None) is not None, "option quotes, Level 2, calendar")
        add("Recorder", *_recorder_check(crew))
    return out


def _recorder_check(crew) -> tuple[bool, str]:
    """Did the previous session's 0DTE quotes land? The standalone recorder (launchd) writes journal.option_quotes and
    a line per day to its day log; either counts."""
    e = crew.e
    hol = holidays(e.cfg)
    prev = previous_trading_day(session_date(e.feed.now()), hol)
    rows = 0
    p = Path(expand((crew.cfg.get("ops") or {}).get("recorder_days_log", "~/.agentdesk/recorder/days.jsonl")))
    try:
        for line in p.read_text().splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("day") == str(prev):
                rows = max(rows, int(d.get("rows") or 0))
    except OSError:
        pass
    db = getattr(e.journal, "db", None)
    if db is not None:
        lo = at_ct(prev, hhmm("00:00"))
        try:
            n = db.execute("SELECT COUNT(*) FROM option_quotes WHERE ts>=? AND ts<?", (lo, lo + 86400)).fetchone()[0]
            rows = max(rows, int(n))
        except Exception:
            pass
    return rows > 0, f"{prev}: {rows} quote rows"


def ops_brief(crew, slot: str) -> dict:
    checks = ops_checks(crew, slot)
    bad = [c for c in checks if not c["ok"]]
    cold = any(c["name"] == "Signal history warm" for c in bad)
    if not bad:
        head = f"Pre-flight {len(checks)}/{len(checks)} green."
    else:
        head = f"Pre-flight: {len(bad)} red ({', '.join(c['name'] for c in bad)})."
    if cold:
        head = (head + " 50% until history loads.")[:90]
    if slot == "halt":
        head = f"After the halt: {len(checks) - len(bad)}/{len(checks)} checks green."
    notes = [f"{c['name']}: {c['detail']}" for c in bad][:4]
    return {"headline": head, "bias": "neutral", "confidence": 1.0, "events": [],
            "size_multiplier": 0.5 if cold else 1.0, "notes": notes, "checks": checks}


# ----------------------------------------------------------------------------- Earnings
def _ec(crew) -> dict:
    return crew.cfg.get("earnings") or {}


def _sim_calendar(crew, today, hol) -> list[dict]:
    """Two seeded names so the simulator shows an E1 and an E2 flag."""
    uni = list(_ec(crew).get("universe") or ["AAPL", "MSFT"])
    out = []
    for sym, t in ((uni[0], 3), (uni[1 % len(uni)], 9)):
        d, n = today, 0
        while n < t:
            d += timedelta(days=1)
            n += d.weekday() < 5 and d not in hol
        out.append({"symbol": sym, "date": d, "timing": "pm", "verified": True})
    return out


async def load_calendar(crew, today) -> tuple[list[dict], str]:
    cache = getattr(crew, "_earnings_cal", None)
    if cache and cache[0] == today:
        return cache[1], cache[2]
    rh = getattr(crew.e, "l2_rh", None)
    cal, src = None, ""
    if rh is not None:
        try:
            data = await asyncio.wait_for(rh.call("get_earnings_calendar", {
                "start_date": str(today), "days": 31, "filter": "high_market_cap"}), timeout=15)
            cal, src = parse_calendar(data), "Robinhood calendar"
            if not cal:             # a reply with no rows is a broken read, not a quiet month (review M6)
                log.warning("earnings calendar: Robinhood replied with no rows")
                src = "Robinhood calendar empty"
        except Exception as ex:
            log.warning("earnings calendar failed: %s", ex)
            src = f"Robinhood calendar unavailable ({str(ex)[:60]}); using config"
    if cal is None:
        cal = parse_calendar(_ec(crew).get("calendar") or [])
        if not cal and crew.e.feed.is_sim:
            cal, src = _sim_calendar(crew, today, holidays(crew.e.cfg)), "sim calendar"
        src = src or "config calendar"
    if not src.startswith(("Robinhood calendar unavailable", "Robinhood calendar empty")):     # those are read again
        crew._earnings_cal = (today, cal, src)
    return cal, src


def _ser(r: dict) -> dict:
    return {**r, "date": str(r["date"])}


async def earnings_brief(crew, now: float) -> dict:
    today = session_date(now)
    hol = holidays(crew.e.cfg)
    cal, src = await load_calendar(crew, today)
    ec = _ec(crew)
    rows = screen(cal, today, ec.get("universe") or [], hol)
    hw = heavyweights_overnight(cal, today, ec.get("heavyweights") or [], hol)
    flagged = [r for r in rows if r["flag"]]
    if flagged:
        head = "E screen: " + "; ".join(f"{r['symbol']} {r['flag']}" + (" (tentative)" if r["tentative"] else "")
                                         for r in flagged)
    else:
        head = "E screen: nothing in a window today."
    notes = [f"{r['symbol']} reported {'last night' if r['timing'] == 'pm' else 'this morning'}; SPY open may gap."
             for r in hw][:2]
    notes += [f"{r['symbol']} T-{r['T']} ({r['timing'] or 'time n/a'}): {FLAG_TEXT[r['flag']]}" for r in flagged][:2]
    nxt = [r for r in rows if not r["flag"]][:2]
    if nxt:
        notes.append("Next: " + ", ".join(f"{r['symbol']} T-{r['T']}" for r in nxt))
    notes.append(f"Source: {src}")
    return {"headline": head[:90], "bias": "neutral", "confidence": 0.5, "events": [], "size_multiplier": 1.0,
            "notes": notes[:4], "screen": [_ser(r) for r in rows], "heavyweights": [_ser(r) for r in hw]}


# ----------------------------------------------------------------------------- Post-mortem
def _audit(crew, p) -> list[dict]:
    cfg, risk = crew.e.cfg, crew.e.risk
    out = []
    early = risk.early_close(p.opened_ts)
    win = cfg["strategy"]["entry_window"]
    t_open = ct_time(p.opened_ts)
    if not (hhmm(win["start"]) <= t_open <= hhmm(min(win["end"], "11:20") if early else win["end"])):
        out.append({"rule": "entry window", "why": f"opened {t_open.strftime('%H:%M')} outside {win['start']}-{win['end']}"})
    bo = next((b for b in risk.st.blackouts if b.start <= p.opened_ts < b.end
               and (b.added_ts is None or b.added_ts <= p.opened_ts)), None)     # not one learned of after the entry
    if bo:
        out.append({"rule": "blackout", "why": f"opened inside the {bo.name} blackout"})
    cap = int(cfg["sizing"]["max_contracts"] * crew.cfg.get("max_size_multiplier", 1.25))
    if p.qty_initial > cap:
        out.append({"rule": "contracts", "why": f"{p.qty_initial} contracts, cap {cap}"})
    flat = at_ct(session_date(p.opened_ts), hhmm("11:40" if early else cfg["exits"]["flatten_at"]))
    if p.closed_ts and p.closed_ts > flat + 60:
        out.append({"rule": "flatten", "why": f"closed {ct(p.closed_ts).strftime('%H:%M')}, after the flatten time"})
    cost = p.entry * 100 * p.qty_initial
    pct = (p.realized - p.fees) / cost if cost else 0.0
    stop = cfg["exits"]["stop_loss_pct"]
    if pct < -(stop + 0.10):
        out.append({"rule": "stop", "why": f"lost {pct * 100:.0f}%, stop is -{stop * 100:.0f}%"})
    return out


def postmortem_brief(crew, now: float) -> dict:
    e = crew.e
    trades = [p for p in e.closed if p.closed_ts]
    rows, breaks, reviews = [], [], []
    for p in trades:
        cost = p.entry * 100 * p.qty_initial
        pct = (p.realized - p.fees) / cost if cost else 0.0
        bad = _audit(crew, p)
        breaks += [{**b, "trade": p.contract.label} for b in bad]
        if p.peak >= p.entry * 1.5 and pct < 0:
            reviews.append({"trade": p.contract.label,
                            "why": f"round trip: peaked +{(p.peak / p.entry - 1) * 100:.0f}%, closed {pct * 100:.0f}%"})
        rows.append((p, pct, bad))
    if not trades:
        head = "No trades today. Nothing to audit."
    elif breaks:
        head = f"{len(breaks)} rule break(s) in {len(trades)} trades. Check the engine: {breaks[0]['why']}"
    else:
        head = f"All {len(trades)} trades followed the rules."
    skips: dict = {}
    for s in getattr(e, "skips", []) or []:
        if session_date(s.get("ts", now)) == session_date(now):
            k = str(s.get("why", "")).split(" (")[0][:40]
            skips[k] = skips.get(k, 0) + 1
    notes = [f"{b['trade']}: {b['why']}" for b in breaks][:2] + [f"{r['trade']}: {r['why']}" for r in reviews][:1]
    if skips:
        notes.append("Skipped: " + ", ".join(f"{k} x{n}" for k, n in sorted(skips.items(), key=lambda x: -x[1])[:3]))
    notes.append(f"Size today {int(e.risk.st.size_mult * 100)}%")
    b = {"headline": head[:90], "bias": "neutral", "confidence": 1.0, "events": [], "size_multiplier": 1.0,
         "notes": notes[:4], "breaks": breaks, "reviews": reviews, "skips": skips, "trades": len(trades)}
    if not e.feed.is_sim:
        _write_report(crew, session_date(now), rows, b)
    return b


def _write_report(crew, day, rows, b) -> None:
    d = Path(expand(crew.cfg.get("postmortem_dir", "~/.agentdesk/postmortems")))
    try:
        d.mkdir(parents=True, exist_ok=True)
        lines = [f"# Post-mortem {day}", "", b["headline"], "",
                 "| Contract | Setup | Qty | Open | Close | P&L % | Peak % | Exit | Rule breaks |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for p, pct, bad in rows:
            lines.append(f"| {p.contract.label} | {p.setup} | {p.qty_initial} | {ct(p.opened_ts).strftime('%H:%M')} | "
                         f"{ct(p.closed_ts).strftime('%H:%M')} | {pct * 100:+.0f}% | {(p.peak / p.entry - 1) * 100:+.0f}% | "
                         f"{p.exit_reason or ''} | {'; '.join(x['why'] for x in bad) or 'none'} |")
        if b["reviews"]:
            lines += ["", "Review:"] + [f"- {r['trade']}: {r['why']}" for r in b["reviews"]]
        if b["skips"]:
            lines += ["", "Skipped entries:"] + [f"- {k}: {n}" for k, n in b["skips"].items()]
        (d / f"{day}.md").write_text("\n".join(lines) + "\n")
    except OSError as ex:
        log.warning("post-mortem write failed: %s", ex)


# ----------------------------------------------------------------------------- roundtable
def templated_lines(slot: str, b: dict) -> list[dict]:
    lines = []
    if "ops" in b:
        bad = [c for c in b["ops"].get("checks", []) if not c["ok"]]
        to = "risk" if "risk" in b else "agent"
        lines.append({"from": "ops", "to": to, "text": "Nothing to fix before the bell. Limits and watchdog are armed." if not bad
                      else f"Heads up: {', '.join(c['name'] for c in bad)[:80]}. Worth a look."})
    if "earnings" in b:
        fl = [r for r in b["earnings"].get("screen", []) if r.get("flag")]
        lines.append({"from": "earnings", "to": "vol",
                      "text": ("For book E: " + ", ".join(f"{r['symbol']} {r['flag']}" for r in fl[:3])
                               + ". Can you check their IV?") if fl else "Nothing in book E's windows today."})
    if "postmortem" in b:
        to = "quant" if "quant" in b else "agent"
        pm = b["postmortem"]
        n = len(pm.get("breaks", []))
        lines.append({"from": "postmortem", "to": to,
                      "text": f"{n} rule break(s) today. That's an engine problem, not a market one." if n
                      else "Every trade followed the rules." if pm.get("trades") else "No trades to audit today."})
    return lines
