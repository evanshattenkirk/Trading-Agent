"""`python -m agentdesk f-report [--since YYYY-MM-DD]`: the paper record of books F1 and F2.

F1 (shares, docs/BOOK_F_HANDOFF.md 4.6): trades, win rate, mean R, PF, t clustered by day, worst day; split by news
tag (priced in), catalyst, RVOL5 bucket and AI/memory list vs the rest; the shadow-shorts summary; and the traded
stop against the published 0.10 x ATR stop on the same trades.
F2 (debit spreads, research/strategy_f2_prereg.md): the same statistics on net P&L / debit paid, split by setup (C
calls, P puts) and by the observe-only skew and IV/realized-vol tags. Under 50 trades is anecdote, and it says so.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

from .f_stocks_in_play import AI_LIST

RVOL_BUCKETS = (("2-3", 0, 3), ("3-5", 3, 5), ("5+", 5, 1e9))


def clustered_t(xs: list[float], groups: list) -> float | None:
    """t of the mean with standard errors clustered by group (a day): trades on one day aren't independent."""
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    g = defaultdict(float)
    for x, k in zip(xs, groups):
        g[k] += x - m
    G = len(g)
    if G < 2:
        return None
    var = G / (G - 1) * sum(v * v for v in g.values()) / (n * n)
    return m / math.sqrt(var) if var > 0 else None


def stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"trades": 0}
    pnl = [r["pnl"] or 0.0 for r in rows]
    win, loss = sum(p for p in pnl if p > 0), -sum(p for p in pnl if p <= 0)
    by_day = defaultdict(float)
    for r in rows:
        by_day[r["session"]] += r["pnl"] or 0.0
    wd = min(by_day, key=by_day.get)
    rs = [r["r"] or 0.0 for r in rows]
    return {"trades": n, "win_rate": sum(1 for p in pnl if p > 0) / n, "mean_r": sum(rs) / n,
            "pf": win / loss if loss > 0 else None, "t": clustered_t(rs, [r["session"] for r in rows]),
            "pnl": round(sum(pnl), 2), "worst_day": {"day": wd, "pnl": round(by_day[wd], 2)}}


# ------------------------------------------------------------------ F2
SKEW_BUCKETS = (("short leg IV < long", 0, 1.0), ("short leg IV >= long", 1.0, 1e9))
IVRV_BUCKETS = (("IV/RV < 1", 0, 1.0), ("IV/RV 1-1.5", 1.0, 1.5), ("IV/RV 1.5+", 1.5, 1e9))


def f2_rows(closed: list[dict]) -> list[dict]:
    """Closed F2 positions (f2_positions JSON) as rows: net P&L and the return on the debit paid."""
    out = []
    for d in closed:
        m = d.get("meta") or {}
        basis = (d.get("entry") or 0) * 100 * (d.get("qty_initial") or 0)
        net = (d.get("realized") or 0) - (d.get("fees") or 0)
        out.append({"session": m.get("entry_day"), "setup": m.get("setup_key"), "symbol": m.get("symbol"),
                    "pnl": round(net, 2), "r": net / basis if basis else 0.0, "skew": m.get("skew"),
                    "iv_rv": m.get("iv_rv"), "exit_reason": d.get("exit_reason")})
    return out


def _bucket(v, buckets) -> str:
    if v is None:
        return "n/a"
    return next((name for name, lo, hi in buckets if lo <= v < hi), "n/a")


def build_f2_report(j2, since: str | None = None) -> dict:
    rows = f2_rows(j2.closed(since))
    split = lambda key: {k: stats(v) for k, v in sorted(_group(rows, key).items())}
    return {"since": since, "all": stats(rows), "by_setup": split(lambda r: {"C": "C calls", "P": "P puts"}.get(r["setup"], "?")),
            "by_skew": split(lambda r: _bucket(r["skew"], SKEW_BUCKETS)),
            "by_iv_rv": split(lambda r: _bucket(r["iv_rv"], IVRV_BUCKETS)),
            "skipped": sum(1 for d in j2.decisions(since=since) if d["outcome"] == "skipped")}


