"""R1: retailer search interest before earnings (rules frozen in research/r1_retail_search_prereg.md).

    python research/r1_retail_search.py \
        [--earnings research/data/x1/retail_earnings.csv] [--prices research/data/x1/retail_daily.csv] \
        [--trends research/data/retail/trends.csv]
    # -> research/r1_retail_search_results.json and research/r1_retail_search.md

Inputs come from research/fetch_retail_mac.py (Mac: SEC report times, Alpaca daily closes) and
research/fetch_retail_trends.py (Google Trends). Pure functions are tested in research/tests/test_r1_retail_search.py.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "research" / "data"
RETAIL = ["WMT", "COST", "HD", "LOW", "TGT", "BBY", "DG", "DLTR", "TJX", "ROST",
          "KSS", "M", "ULTA", "TSCO", "DKS", "BJ", "BURL", "AZO", "ORLY"]
LAG_DAYS = 35          # M = month containing R - 35 days
PRIOR = 4              # surprise = g minus the mean of the name's 4 previous reports' g
COST_BP = 5.0          # round trip
SAMPLE = (date(2016, 1, 15), date(2026, 10, 31))
T_BAR = 2.33


# ------------------------------------------------------------------ pure parts
def shift_month(ym: tuple[int, int], k: int) -> tuple[int, int]:
    y, m = ym
    i = y * 12 + (m - 1) + k
    return i // 12, i % 12 + 1


def signal_month(r: date) -> tuple[int, int]:
    d = r - timedelta(days=LAG_DAYS)
    return d.year, d.month


def growth(series: dict[tuple[int, int], float], r: date) -> float | None:
    """g = ln(mean of months M-2..M / mean of the same months a year earlier); None if a month is missing or 0."""
    m = signal_month(r)
    cur = [series.get(shift_month(m, -k)) for k in (2, 1, 0)]
    base = [series.get(shift_month(m, -12 - k)) for k in (2, 1, 0)]
    if any(v is None for v in cur + base):
        return None
    c, b = sum(cur) / 3, sum(base) / 3
    if c <= 0 or b <= 0:
        return None
    return math.log(c / b)


def surprises(reports: list[date], g: dict[date, float | None]) -> dict[date, float | None]:
    """s(R) = g(R) - mean g of the 4 kept reports just before R; None unless all five have a g."""
    out = {}
    rs = sorted(reports)
    for i, r in enumerate(rs):
        prev = rs[max(0, i - PRIOR):i]
        vals = [g.get(p) for p in prev]
        if g.get(r) is None or len(prev) < PRIOR or any(v is None for v in vals):
            out[r] = None
        else:
            out[r] = g[r] - sum(vals) / PRIOR
    return out


def window_return(closes: dict[tuple[str, date], float], sym: str, start: date, end: date) -> float | None:
    """Stock minus SPY close-to-close return from `start` to `end`; None if a close is missing."""
    keys = [(sym, start), (sym, end), ("SPY", start), ("SPY", end)]
    if any(k not in closes for k in keys):
        return None
    a, b, sa, sb = (closes[k] for k in keys)
    return (b / a - 1) - (sb / sa - 1)


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def clustered_mean(x: list[float], groups: list[str]) -> dict:
    """Mean and its cluster-robust t (clusters = groups), with the small-sample G/(G-1) factor."""
    n = len(x)
    if n < 2:
        return {"n": n, "mean": (x[0] if x else None), "t": None, "clusters": len(set(groups))}
    mu = sum(x) / n
    sums = defaultdict(float)
    for v, g in zip(x, groups):
        sums[g] += v - mu
    G = len(sums)
    var = sum(s * s for s in sums.values()) / n ** 2 * (G / (G - 1) if G > 1 else 1.0)
    return {"n": n, "mean": mu, "t": mu / math.sqrt(var) if var > 0 else None, "clusters": G}


def clustered_slope(y: list[float], x: list[float], groups: list[str]) -> dict:
    """OLS y = a + b x with a cluster-robust t on b."""
    n = len(y)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    b = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / sxx
    a = my - b * mx
    sums = defaultdict(float)
    for xi, yi, g in zip(x, y, groups):
        sums[g] += (xi - mx) * (yi - a - b * xi)
    G = len(sums)
    var = sum(s * s for s in sums.values()) / sxx ** 2 * (G / (G - 1) if G > 1 else 1.0)
    return {"n": n, "slope": b, "t": b / math.sqrt(var) if var > 0 else None, "clusters": G}


def zscore(x: list[float]) -> list[float]:
    mu = sum(x) / len(x)
    sd = math.sqrt(sum((v - mu) ** 2 for v in x) / (len(x) - 1))
    return [(v - mu) / sd for v in x]


def next_session(sessions: list[date], d: date) -> date | None:
    for s in sessions:
        if s > d:
            return s
    return None


def build_trades(events: list[dict], trends: dict[str, dict], closes: dict, sessions: list[date],
                 sample=SAMPLE, cost_bp: float = COST_BP) -> tuple[list[dict], dict]:
    """events: kept reports {symbol, report_session, entry_session}. Returns (trades, skip counts)."""
    skips = defaultdict(int)
    by_sym = defaultdict(list)
    for e in events:
        by_sym[e["symbol"]].append(e)
    trades = []
    for sym, evs in sorted(by_sym.items()):
        series = trends.get(sym, {})
        g = {e["report_session"]: growth(series, e["report_session"]) for e in evs}
        s = surprises([e["report_session"] for e in evs], g)
        for e in sorted(evs, key=lambda e: e["report_session"]):
            r = e["report_session"]
            if not (sample[0] <= r <= sample[1]):
                continue
            if g[r] is None:
                skips["no_search_value"] += 1
                continue
            if s[r] is None:
                skips["fewer_than_4_prior"] += 1
                continue
            if s[r] == 0:
                skips["zero_surprise"] += 1
                continue
            ab = window_return(closes, sym, e["entry_session"], r)
            if ab is None:
                skips["no_prices"] += 1
                continue
            sign = 1 if s[r] > 0 else -1
            nx = next_session(sessions, r)
            ab3 = window_return(closes, sym, e["entry_session"], nx) if nx else None
            trades.append({"symbol": sym, "R": r, "entry": e["entry_session"], "g": g[r], "s": s[r], "side": sign,
                           "abnormal": ab, "gross": sign * ab, "net": sign * ab - cost_bp / 1e4,
                           "abnormal_3d": ab3})
    return trades, dict(skips)


def evaluate(trades: list[dict], t_bar: float = T_BAR) -> dict:
    trades = sorted(trades, key=lambda t: t["R"])
    wk = [iso_week(t["R"]) for t in trades]
    net = clustered_mean([t["net"] for t in trades], wk)
    gross = clustered_mean([t["gross"] for t in trades], wk)
    mid = trades[len(trades) // 2]["R"]
    first = [t for t in trades if t["R"] < mid]
    second = [t for t in trades if t["R"] >= mid]
    halves = [clustered_mean([t["net"] for t in h], [iso_week(t["R"]) for t in h]) for h in (first, second)]
    passed = bool(net["t"] is not None and net["t"] >= t_bar and net["mean"] > 0
                  and all(h["mean"] is not None and h["mean"] > 0 for h in halves))
    z = zscore([t["s"] for t in trades])
    slope = clustered_slope([t["abnormal"] for t in trades], z, wk)
    three = [(t, w) for t, w in zip(trades, wk) if t["abnormal_3d"] is not None]
    slope3 = clustered_slope([t["abnormal_3d"] for t, _ in three], zscore([t["s"] for t, _ in three]),
                             [w for _, w in three])
    side = {k: clustered_mean([t["net"] for t in trades if t["side"] == v],
                              [iso_week(t["R"]) for t in trades if t["side"] == v]) for k, v in (("long", 1), ("short", -1))}
    per_name = {}
    for sym in sorted({t["symbol"] for t in trades}):
        xs = [t["net"] for t in trades if t["symbol"] == sym]
        per_name[sym] = {"n": len(xs), "mean_net": sum(xs) / len(xs), "hit": sum(x > 0 for x in xs) / len(xs)}
    per_year = {}
    for y in sorted({t["R"].year for t in trades}):
        xs = [t["net"] for t in trades if t["R"].year == y]
        per_year[y] = {"n": len(xs), "mean_net": sum(xs) / len(xs)}
    return {"verdict": "PASS" if passed else "FAIL", "net": net, "gross": gross, "split_date": mid.isoformat(),
            "halves": halves, "slope_1d": slope, "slope_3d": slope3, "by_side": side, "per_name": per_name,
            "per_year": per_year, "hit_rate": sum(t["net"] > 0 for t in trades) / len(trades)}


# ------------------------------------------------------------------ io
def read_events(path: Path) -> list[dict]:
    with open(path) as f:
        return [{"symbol": r["symbol"], "report_session": date.fromisoformat(r["report_session"]),
                 "entry_session": date.fromisoformat(r["entry_session"])}
                for r in csv.DictReader(f) if r["status"] == "kept" and r["symbol"] in RETAIL]


def read_trends(path: Path) -> dict[str, dict]:
    out = defaultdict(dict)
    with open(path) as f:
        for r in csv.DictReader(f):
            y, m = r["month"].split("-")
            out[r["symbol"]][(int(y), int(m))] = float(r["value"])
    return dict(out)


def read_closes(path: Path) -> tuple[dict, list[date]]:
    closes, spy = {}, []
    with open(path) as f:
        for r in csv.DictReader(f):
            d = date.fromisoformat(r["date"])
            closes[(r["symbol"], d)] = float(r["close"])
            if r["symbol"] == "SPY":
                spy.append(d)
    return closes, sorted(spy)


def pct(x, nd=2):
    return "n/a" if x is None else f"{100 * x:+.{nd}f}%"


def report(res: dict, counts: dict) -> str:
    n, g = res["net"], res["gross"]
    lines = [
        "# R1: retailer search interest before earnings — results",
        "",
        f"Rules: `research/r1_retail_search_prereg.md`. **Verdict: {res['verdict']}.**",
        "",
        f"- Trades: {n['n']} reports, {n['clusters']} report weeks. Skipped: {counts['skips']}.",
        f"- Net (5 bp round trip): mean {pct(n['mean'], 3)} per trade, t {n['t']:.2f} (bar {T_BAR}); "
        f"gross {pct(g['mean'], 3)}, t {g['t']:.2f}. Hit rate {res['hit_rate']:.1%}.",
        f"- Halves split at {res['split_date']}: " + "; ".join(
            f"{pct(h['mean'], 3)} (t {h['t']:.2f}, n {h['n']})" for h in res["halves"]),
        f"- Long side {pct(res['by_side']['long']['mean'], 3)} (n {res['by_side']['long']['n']}); short side "
        f"{pct(res['by_side']['short']['mean'], 3)} (n {res['by_side']['short']['n']}).",
        f"- Slope of abnormal return on z-scored surprise: 1-day {pct(res['slope_1d']['slope'], 3)} per SD "
        f"(t {res['slope_1d']['t']:.2f}); 3-day {pct(res['slope_3d']['slope'], 3)} per SD (t {res['slope_3d']['t']:.2f}).",
        "",
        "| Year | Trades | Mean net |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {y} | {v['n']} | {pct(v['mean_net'], 3)} |" for y, v in res["per_year"].items()]
    lines += ["", "| Name | Trades | Mean net | Hit rate |", "| --- | --- | --- | --- |"]
    lines += [f"| {s} | {v['n']} | {pct(v['mean_net'], 3)} | {v['hit']:.0%} |" for s, v in res["per_name"].items()]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--earnings", type=Path, default=DATA / "x1" / "retail_earnings.csv")
    ap.add_argument("--prices", type=Path, default=DATA / "x1" / "retail_daily.csv")
    ap.add_argument("--trends", type=Path, default=DATA / "retail" / "trends.csv")
    ap.add_argument("--out", type=Path, default=ROOT / "research" / "r1_retail_search_results.json")
    ap.add_argument("--md", type=Path, default=ROOT / "research" / "r1_retail_search.md")
    a = ap.parse_args()
    events = read_events(a.earnings)
    closes, sessions = read_closes(a.prices)
    trades, skips = build_trades(events, read_trends(a.trends), closes, sessions)
    res = evaluate(trades)
    counts = {"reports_in_file": len(events), "skips": skips, "traded": len(trades)}
    a.out.write_text(json.dumps({"counts": counts, **res}, indent=2, default=str))
    a.md.write_text(report(res, counts))
    print(report(res, counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
