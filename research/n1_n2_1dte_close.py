"""N1 and N2: 1DTE SPY credit structures sold at the close, held to 15:40 ET on expiry day, on real ThetaData quotes.
Rules frozen in research/n1_n2_1dte_close_prereg.md (merged to main before this script existed).

Night d -> e, where e is SPY's first listed expiry after d and also the next trading session; not when d or e is a
13:00 close, and not the night before an ex-dividend date (H4's calendar rules). At 15:50 ET on d, K = round(parity
spot) on the e chain:
  N1  sell the K call and K put, buy the K+5 call and K-5 put (iron fly);
  N2  sell the K put, buy the K-5 put (bull put spread).
At 15:40 ET on e close every leg (latest fully quoted minute back to 15:30). No take-profit, no stop. Fills mid /
patient (mid -/+ 1c, never worse than natural) / taker; $0.04 per contract per leg per side. Bar per candidate:
in-sample (2016-2021) mean > 0 at patient, out-of-sample (2022 on) t >= 2.58 at patient, out-of-sample mean > 0 at
taker.

Usage (Mac, repo root; uses the existing pulls, no download):
    .venv/bin/python research/n1_n2_1dte_close.py --quotes data/thetadata/spy_0dte \\
        --next-quotes data/thetadata/spy_next --out data/thetadata/n1_n2_results
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bd_real_quotes as bd  # noqa: E402
import h4_overnight_fly as h4  # noqa: E402

ENTRY_MIN = 15 * 60 + 50
EXIT_MIN, EXIT_FIRST = 15 * 60 + 40, 15 * 60 + 30
WING = 5.0
T_BAR = 2.58
IS_END, OOS_START = h4.IS_END, h4.OOS_START
CANDIDATES = ("N1", "N2")
SENSITIVITIES = {"exit 15:25": {"exit_min": 15 * 60 + 25}, "wings 3": {"wing": 3.0}, "wings 10": {"wing": 10.0}}
stats = h4.stats


def night_reason(d: date, e: date) -> str | None:
    """None when the night d -> e is traded, else why not."""
    why = h4.night_reason(d, e)
    if why:
        return why
    if e in h4._HALF:
        return "exit_half_day"
    return None


def legs(cand: str, S: float, wing: float = WING):
    k = float(round(S))
    if cand == "N1":
        return [("C", k, -1), ("P", k, -1), ("C", k + wing, 1), ("P", k - wing, 1)]
    if cand == "N2":
        return [("P", k, -1), ("P", k - wing, 1)]
    raise ValueError(cand)


def _open(cand: str, p_next: bd.QuotePanel, model: str, wing: float):
    S = bd.spot(p_next, ENTRY_MIN)
    if S is None:
        return None, {"skip": "no_spot"}
    lg = legs(cand, S, wing)
    credit = bd._value(p_next, lg, ENTRY_MIN, model, opening=True)
    if credit is None:
        return None, {"skip": "missing_leg"}
    if h4._wide(p_next, lg, ENTRY_MIN):
        return None, {"skip": "wide_leg"}
    return (S, lg, credit), None


def _result(S, lg, credit, debit, wing, fees, **extra) -> dict:
    debit = min(debit, wing)                     # a $wing-wide structure is never worth more than its width
    pnl = 100 * (credit - debit) - fees
    max_risk = 100 * (wing - credit) + 2 * len(lg) * bd.FEE * 100
    return {"spot": S, "strike": lg[0][1], "credit": credit, "debit": debit, "pnl": pnl, "max_risk": max_risk,
            "ret": pnl / max_risk if max_risk > 0 else float("nan"), **extra}


def trade(cand: str, p_next: bd.QuotePanel, p_exp: bd.QuotePanel, model: str, wing: float = WING,
          exit_min: int = EXIT_MIN) -> dict:
    """One night, closed on e at exit_min (or the latest fully quoted minute back to 15:30). $ per 1 lot."""
    opened, skip = _open(cand, p_next, model, wing)
    if skip:
        return skip
    S, lg, credit = opened
    for m in range(exit_min, min(exit_min, EXIT_FIRST) - 1, -1):
        debit = bd._value(p_exp, lg, m, model, opening=False)
        if debit is not None:
            return _result(S, lg, credit, debit, wing, 2 * len(lg) * bd.FEE * 100, exit_minute=m)
    return {"skip": "no_exit_quote"}


def settle_trade(cand: str, p_next: bd.QuotePanel, p_exp: bd.QuotePanel, model: str, wing: float = WING) -> dict:
    """Reference only (not tradeable on SPY): legs at intrinsic against e's last parity spot, no exit cost."""
    opened, skip = _open(cand, p_next, model, wing)
    if skip:
        return skip
    S, lg, credit = opened
    s_end = bd.spot(p_exp, min(p_exp.last_minute, bd.CLOSE - 1))
    if s_end is None:
        return {"skip": "no_settle_spot"}
    return _result(S, lg, credit, bd._payoff(lg, s_end), wing, len(lg) * bd.FEE * 100, settle=s_end)


