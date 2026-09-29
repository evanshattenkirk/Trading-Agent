"""`python -m agentdesk f-report [--since YYYY-MM-DD]`: book F's paper record (docs/BOOK_F_HANDOFF.md 4.6).

Trades, win rate, mean R, PF, t clustered by day, worst day; split by news tag (priced in), catalyst, RVOL5 bucket
and AI/memory list vs the rest; and the shadow-shorts summary. Under 50 trades is anecdote, and it says so.
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
            "shadow_shorts": stats([{**s, "r": s["r"]} for s in sh])}


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
    return "\n".join(out)
