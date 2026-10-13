"""N3: weekly SPY put credit spread on real ThetaData quotes. Rules frozen in research/n3_weekly_put_spread_prereg.md
before the quote pull or this replay ran.

Week d -> e: d is the last session of a calendar week, e the last session of the next week (a listed SPY expiry);
not when d or e is a 13:00 close. At 15:50 ET on d: S = parity spot on d's same-day chain; EM = the e-expiry ATM
straddle mid at round(S); sell the round(S - 0.5 EM) put, buy the put W = max(1, round(S / 100)) dollars lower, both
expiry e. At 15:40 ET on e close both legs (latest fully quoted minute back to 15:30). No take-profit, no stop.
Fills mid / patient (mid -/+ 1c, never worse than natural) / taker; $0.04 per contract per leg per side. Bar:
in-sample (2016-2021) mean > 0 at patient, out-of-sample (2022 on) t >= 2.33 at patient, out-of-sample mean > 0 at
taker.

Usage (Mac, repo root, after research/fetch_thetadata_spy_weekly.py):
    .venv/bin/python research/n3_weekly_put_spread.py --quotes data/thetadata/spy_0dte \\
        --weekly-quotes data/thetadata/spy_weekly --out data/thetadata/n3_results
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
import h4_overnight_fly as h4  # noqa: E402

ENTRY_MIN = 15 * 60 + 50
EXIT_MIN, EXIT_FIRST = 15 * 60 + 40, 15 * 60 + 30
SHORT_MULT = 0.5
T_BAR = 2.33
IS_END, OOS_START = h4.IS_END, h4.OOS_START
SENSITIVITIES = {"short 0.25 x EM": {"short_mult": 0.25}, "short 1.0 x EM": {"short_mult": 1.0},
                 "width 2W": {"width_mult": 2}}
stats = h4.stats


def week_reason(d: date, e: date) -> str | None:
    """None when the week d -> e is traded, else why not."""
    h4._cal(d.year)
    h4._cal(e.year)
    if d in h4._HALF or e in h4._HALF:
        return "half_day"
    nxt = h4.next_session(e)
    nxt_week = (d + timedelta(days=7)).isocalendar()[:2]
    if e.isocalendar()[:2] != nxt_week or nxt.isocalendar()[:2] == nxt_week:
        return "expiry_not_next_week"
    return None


def strikes(S: float, em: float, short_mult: float = SHORT_MULT, width_mult: int = 1) -> tuple[float, float]:
    return float(round(S - short_mult * em)), float(max(1, round(S / 100)) * width_mult)


def _open(p_spot: bd.QuotePanel, p_week: bd.QuotePanel, model: str, short_mult: float, width_mult: int):
    S = bd.spot(p_spot, ENTRY_MIN)
    if S is None:
        return None, {"skip": "no_spot"}
    k0 = float(round(S))
    em = bd._value(p_week, [("C", k0, -1), ("P", k0, -1)], ENTRY_MIN, "mid", opening=True)
    if em is None or em <= 0:
        return None, {"skip": "no_implied_move"}
    ks, w = strikes(S, em, short_mult, width_mult)
    lg = [("P", ks, -1), ("P", ks - w, 1)]
    credit = bd._value(p_week, lg, ENTRY_MIN, model, opening=True)
    if credit is None:
        return None, {"skip": "missing_leg"}
    if h4._wide(p_week, lg, ENTRY_MIN):
        return None, {"skip": "wide_leg"}
    if credit <= 0:
        return None, {"skip": "no_credit"}
    return {"spot": S, "em": em, "short": ks, "width": w, "lg": lg, "credit": credit}, None


def _result(o: dict, debit: float, fees: float, **extra) -> dict:
    debit = min(debit, o["width"])
    pnl = 100 * (o["credit"] - debit) - fees
    max_risk = 100 * (o["width"] - o["credit"]) + 4 * bd.FEE * 100
    return {"spot": o["spot"], "em": o["em"], "short": o["short"], "width": o["width"], "credit": o["credit"],
            "debit": debit, "pnl": pnl, "max_risk": max_risk,
            "ret": pnl / max_risk if max_risk > 0 else float("nan"), **extra}


def trade(p_spot: bd.QuotePanel, p_week: bd.QuotePanel, p_exp: bd.QuotePanel, model: str,
          short_mult: float = SHORT_MULT, width_mult: int = 1, exit_min: int = EXIT_MIN) -> dict:
    o, skip = _open(p_spot, p_week, model, short_mult, width_mult)
    if skip:
        return skip
    for m in range(exit_min, min(exit_min, EXIT_FIRST) - 1, -1):
        debit = bd._value(p_exp, o["lg"], m, model, opening=False)
        if debit is not None:
            return _result(o, debit, 4 * bd.FEE * 100, exit_minute=m)
    return {"skip": "no_exit_quote"}


def settle_trade(p_spot, p_week, p_exp, model: str) -> dict:
    """Reference only (not tradeable on SPY): legs at intrinsic against e's last parity spot, no exit cost."""
    o, skip = _open(p_spot, p_week, model, SHORT_MULT, 1)
    if skip:
        return skip
    s_end = bd.spot(p_exp, min(p_exp.last_minute, bd.CLOSE - 1))
    if s_end is None:
        return {"skip": "no_settle_spot"}
    return _result(o, bd._payoff(o["lg"], s_end), 2 * bd.FEE * 100, settle=s_end)


