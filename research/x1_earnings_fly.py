"""X1: a short iron fly through earnings reports, replayed on real single-name option quotes. Rules frozen in
research/x1_earnings_fly_prereg.md. Run on the Mac after fetch_earnings_edgar.py and fetch_thetadata_x1.py:

    .venv/bin/python research/x1_earnings_fly.py        # writes research/x1_earnings_fly.md and .json

Sell the ATM call and put in the first expiry after the report at 15:45 ET on the last session before it, buy wings
1.5 straddles out, buy it all back at 10:00 ET on the first session after the report.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bd_real_quotes as bd  # noqa: E402
from agentdesk.books.f_report import clustered_t  # noqa: E402
from fetch_earnings_edgar import prev_session  # noqa: E402

MODELS = ("mid", "mid1", "mid_frac", "taker")
FEE = 0.04                      # per contract per leg per side
CFG = {"wing_x": 1.5, "entry_minute": 15 * 60 + 45, "exit_minute": 10 * 60, "max_spread_pct": 0.08,
       "tick_exempt": 0.05, "vix_max": 30.0, "use_vix": True, "min_credit_width": 0.25, "frac": 0.35}
VARIANTS = {"exit_0945": {"exit_minute": 9 * 60 + 45}, "exit_1545": {"exit_minute": 15 * 60 + 45},
            "wings_1.0x": {"wing_x": 1.0}, "wings_2.0x": {"wing_x": 2.0}, "no_vix_filter": {"use_vix": False}}
HALF = "2022-01-01"
SAMPLE = ("2018-01-01", "2026-09-30")


# ------------------------------------------------------------------ pricing
def mid(q) -> float:
    return (q[0] + q[1]) / 2


def leg_px(q, side: str, model: str, frac: float = 0.35) -> float:
    """One leg's fill per share. side is "sell" or "buy"."""
    b, a = q
    m = mid(q)
    if model == "mid":
        return m
    if model == "mid1":
        return max(b, m - 0.01) if side == "sell" else min(a, m + 0.01)
    if model == "mid_frac":
        return m - frac * (m - b) if side == "sell" else m + frac * (a - m)
    if model == "taker":
        return b if side == "sell" else a
    raise ValueError(model)


def leg_ok(q, cfg: dict) -> str | None:
    if q is None:
        return "no quote"
    b, a = q
    if b <= 0:
        return "zero bid"
    if a - b > max(cfg["tick_exempt"], cfg["max_spread_pct"] * mid(q)) + 1e-9:
        return f"spread {(a - b) / mid(q):.0%} of mid"
    return None


def nearest(strikes, target: float, prefer_high: bool) -> float | None:
    if not strikes:
        return None
    return min(strikes, key=lambda k: (abs(k - target), -k if prefer_high else k))


# ------------------------------------------------------------------ the trade
def build(panel, cfg: dict) -> tuple[dict | None, str | None]:
    """The fly at the entry minute on the entry session's panel, or (None, why)."""
    t = cfg["entry_minute"]
    s = bd.spot(panel, t)
    if s is None:
        return None, "no spot"
    both = [k for k in sorted(set(panel.strikes("C")) & set(panel.strikes("P")))
            if panel.ba("C", k, t) and panel.ba("P", k, t)]
    k = nearest(both, s, prefer_high=False)
    if k is None:
        return None, "no ATM strike"
    qc, qp = panel.ba("C", k, t), panel.ba("P", k, t)
    for label, q in (("ATM call", qc), ("ATM put", qp)):
        why = leg_ok(q, cfg)
        if why:
            return None, f"{label}: {why}"
    straddle = mid(qc) + mid(qp)
    cw = nearest([x for x in panel.strikes("C") if x > k], k + cfg["wing_x"] * straddle, prefer_high=True)
    pw = nearest([x for x in panel.strikes("P") if x < k], k - cfg["wing_x"] * straddle, prefer_high=False)
    if cw is None or pw is None:
        return None, "chain too short"
    for label, q in (("call wing", panel.ba("C", cw, t)), ("put wing", panel.ba("P", pw, t))):
        why = leg_ok(q, cfg)
        if why:
            return None, f"{label}: {why}"
    st = {"k": k, "cw": cw, "pw": pw, "spot": s, "straddle": straddle, "width": max(cw - k, k - pw)}
    credit = fly_value(panel, st, t, "mid", opening=True, frac=cfg["frac"])
    if credit < cfg["min_credit_width"] * st["width"]:
        return None, "credit below 25% of width"
    return st, None