def straddle(p_next: bd.QuotePanel, p_exp: bd.QuotePanel, model: str) -> float | None:
    """Short ATM straddle, same timing, as a fraction of the entry premium (descriptive; not tradeable at Level 3)."""
    S = bd.spot(p_next, ENTRY_MIN)
    if S is None:
        return None
    k = float(round(S))
    lg = [("C", k, -1), ("P", k, -1)]
    prem = bd._value(p_next, lg, ENTRY_MIN, model, opening=True)
    back = bd._value(p_exp, lg, EXIT_MIN, model, opening=False)
    if prem is None or back is None or prem <= 0:
        return None
    return (prem - back - 2 * len(lg) * bd.FEE) / prem


def spy_move(p_next: bd.QuotePanel, p_exp: bd.QuotePanel) -> float | None:
    """SPY's return from 15:50 on d to 15:40 on e (parity spots): context for N2's directional exposure."""
    a, b = bd.spot(p_next, ENTRY_MIN), bd.spot(p_exp, EXIT_MIN)
    return None if a is None or b is None else b / a - 1


def passes(r: dict) -> bool:
    return r["is"]["patient"]["mean"] > 0 and r["oos"]["patient"]["t"] >= T_BAR and r["oos"]["taker"]["mean"] > 0


# ---------------------------------------------------------------- run

def collect(quotes: Path, files) -> tuple[list, dict, list]:
    """Trades, skip counts and descriptive rows for the given spy_next files (the run can be done in chunks)."""
    rows, skips, extra = [], {}, []
    for f in files:
        f = Path(f)
        d = date.fromisoformat(f.stem)
        raw = h4._read(f)
        e = h4._expiry(raw)
        why = "no_expiry" if e is None else night_reason(d, e)
        if why is None and not (Path(quotes) / f"{e.isoformat()}.parquet").exists():
            why = "no_0dte_file"
        if why:
            skips[why] = skips.get(why, 0) + 1
            continue
        f2 = Path(quotes) / f"{e.isoformat()}.parquet"
        p1, p2 = h4._panel(raw, f.name), h4._panel(h4._read(f2), f2.name)
        if p1 is None or p2 is None:
            skips["unreadable"] = skips.get("unreadable", 0) + 1
            continue
        base = {"date": d, "expiry": e, "night": "weekday" if (e - d).days == 1 else "weekend/holiday"}
        for cand in CANDIDATES:
            for model in bd.MODELS:
                t = trade(cand, p1, p2, model)
                if "skip" in t:
                    if model == "patient":
                        k = f"{cand} {t['skip']}"
                        skips[k] = skips.get(k, 0) + 1
                    continue
                rows.append({**base, "cand": cand, "variant": "main", "model": model, **t})
                for label, kw in SENSITIVITIES.items():
                    s = trade(cand, p1, p2, model, **kw)
                    if "skip" not in s:
                        rows.append({**base, "cand": cand, "variant": label, "model": model, **s})
                s = settle_trade(cand, p1, p2, model)
                if "skip" not in s:
                    rows.append({**base, "cand": cand, "variant": "settlement value", "model": model, **s})
        for model in bd.MODELS:
            extra.append({**base, "model": model, "straddle": straddle(p1, p2, model)})
        extra.append({**base, "model": "spy", "spy_move": spy_move(p1, p2)})
    return rows, skips, extra


def finish(rows: list, skips: dict, extra: list, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out / "n1_n2_trades.csv", index=False)
    res = summarize(df, pd.DataFrame(extra), skips)
    (out / "n1_n2_results.json").write_text(json.dumps(res, indent=1, default=str))
    (out / "report.md").write_text(report(res))
    print(report(res))
    return res


def run(quotes: Path, next_quotes: Path, out: Path) -> dict:
    rows, skips, extra = collect(quotes, sorted(Path(next_quotes).glob("*.parquet")))
    return finish(rows, skips, extra, out)


def _split(g: pd.DataFrame) -> dict:
    return {"is": stats(g[g.date <= IS_END].pnl, g[g.date <= IS_END].ret),
            "oos": stats(g[g.date >= OOS_START].pnl, g[g.date >= OOS_START].ret)}


