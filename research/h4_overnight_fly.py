"""H4: overnight 1DTE SPY iron fly on real ThetaData quotes. Rules frozen in research/book_h_candidates_prereg.md
section 4 before this ran (commit 300b931).

Night d -> e, where e is SPY's first listed expiry after d and also the next trading session; not when d is a 13:00
close, and not the night before an ex-dividend date (third Friday of Mar/Jun/Sep/Dec). At 15:50 ET on d sell the
round(spot) call and put of expiry e and buy the +/-$5 wings (spot by put-call parity on the e chain). At 09:45 ET
on e buy all four back (first minute to 10:00 with all four quoted). No take-profit, no stop. Fills mid / patient
(mid -/+ 1c, never worse than natural) / taker; $0.04 per contract per leg per side. Bar: in-sample (2016-2021) mean
> 0 at patient, out-of-sample (2022 onward) t >= 2.33 at patient, out-of-sample mean > 0 at taker.

Usage (Mac, repo root, after research/fetch_thetadata_spy_next.py):
    .venv/bin/python research/h4_overnight_fly.py --quotes data/thetadata/spy_0dte \\
        --next-quotes data/thetadata/spy_next --out data/thetadata/h4_results
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bd_real_quotes as bd  # noqa: E402
import nyse_calendar  # noqa: E402

ENTRY_MIN, EXIT_MIN, EXIT_LAST = 15 * 60 + 50, 9 * 60 + 45, 10 * 60
WING = 5.0
SPREAD_PCT, SPREAD_EXEMPT = 0.25, 0.02
IS_END, OOS_START = date(2021, 12, 31), date(2022, 1, 1)
T_BAR = 2.33
SENSITIVITIES = {"exit 09:35": {"exit_min": 9 * 60 + 35}, "exit 10:00": {"exit_min": 10 * 60},
                 "wings +/-3": {"wing": 3.0}, "wings +/-10": {"wing": 10.0}}

_CLOSED: set[date] = set()
_HALF: set[date] = set()


def _cal(y: int) -> None:
    if not any(d.year == y for d in _CLOSED):
        _CLOSED.update(nyse_calendar.holidays_between(y - 1, y + 1))
        _HALF.update(nyse_calendar.half_days_between(y - 1, y + 1))


def next_session(d: date) -> date:
    _cal(d.year)
    d += timedelta(days=1)
    while d.weekday() >= 5 or d in _CLOSED:
        d += timedelta(days=1)
    return d


def ex_div_date(e: date) -> bool:
    """SPY goes ex-dividend on the third Friday of March, June, September and December."""
    return e.month in (3, 6, 9, 12) and e.weekday() == 4 and 15 <= e.day <= 21


def night_reason(d: date, e: date) -> str | None:
    """None when the night d -> e is traded, else why not."""
    _cal(d.year)
    if d in _HALF:
        return "half_day"
    if e != next_session(d):
        return "expiry_not_next_session"
    if ex_div_date(e):
        return "ex_div_eve"
    return None


def legs(S: float, wing: float = WING):
    k = float(round(S))
    return [("C", k, -1), ("P", k, -1), ("C", k + wing, 1), ("P", k - wing, 1)]


def _wide(p: bd.QuotePanel, lg, minute: int) -> bool:
    for r, k, _ in lg:
        b, a = p.ba(r, k, minute)
        mid = (a + b) / 2
        if a - b > SPREAD_EXEMPT and a - b > SPREAD_PCT * mid:
            return True
    return False


def trade(p_next: bd.QuotePanel, p_exp: bd.QuotePanel, model: str, wing: float = WING,
          exit_min: int = EXIT_MIN) -> dict:
    """One night. Returns the trade, or {"skip": reason}. $ per 1 lot; credit/debit per share."""
    S = bd.spot(p_next, ENTRY_MIN)
    if S is None:
        return {"skip": "no_spot"}
    lg = legs(S, wing)
    credit = bd._value(p_next, lg, ENTRY_MIN, model, opening=True)
    if credit is None:
        return {"skip": "missing_leg"}
    if _wide(p_next, lg, ENTRY_MIN):
        return {"skip": "wide_leg"}
    debit, used = None, None
    for m in range(exit_min, max(exit_min, EXIT_LAST) + 1):
        debit = bd._value(p_exp, lg, m, model, opening=False)
        if debit is not None:
            used = m
            break
    if debit is None:
        return {"skip": "no_exit_quote"}
    debit = min(debit, wing)                 # the fly is never worth more than its width
    fees = 2 * len(lg) * bd.FEE * 100
    pnl = 100 * (credit - debit) - fees
    max_risk = 100 * (wing - credit) + fees
    return {"spot": S, "strike": lg[0][1], "credit": credit, "debit": debit, "pnl": pnl, "max_risk": max_risk,
            "ret": pnl / max_risk if max_risk > 0 else float("nan"), "exit_minute": used}


def straddle(p_next: bd.QuotePanel, p_exp: bd.QuotePanel, model: str, exit_min: int = EXIT_MIN) -> float | None:
    """Short ATM straddle overnight, as a fraction of the entry premium (descriptive; not tradeable at Level 3)."""
    S = bd.spot(p_next, ENTRY_MIN)
    if S is None:
        return None
    k = float(round(S))
    lg = [("C", k, -1), ("P", k, -1)]
    prem = bd._value(p_next, lg, ENTRY_MIN, model, opening=True)
    back = None
    for m in range(exit_min, max(exit_min, EXIT_LAST) + 1):
        back = bd._value(p_exp, lg, m, model, opening=False)
        if back is not None:
            break
    if prem is None or back is None or prem <= 0:
        return None
    return (prem - back - 2 * len(lg) * bd.FEE) / prem


# ---------------------------------------------------------------- stats

def stats(pnl: pd.Series, ret: pd.Series) -> dict:
    pnl, ret = pnl.astype(float), ret.astype(float)
    n = len(pnl)
    if n == 0:
        return {"n": 0, "mean": 0.0, "t": 0.0}
    sd = pnl.std(ddof=1) if n > 1 else 0.0
    gains, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    return {"n": n, "mean": float(pnl.mean()), "t": float(pnl.mean() / (sd / math.sqrt(n))) if sd > 0 else 0.0,
            "win": float((pnl > 0).mean()), "pf": float(gains / losses) if losses > 0 else float("inf"),
            "worst": float(pnl.min()), "best": float(pnl.max()), "ret_pct": float(ret.mean() * 100),
            "total": float(pnl.sum())}


def passes(r: dict) -> bool:
    return r["is"]["patient"]["mean"] > 0 and r["oos"]["patient"]["t"] >= T_BAR and r["oos"]["taker"]["mean"] > 0


# ---------------------------------------------------------------- run

def _panel(raw: pd.DataFrame | None, name: str):
    try:
        return None if raw is None else bd.QuotePanel(bd.normalize(raw))
    except Exception as e:  # noqa: BLE001
        print(f"{name}: unreadable ({e})", file=sys.stderr)
        return None


def _read(path: Path) -> pd.DataFrame | None:
    try:
        return pd.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        print(f"{path.name}: unreadable ({e})", file=sys.stderr)
        return None


def _expiry(raw: pd.DataFrame | None) -> date | None:
    if raw is None or raw.empty:
        return None
    cols = {c.lower(): c for c in raw.columns}
    if "expiration" not in cols:
        return None
    return pd.to_datetime(raw[cols["expiration"]].iloc[0]).date()


def run(quotes: Path, next_quotes: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rows, skips, straddles = [], {}, []
    for f in sorted(Path(next_quotes).glob("*.parquet")):
        d = date.fromisoformat(f.stem)
        raw = _read(f)
        e = _expiry(raw)
        why = "no_expiry" if e is None else night_reason(d, e)
        if why is None and not (Path(quotes) / f"{e.isoformat()}.parquet").exists():
            why = "no_0dte_file"
        if why:
            skips[why] = skips.get(why, 0) + 1
            continue
        f2 = Path(quotes) / f"{e.isoformat()}.parquet"
        p1, p2 = _panel(raw, f.name), _panel(_read(f2), f2.name)
        if p1 is None or p2 is None:
            skips["unreadable"] = skips.get("unreadable", 0) + 1
            continue
        base = {"date": d, "expiry": e, "night": "weekday" if (e - d).days == 1 else "weekend/holiday"}
        for model in bd.MODELS:
            t = trade(p1, p2, model)
            if "skip" in t:
                if model == "patient":
                    skips[t["skip"]] = skips.get(t["skip"], 0) + 1
                continue
            rows.append({**base, "variant": "H4", "model": model, **t})
            for label, kw in SENSITIVITIES.items():
                s = trade(p1, p2, model, **kw)
                if "skip" not in s:
                    rows.append({**base, "variant": label, "model": model, **s})
            s = straddle(p1, p2, model)
            if s is not None:
                straddles.append({**base, "model": model, "ret": s})
    df = pd.DataFrame(rows)
    df.to_csv(out / "h4_trades.csv", index=False)
    res = summarize(df, pd.DataFrame(straddles), skips)
    (out / "h4_results.json").write_text(json.dumps(res, indent=1, default=str))
    (out / "report.md").write_text(report(res))
    print(report(res))
    return res


def summarize(df: pd.DataFrame, st: pd.DataFrame, skips: dict) -> dict:
    res = {"skips": skips, "H4": {"is": {}, "oos": {}}, "by_year": {}, "nights": {}, "sensitivities": {},
           "straddle": {}}
    if df.empty:
        res["pass"] = False
        return res
    h = df[df.variant == "H4"]
    for model in bd.MODELS:
        m = h[h.model == model]
        for per, mask in (("is", m.date <= IS_END), ("oos", m.date >= OOS_START)):
            res["H4"][per][model] = stats(m[mask].pnl, m[mask].ret)
        res["by_year"][model] = {int(y): stats(g.pnl, g.ret) for y, g in m.groupby(m.date.map(lambda x: x.year))}
        res["nights"][model] = {k: stats(g.pnl, g.ret) for k, g in m.groupby("night")}
    for label in SENSITIVITIES:
        s = df[df.variant == label]
        res["sensitivities"][label] = {model: {per: stats(g.pnl, g.ret) for per, g in
                                               (("is", s[(s.model == model) & (s.date <= IS_END)]),
                                                ("oos", s[(s.model == model) & (s.date >= OOS_START)]))}
                                       for model in ("patient", "taker")}
    wk = h[h.night == "weekday"]
    res["sensitivities"]["weekday nights only"] = {
        model: {per: stats(g.pnl, g.ret) for per, g in (("is", wk[(wk.model == model) & (wk.date <= IS_END)]),
                                                         ("oos", wk[(wk.model == model) & (wk.date >= OOS_START)]))}
        for model in ("patient", "taker")}
    if not st.empty:
        for model in bd.MODELS:
            s = st[st.model == model]
            res["straddle"][model] = {per: {"n": int(len(g)), "ret_pct": float(g.ret.mean() * 100) if len(g) else 0.0}
                                      for per, g in (("is", s[s.date <= IS_END]), ("oos", s[s.date >= OOS_START]))}
    res["pass"] = passes(res["H4"])
    return res


def _fmt(s: dict) -> str:
    if not s.get("n"):
        return "n=0 | | | | | | |"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"{s['n']} | {s['mean']:+.2f} | {s['t']:+.2f} | {s['ret_pct']:+.2f}% | {s['win'] * 100:.0f}% | {pf} | "
            f"{s['worst']:+.0f}")


def report(res: dict) -> str:
    H = "| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |\n|---|---|---|---|---|---|---|---|---|"
    L = ["# H4 overnight 1DTE iron fly: real-quote replay", "",
         "Rules frozen in `research/book_h_candidates_prereg.md` section 4 (commit 300b931) before this ran.",
         f"Bar: in-sample (2016-2021) mean > 0 at patient; out-of-sample (2022 on) t >= {T_BAR} at patient; "
         "out-of-sample mean > 0 at taker.", "", f"**Result: {'PASS' if res.get('pass') else 'FAIL'}**", "",
         "Nights skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(res["skips"].items())), "", H]
    for per in ("is", "oos"):
        for model in bd.MODELS:
            if model in res["H4"][per]:
                L.append(f"| H4 {per} | {model} | " + _fmt(res["H4"][per][model]) + " |")
    L += ["", "## Sensitivities (reported, never selected from)", "", H]
    for label, r in res["sensitivities"].items():
        for model, pers in r.items():
            for per, s in pers.items():
                L.append(f"| {label} {per} | {model} | " + _fmt(s) + " |")
    L += ["", "## Weekday vs weekend/holiday nights", "", H]
    for model, r in res["nights"].items():
        for k, s in r.items():
            L.append(f"| {k} | {model} | " + _fmt(s) + " |")
    L += ["", "## By year", "", H]
    for model in ("patient", "taker"):
        for y, s in sorted(res["by_year"].get(model, {}).items()):
            L.append(f"| {y} | {model} | " + _fmt(s) + " |")
    if res["straddle"]:
        L += ["", "## Short ATM straddle overnight, % of entry premium (descriptive)", "",
              "| Fills | IS n | IS mean | OOS n | OOS mean |", "|---|---|---|---|---|"]
        for model, r in res["straddle"].items():
            L.append(f"| {model} | {r['is']['n']} | {r['is']['ret_pct']:+.2f}% | {r['oos']['n']} | {r['oos']['ret_pct']:+.2f}% |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quotes", type=Path, required=True, help="data/thetadata/spy_0dte")
    ap.add_argument("--next-quotes", type=Path, required=True, help="data/thetadata/spy_next")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    run(a.quotes, a.next_quotes, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