def fly_value(panel, st: dict, minute: int, model: str, opening: bool, frac: float = 0.35) -> float | None:
    """Per share: the credit received to open (sell the body, buy the wings) or the debit paid to close."""
    legs = [("C", st["k"], -1), ("P", st["k"], -1), ("C", st["cw"], 1), ("P", st["pw"], 1)]   # -1 = short
    total = 0.0
    for r, k, pos in legs:
        q = panel.ba(r, k, minute)
        if q is None:
            return None
        side = ("sell" if pos < 0 else "buy") if opening else ("buy" if pos < 0 else "sell")
        px = leg_px(q, side, model, frac)
        total += px if side == "sell" else -px
    return total if opening else -total


def replay(ev: dict, entry_panel, exit_panel, vix_prev: float | None, cfg: dict) -> dict:
    """{"skipped": why} or {"rows": [one per fill model]}."""
    if cfg["use_vix"]:
        if vix_prev is None:
            return {"skipped": "no prior VIX close"}
        if vix_prev > cfg["vix_max"]:
            return {"skipped": "prior VIX close above 30"}
    st, why = build(entry_panel, cfg)
    if st is None:
        return {"skipped": why}
    if exit_panel is None:
        return {"skipped": "no exit quote"}
    rows = []
    for model in MODELS:
        credit = fly_value(entry_panel, st, cfg["entry_minute"], model, True, cfg["frac"])
        debit = fly_value(exit_panel, st, cfg["exit_minute"], model, False, cfg["frac"])
        if debit is None:
            return {"skipped": "no exit quote"}
        max_loss = st["width"] - credit
        if max_loss <= 0:
            return {"skipped": "credit at or above width"}
        pnl = (credit - debit) * 100 - FEE * 4 * 2
        rows.append({"symbol": ev["symbol"], "entry": ev["entry"], "exit": ev["exit"], "model": model,
                     "k": st["k"], "cw": st["cw"], "pw": st["pw"], "straddle": round(st["straddle"], 4),
                     "credit": round(credit, 4), "debit": round(debit, 4), "pnl": round(pnl, 2),
                     "ret": pnl / (max_loss * 100)})
    return {"rows": rows}


# ------------------------------------------------------------------ statistics
def summary(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"trades": 0}
    pnl = [r["pnl"] for r in rows]
    rets = [r["ret"] for r in rows]
    win, loss = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x <= 0)
    return {"trades": n, "win_rate": sum(x > 0 for x in pnl) / n, "mean_ret": sum(rets) / n,
            "pf": win / loss if loss > 0 else None, "t": clustered_t(rets, [r["entry"] for r in rows]),
            "pnl_per_lot": round(sum(pnl), 2), "worst": min(rets)}


def block(rows: list[dict]) -> dict:
    first = [r for r in rows if r["entry"] < HALF]
    second = [r for r in rows if r["entry"] >= HALF]
    years = defaultdict(list)
    for r in rows:
        years[r["entry"][:4]].append(r)
    return {"full": summary(rows), "2018_2021": summary(first), "2022_2026": summary(second),
            "by_year": {y: summary(v) for y, v in sorted(years.items())}}