def summarize(df: pd.DataFrame, extra: pd.DataFrame, skips: dict) -> dict:
    res = {"skips": skips, "descriptive": {}}
    for cand in CANDIDATES:
        r = {"main": {"is": {}, "oos": {}}, "by_year": {}, "nights": {}, "sensitivities": {}}
        c = df[df.cand == cand] if not df.empty else df
        if c.empty:
            r["pass"] = False
            res[cand] = r
            continue
        m0 = c[c.variant == "main"]
        for model in bd.MODELS:
            m = m0[m0.model == model]
            for per, g in _split(m).items():
                r["main"][per][model] = g
            r["by_year"][model] = {int(y): stats(g.pnl, g.ret) for y, g in m.groupby(m.date.map(lambda x: x.year))}
            r["nights"][model] = {k: stats(g.pnl, g.ret) for k, g in m.groupby("night")}
        for label in list(SENSITIVITIES) + ["settlement value"]:
            s = c[c.variant == label]
            r["sensitivities"][label] = {model: _split(s[s.model == model]) for model in ("mid", "patient", "taker")}
        r["pass"] = passes(r["main"])
        res[cand] = r
    if not extra.empty:
        for model in bd.MODELS:
            s = extra[(extra.model == model)].dropna(subset=["straddle"]) if "straddle" in extra else extra.iloc[0:0]
            res["descriptive"][f"straddle {model}"] = {
                per: {"n": int(len(g)), "pct": float(g.straddle.mean() * 100) if len(g) else 0.0}
                for per, g in (("is", s[s.date <= IS_END]), ("oos", s[s.date >= OOS_START]))}
        sp = extra[extra.model == "spy"].dropna(subset=["spy_move"]) if "spy_move" in extra else extra.iloc[0:0]
        res["descriptive"]["spy move"] = {
            per: {"n": int(len(g)), "pct": float(g.spy_move.mean() * 100) if len(g) else 0.0}
            for per, g in (("is", sp[sp.date <= IS_END]), ("oos", sp[sp.date >= OOS_START]))}
    return res


def report(res: dict) -> str:
    H = ("| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |\n"
         "|---|---|---|---|---|---|---|---|---|")
    L = ["# N1 / N2: 1DTE SPY credit structures sold at 15:50 ET, closed 15:40 ET on expiry day (real quotes)", "",
         "Rules frozen in `research/n1_n2_1dte_close_prereg.md` before this script existed.",
         f"Bar per candidate: in-sample (2016-2021) mean > 0 at patient; out-of-sample (2022 on) t >= {T_BAR} at "
         "patient; out-of-sample mean > 0 at taker.", ""]
    L += [f"**{c}: {'PASS' if res.get(c, {}).get('pass') else 'FAIL'}**" for c in CANDIDATES]
    L += ["", "Nights skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(res["skips"].items()))]
    for cand in CANDIDATES:
        r = res.get(cand, {})
        name = "N1 iron fly ($5 wings)" if cand == "N1" else "N2 bull put spread ($5 wide)"
        L += ["", f"## {name}", "", H]
        for per in ("is", "oos"):
            for model in bd.MODELS:
                if model in r.get("main", {}).get(per, {}):
                    L.append(f"| {cand} {per} | {model} | " + h4._fmt(r["main"][per][model]) + " |")
        L += ["", f"### {cand} sensitivities (reported, never selected from)", "",
              "Settlement value prices the legs at intrinsic against the last parity spot on e (15:59), with no exit "
              "cost. It is not tradeable on SPY; it shows how much sits in the last 20 minutes.", "", H]
        for label, models in r.get("sensitivities", {}).items():
            for model, pers in models.items():
                for per, s in pers.items():
                    L.append(f"| {label} {per} | {model} | " + h4._fmt(s) + " |")
        L += ["", f"### {cand} weekday vs weekend/holiday nights", "", H]
        for model, rr in r.get("nights", {}).items():
            for k, s in rr.items():
                L.append(f"| {k} | {model} | " + h4._fmt(s) + " |")
        L += ["", f"### {cand} by year", "", H]
        for model in ("patient", "taker"):
            for y, s in sorted(r.get("by_year", {}).get(model, {}).items()):
                L.append(f"| {y} | {model} | " + h4._fmt(s) + " |")
    if res["descriptive"]:
        L += ["", "## Descriptive", "",
              "Short ATM straddle with the same timing, % of entry premium (not tradeable at Level 3), and SPY's own "
              "move from 15:50 on d to 15:40 on e.", "",
              "| Row | IS n | IS mean | OOS n | OOS mean |", "|---|---|---|---|---|"]
        for k, r in res["descriptive"].items():
            L.append(f"| {k} | {r['is']['n']} | {r['is']['pct']:+.3f}% | {r['oos']['n']} | {r['oos']['pct']:+.3f}% |")
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