def format_f2_report(r: dict) -> str:
    a = r["all"]
    if not a.get("trades"):
        return ("No closed F2 spreads" + (f" since {r['since']}" if r.get("since") else "") + " yet "
                f"({r['skipped']} candidates skipped; see f2_decisions).")
    out = ["Book F2 (debit spreads), paper; 'mean R' is net P&L / debit paid" + (f", since {r['since']}" if r.get("since") else ""),
           _line("all", a) + ("   (under 50 trades: treat as anecdote)" if a["trades"] < 50 else ""),
           f"  candidates skipped: {r['skipped']}", "", "By setup:"]
    out += [_line(k, s) for k, s in r["by_setup"].items()]
    out += ["", "By skew (observe-only):"] + [_line(k, s) for k, s in r["by_skew"].items()]
    out += ["", "By ATM IV / 20-day realized vol (observe-only):"] + [_line(k, s) for k, s in r["by_iv_rv"].items()]
    return "\n".join(out)


def _news(r: dict) -> dict:
    try:
        return json.loads(r.get("news") or "null") or {}
    except (TypeError, ValueError):
        return {}


def build_report(fj, since: str | None = None) -> dict:
    rows = fj.trades(since)
    split = lambda key: {k: stats(v) for k, v in sorted(_group(rows, key).items())}
    bucket = lambda r: next((name for name, lo, hi in RVOL_BUCKETS if lo <= (r["rvol5"] or 0) < hi), "n/a")
    sh = fj.shadows(since)
    return {"since": since, "all": stats(rows),
            "by_priced_in": split(lambda r: _news(r).get("priced_in") or "unknown"),
            "by_catalyst": split(lambda r: _news(r).get("catalyst") or "unknown"),
            "by_rvol5": split(bucket),
            "ai_list": stats([r for r in rows if r["symbol"] in AI_LIST]),
            "rest": stats([r for r in rows if r["symbol"] not in AI_LIST]),
            "shadow_shorts": stats([{**s, "r": s["r"]} for s in sh]),
            "published_stop": _published_stop(rows)}


def _published_stop(rows) -> dict:
    """F1 v2 trades carry the P&L they would have had with the published 0.10 x ATR stop (never traded)."""
    both = [r for r in rows if r.get("shadow_pnl") is not None]
    return {"trades": len(both), "pnl": round(sum(r["pnl"] or 0 for r in both), 2),
            "shadow_pnl": round(sum(r["shadow_pnl"] for r in both), 2),
            "shadow_stopped": sum(1 for r in both if r.get("shadow_hit"))}


def _group(rows, key) -> dict:
    out = defaultdict(list)
    for r in rows:
        out[key(r)].append(r)
    return out


def _line(label: str, s: dict) -> str:
    if not s.get("trades"):
        return f"  {label:22s} no trades"
    f = lambda x, p=2: "n/a" if x is None else f"{x:.{p}f}"
    return (f"  {label:22s} {s['trades']} trades  win {100 * s['win_rate']:5.1f}%  mean R {f(s['mean_r'])}  "
            f"PF {f(s['pf'])}  t {f(s['t'])}  P&L ${s['pnl']:+.2f}")


def format_report(r: dict) -> str:
    a = r["all"]
    if not a.get("trades"):
        return "No closed F trades" + (f" since {r['since']}" if r.get("since") else "") + " yet."
    out = [f"Book F1 (stocks in play, shares), paper" + (f", since {r['since']}" if r.get("since") else ""),
           _line("all", a), f"  worst day {a['worst_day']['day']} ${a['worst_day']['pnl']:+.2f}"
           + ("   (under 50 trades: treat as anecdote)" if a["trades"] < 50 else ""), "", "By news tag (priced in):"]
    out += [_line(k, s) for k, s in r["by_priced_in"].items()]
    out += ["", "By catalyst:"] + [_line(k, s) for k, s in r["by_catalyst"].items()]
    out += ["", "By RVOL5:"] + [_line(k, s) for k, s in r["by_rvol5"].items()]
    out += ["", "AI/memory list vs the rest:", _line("AI list", r["ai_list"]), _line("rest", r["rest"])]
    out += ["", "Shadow shorts (red first candle, never traded):", _line("would-be shorts", r["shadow_shorts"])]
    ps = r.get("published_stop") or {}
    if ps.get("trades"):
        out += ["", "Stop comparison (same trades):",
                f"  opening-range-low stop (traded)  P&L ${ps['pnl']:+.2f}",
                f"  published 0.10 x ATR stop (shadow) P&L ${ps['shadow_pnl']:+.2f}, "
                f"stopped {ps['shadow_stopped']} of {ps['trades']}"]
    return "\n".join(out)