def passes(taker: dict) -> bool:
    f = taker["full"]
    return bool(f.get("pf") and f["pf"] >= 1.1 and f.get("t") and f["t"] > 2.50
                and taker["2018_2021"].get("mean_ret", 0) > 0 and taker["2022_2026"].get("mean_ret", 0) > 0)


def run_variant(events, panels_for, vix: dict, cfg: dict) -> tuple[dict, Counter]:
    rows, skipped = [], Counter()
    for ev in events:
        panels = panels_for(ev)
        if panels is None:
            skipped["no quotes on disk"] += 1
            continue
        got = replay(ev, panels.get(ev["entry"]), panels.get(ev["exit"]), vix.get(ev["vix_day"]), cfg) \
            if panels.get(ev["entry"]) is not None else {"skipped": "no entry quotes"}
        if "skipped" in got:
            skipped[got["skipped"]] += 1
        else:
            rows += got["rows"]
    return {m: block([r for r in rows if r["model"] == m]) for m in MODELS}, skipped


def sanity(events: list[dict], daily: dict[str, dict[str, tuple[float, float]]]) -> dict:
    """Median |open(exit) / close(entry) - 1| on event pairs vs every other consecutive session pair."""
    ev_moves, pairs = [], {(e["symbol"], e["entry"], e["exit"]) for e in events}
    other = []
    for s, by_day in daily.items():
        days = sorted(by_day)
        for a, b in zip(days, days[1:]):
            m = abs(by_day[b][0] / by_day[a][1] - 1)
            (ev_moves if (s, a, b) in pairs else other).append(m)
    if not ev_moves or not other:
        return {"events": len(ev_moves), "ratio": None, "valid": False}
    ratio = statistics.median(ev_moves) / statistics.median(other)
    return {"events": len(ev_moves), "event_median": statistics.median(ev_moves),
            "other_median": statistics.median(other), "ratio": ratio, "valid": ratio >= 2}


# ------------------------------------------------------------------ disk
def read_events(path: Path) -> tuple[list[dict], Counter]:
    with open(path) as f:
        rows = list(csv.DictReader(f))
    status = Counter(r["status"].split(":")[0] for r in rows)
    events = [{"symbol": r["symbol"], "entry": r["entry_session"], "exit": r["report_session"],
               "timing": r["timing"], "vix_day": prev_session(date.fromisoformat(r["entry_session"])).isoformat()}
              for r in rows if r["status"] == "kept" and SAMPLE[0] <= r["entry_session"] <= SAMPLE[1]]
    return events, status


def read_daily(path: Path) -> dict[str, dict[str, tuple[float, float]]]:
    out: dict[str, dict[str, tuple[float, float]]] = defaultdict(dict)
    if path.exists():
        with open(path) as f:
            for r in csv.DictReader(f):
                out[r["symbol"]][r["date"]] = (float(r["open"]), float(r["close"]))
    return out


def panel_loader(quotes_dir: Path):
    import pandas as pd
    cache: dict[tuple, dict | None] = {}

    def load(ev: dict):
        key = (ev["symbol"], ev["exit"])
        if key not in cache:
            p = quotes_dir / f"{ev['symbol']}_{ev['exit']}.parquet"
            if not p.exists():
                cache[key] = None
            else:
                raw = pd.read_parquet(p)
                cache[key] = {str(d)[:10]: bd.QuotePanel(bd.normalize(g.drop(columns=["session"])))
                              for d, g in raw.groupby("session")}
        return cache[key]
    return load


# ------------------------------------------------------------------ report
def fmt(s: dict) -> str:
    if not s.get("trades"):
        return "no trades"
    f = lambda x: "n/a" if x is None else f"{x:.2f}"
    return (f"{s['trades']} trades, win {100 * s['win_rate']:.0f}%, mean {100 * s['mean_ret']:+.2f}% of max loss, "
            f"PF {f(s['pf'])}, t {f(s['t'])}, P&L per lot ${s['pnl_per_lot']:+,.0f}, worst {100 * s['worst']:.0f}%")