def spy_move(p_spot: bd.QuotePanel, p_exp: bd.QuotePanel) -> float | None:
    a, b = bd.spot(p_spot, ENTRY_MIN), bd.spot(p_exp, EXIT_MIN)
    return None if a is None or b is None else b / a - 1


def net_of_spy(pnl: pd.Series, ret: pd.Series) -> dict:
    """OLS pnl = alpha + beta * SPY return; alpha's t; mean P&L in up and down weeks."""
    pnl, ret = pnl.astype(float).to_numpy(), ret.astype(float).to_numpy()
    n = len(pnl)
    if n < 3:
        return {"n": n}
    X = np.column_stack([np.ones(n), ret])
    coef, *_ = np.linalg.lstsq(X, pnl, rcond=None)
    resid = pnl - X @ coef
    s2 = resid @ resid / (n - 2)
    cov = s2 * np.linalg.inv(X.T @ X)
    return {"n": n, "alpha": float(coef[0]), "alpha_t": float(coef[0] / math.sqrt(cov[0, 0])) if cov[0, 0] > 0 else 0.0,
            "beta": float(coef[1]), "up_mean": float(pnl[ret > 0].mean()) if (ret > 0).any() else 0.0,
            "down_mean": float(pnl[ret <= 0].mean()) if (ret <= 0).any() else 0.0,
            "up_n": int((ret > 0).sum()), "down_n": int((ret <= 0).sum())}


def passes(r: dict) -> bool:
    return r["is"]["patient"]["mean"] > 0 and r["oos"]["patient"]["t"] >= T_BAR and r["oos"]["taker"]["mean"] > 0


# ---------------------------------------------------------------- run

def collect(quotes: Path, files) -> tuple[list, dict]:
    rows, skips = [], {}
    for f in files:
        f = Path(f)
        d = date.fromisoformat(f.stem)
        raw = h4._read(f)
        e = h4._expiry(raw)
        why = "no_expiry" if e is None else week_reason(d, e)
        if why is None and not (Path(quotes) / f"{d.isoformat()}.parquet").exists():
            why = "no_entry_spot"
        if why is None and not (Path(quotes) / f"{e.isoformat()}.parquet").exists():
            why = "no_0dte_file"
        if why:
            skips[why] = skips.get(why, 0) + 1
            continue
        fs, fe = Path(quotes) / f"{d.isoformat()}.parquet", Path(quotes) / f"{e.isoformat()}.parquet"
        pw, ps, pe = h4._panel(raw, f.name), h4._panel(h4._read(fs), fs.name), h4._panel(h4._read(fe), fe.name)
        if pw is None or ps is None or pe is None:
            skips["unreadable"] = skips.get("unreadable", 0) + 1
            continue
        base = {"date": d, "expiry": e, "spy_ret": spy_move(ps, pe)}
        for model in bd.MODELS:
            t = trade(ps, pw, pe, model)
            if "skip" in t:
                if model == "patient":
                    skips[t["skip"]] = skips.get(t["skip"], 0) + 1
                continue
            rows.append({**base, "variant": "main", "model": model, **t})
            for label, kw in SENSITIVITIES.items():
                s = trade(ps, pw, pe, model, **kw)
                if "skip" not in s:
                    rows.append({**base, "variant": label, "model": model, **s})
            s = settle_trade(ps, pw, pe, model)
            if "skip" not in s:
                rows.append({**base, "variant": "settlement value", "model": model, **s})
    return rows, skips


def finish(rows: list, skips: dict, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out / "n3_trades.csv", index=False)
    res = summarize(df, skips)
    (out / "n3_results.json").write_text(json.dumps(res, indent=1, default=str))
    (out / "report.md").write_text(report(res))
    print(report(res))
    return res