def report(res: dict, out: Path) -> None:
    main = res["X1"]
    lines = ["# X1: short iron fly through earnings, on real single-name quotes", "",
             f"Rules: `{res['rules']}`. Bar, all at taker fills: PF >= 1.1, t > 2.50 clustered by entry session, "
             "mean > 0 in 2018-2021 and in 2022-2026.", "",
             f"**Verdict: {'PASS' if main['pass'] else 'FAIL'}.**"
             + ("" if res["sanity"].get("valid") else " (Date sanity check failed: the run is invalid.)"), "",
             f"Events: {res['events']} kept reports in the sample; EDGAR filings by status: {res['edgar_status']}.",
             f"Date sanity check: {res['sanity']}.", "", "## X1 (wings 1.5 x straddle, exit 10:00 ET)", ""]
    for m in MODELS:
        b = main["models"][m]
        lines += [f"- **{m}**: {fmt(b['full'])}", f"  - 2018-2021: {fmt(b['2018_2021'])}",
                  f"  - 2022-2026: {fmt(b['2022_2026'])}"]
    lines += ["", "By year (mid1 / taker):", ""]
    for y in main["models"]["taker"]["by_year"]:
        lines.append(f"- {y}: {fmt(main['models']['mid1']['by_year'].get(y, {}))} / "
                     f"{fmt(main['models']['taker']['by_year'][y])}")
    lines += ["", "Skipped events: " + ", ".join(f"{k} {v}" for k, v in main["skipped"].most_common()), "",
              "## Sensitivities (reported, never selected from)", ""]
    for name, v in res["variants"].items():
        lines += [f"- {name}: mid1 {fmt(v['models']['mid1']['full'])}; taker {fmt(v['models']['taker']['full'])}"]
    (out / "x1_earnings_fly.md").write_text("\n".join(lines) + "\n")
    js = {**res, "X1": {**main, "skipped": dict(main["skipped"])},
          "variants": {k: {**v, "skipped": dict(v["skipped"])} for k, v in res["variants"].items()}}
    (out / "x1_earnings_fly_results.json").write_text(json.dumps(js, indent=1, default=str))


def run(earnings: Path, quotes_dir: Path, vix_csv: Path, daily_csv: Path, out: Path) -> dict:
    from vix_carry import read_index_csv
    events, status = read_events(earnings)
    vix = {d.isoformat(): float(v) for d, v in read_index_csv(vix_csv).items()}
    load = panel_loader(quotes_dir)
    res = {"rules": "research/x1_earnings_fly_prereg.md", "events": len(events), "edgar_status": dict(status),
           "sanity": sanity(events, read_daily(daily_csv))}
    models, skipped = run_variant(events, load, vix, CFG)
    res["X1"] = {"models": models, "skipped": skipped, "pass": passes(models["taker"])}
    res["variants"] = {}
    for name, change in VARIANTS.items():
        models, skipped = run_variant(events, load, vix, {**CFG, **change})
        res["variants"][name] = {"models": models, "skipped": skipped}
    report(res, out)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--earnings", type=Path, default=ROOT / "research" / "data" / "x1" / "earnings.csv")
    ap.add_argument("--quotes", type=Path, default=ROOT / "data" / "thetadata" / "x1")
    ap.add_argument("--vix", type=Path, default=ROOT / "data" / "cboe" / "VIX_History.csv")
    ap.add_argument("--daily", type=Path, default=ROOT / "research" / "data" / "x1" / "daily.csv")
    ap.add_argument("--out", type=Path, default=ROOT / "research")
    a = ap.parse_args()
    res = run(a.earnings, a.quotes, a.vix, a.daily, a.out)
    t = res["X1"]["models"]["taker"]["full"]
    print(f"X1 {'PASS' if res['X1']['pass'] else 'FAIL'}: taker {fmt(t)}; sanity {res['sanity']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