def run(quotes: Path, weekly_quotes: Path, out: Path) -> dict:
    rows, skips = collect(quotes, sorted(Path(weekly_quotes).glob("*.parquet")))
    return finish(rows, skips, out)


def _split(g: pd.DataFrame) -> dict:
    return {"is": stats(g[g.date <= IS_END].pnl, g[g.date <= IS_END].ret),
            "oos": stats(g[g.date >= OOS_START].pnl, g[g.date >= OOS_START].ret)}


def summarize(df: pd.DataFrame, skips: dict) -> dict:
    r = {"main": {"is": {}, "oos": {}}, "by_year": {}, "sensitivities": {}, "net_of_spy": {}}
    res = {"skips": skips, "N3": r}
    if df.empty:
        r["pass"] = False
        return res
    m0 = df[df.variant == "main"]
    for model in bd.MODELS:
        m = m0[m0.model == model]
        for per, g in _split(m).items():
            r["main"][per][model] = g
        r["by_year"][model] = {int(y): stats(g.pnl, g.ret) for y, g in m.groupby(m.date.map(lambda x: x.year))}
    for label in list(SENSITIVITIES) + ["settlement value"]:
        s = df[df.variant == label]
        r["sensitivities"][label] = {model: _split(s[s.model == model]) for model in ("mid", "patient", "taker")}
    p = m0[(m0.model == "patient")].dropna(subset=["spy_ret"])
    for per, g in (("is", p[p.date <= IS_END]), ("oos", p[p.date >= OOS_START])):
        r["net_of_spy"][per] = net_of_spy(g.pnl, g.spy_ret)
    r["mean_credit"] = float(m0[m0.model == "patient"].credit.mean())
    r["mean_width"] = float(m0[m0.model == "patient"].width.mean())
    r["pass"] = passes(r["main"])
    return res


def report(res: dict) -> str:
    r = res["N3"]
    H = ("| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |\n"
         "|---|---|---|---|---|---|---|---|---|")
    L = ["# N3: weekly SPY put credit spread, sold 15:50 ET on the week's last session, closed 15:40 ET on next "
         "week's expiry (real quotes)", "",
         "Rules frozen in `research/n3_weekly_put_spread_prereg.md` before the pull or this replay ran.",
         f"Bar: in-sample (2016-2021) mean > 0 at patient; out-of-sample (2022 on) t >= {T_BAR} at patient; "
         "out-of-sample mean > 0 at taker.", "", f"**N3: {'PASS' if r.get('pass') else 'FAIL'}**", "",
         "Weeks skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(res["skips"].items())), ""]
    if "mean_credit" in r:
        L += [f"Mean credit ${r['mean_credit']:.2f} a share on a mean width of ${r['mean_width']:.2f}.", ""]
    L += [H]
    for per in ("is", "oos"):
        for model in bd.MODELS:
            if model in r["main"][per]:
                L.append(f"| N3 {per} | {model} | " + h4._fmt(r["main"][per][model]) + " |")
    L += ["", "## Sensitivities (reported, never selected from)", "",
          "Settlement value prices the legs at intrinsic against e's last parity spot (15:59) with no exit cost; "
          "not tradeable on SPY.", "", H]
    for label, models in r["sensitivities"].items():
        for model, pers in models.items():
            for per, s in pers.items():
                L.append(f"| {label} {per} | {model} | " + h4._fmt(s) + " |")
    L += ["", "## Net of SPY's move (patient fills, descriptive)", "",
          "OLS of P&L per lot on SPY's return from 15:50 on d to 15:40 on e.", "",
          "| Period | n | alpha $/lot | alpha t | beta ($ per 100% move) | up weeks (n, mean $) | down weeks (n, mean $) |",
          "|---|---|---|---|---|---|---|"]
    for per, s in r["net_of_spy"].items():
        if s.get("n", 0) >= 3:
            L.append(f"| {per} | {s['n']} | {s['alpha']:+.2f} | {s['alpha_t']:+.2f} | {s['beta']:+.0f} | "
                     f"{s['up_n']}, {s['up_mean']:+.2f} | {s['down_n']}, {s['down_mean']:+.2f} |")
    L += ["", "## By year", "", H]
    for model in ("patient", "taker"):
        for y, s in sorted(r["by_year"].get(model, {}).items()):
            L.append(f"| {y} | {model} | " + h4._fmt(s) + " |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quotes", type=Path, required=True, help="data/thetadata/spy_0dte")
    ap.add_argument("--weekly-quotes", type=Path, required=True, help="data/thetadata/spy_weekly")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    run(a.quotes, a.weekly_quotes, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
