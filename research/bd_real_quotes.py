"""Books B (iron fly) and D (10:00 ET iron condor) replayed on real 1-minute SPY 0DTE NBBO quotes.

This is HANDOFF v3 section 10 step 7(a)/(d): the real-quote check behind the Black-Scholes tables in section 7.
Rules are copied from research/strategies_bcd.py (condor_like) and are not refitted:
  B: 09:45 ET, sell ATM call+put (strike = round(spot)), buy wings +-$5; TP when the closing debit <= 50% of the
     credit; stop when the closing debit >= 2x the credit; otherwise close at 15:30 ET (14:30 CT).
  D: 10:00 ET, shorts at ceil/floor(spot +- 0.9 x remaining-session expected move from the prior VIX close, using
     the backtest's 0.80 RTH factor), wings $2 beyond; same TP/stop; close 15:25 ET (14:25 CT).
     Quiet filter: first-hour range below its trailing 14-day median and spot within 0.12% of VWAP at entry.
     The backtest measured that range over 09:30-10:30, 30 minutes after the 10:00 entry (look-ahead). Both the
     as-backtested window and the only tradeable one (09:30-10:00) are reported, labelled.
Exits are checked every minute on the closing debit under the same fill model, like the backtest's bar loop.
Fill models per leg: mid; patient = mid -+ 1c but never worse than the natural price; taker = bid/ask.
Fees: $0.04 per contract per leg per side (review_option_order, 2026-09-27).
Spot comes from put-call parity on the 0DTE chain, so the quote files alone are enough; SPY 1-minute bars
(Alpaca SIP) are only needed for the quiet filter's range and VWAP.

Usage:
    python research/bd_real_quotes.py fetch-spy --start 2018-09-01 --end 2026-09-25 --out data/spy_1m
    python research/bd_real_quotes.py run --quotes data/thetadata/spy_0dte --out data/thetadata/bd_results \\
        [--spy data/spy_1m] [--vix research/data/vix.csv]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

FEE = 0.04 / 100            # $ per share per leg per side
M_RTH = 0.80                # backtest's RTH factor, used only to place D's strikes as the backtest did
OPEN, CLOSE = 9 * 60 + 30, 16 * 60
B_ENTRY, B_CLOSE = 9 * 60 + 45, 15 * 60 + 30
D_ENTRY, D_CLOSE = 10 * 60, 15 * 60 + 25
MODELS = ("mid", "patient", "taker")
VIX_URL = "https://raw.githubusercontent.com/datasets/finance-vix/main/data/vix-daily.csv"
DAILY_ERA = date(2022, 11, 14)   # approx. start of SPY Tue/Thu expiries; before it only some weekdays had a same-day expiry


# ---------------------------------------------------------------- loading

def normalize(raw: pd.DataFrame) -> pd.DataFrame:
    """ThetaData option quote rows -> minute (ET minute of day), right (C/P), strike ($), bid, ask."""
    df = raw.copy()
    cols = {c.lower(): c for c in df.columns}
    if "timestamp" in cols:
        ts = pd.to_datetime(df[cols["timestamp"]])
        if ts.dt.tz is not None:
            ts = ts.dt.tz_convert("America/New_York")
        minute = ts.dt.hour * 60 + ts.dt.minute
    elif "ms_of_day" in cols:
        minute = df[cols["ms_of_day"]] // 60_000
    else:
        raise ValueError(f"no timestamp column in {list(df.columns)}")
    strike = df[cols["strike"]].astype(float)
    if strike.median() > 20_000:                     # v2 style: thousandths of a dollar
        strike = strike / 1000.0
    right = df[cols["right"]].astype(str).str.upper().str[0]
    out = pd.DataFrame({"minute": minute.astype(int), "right": right, "strike": strike,
                        "bid": df[cols["bid"]].astype(float), "ask": df[cols["ask"]].astype(float)})
    return out[out.right.isin(["C", "P"])].reset_index(drop=True)


class QuotePanel:
    """Per-contract bid/ask on a 09:30..16:00 minute grid, forward-filled; missing or crossed quotes are dropped."""

    def __init__(self, df: pd.DataFrame):
        df = df[(df.ask > 0) & (df.bid >= 0) & (df.ask >= df.bid)]
        self.minutes = np.arange(OPEN, CLOSE + 1)
        self.q: dict[tuple[str, float], tuple[np.ndarray, np.ndarray]] = {}
        for (r, k), g in df.groupby(["right", "strike"]):
            g = g.drop_duplicates("minute", keep="last").set_index("minute").reindex(self.minutes).ffill()
            self.q[(r, float(k))] = (g.bid.to_numpy(), g.ask.to_numpy())
        self.last_minute = int(df.minute.max()) if len(df) else 0

    def ba(self, right: str, strike: float, minute: int):
        v = self.q.get((right, float(strike)))
        if v is None:
            return None
        i = minute - OPEN
        b, a = v[0][i], v[1][i]
        return None if (np.isnan(b) or np.isnan(a)) else (float(b), float(a))

    def strikes(self, right: str):
        return sorted(k for r, k in self.q if r == right)


def spot(p: QuotePanel, minute: int) -> float | None:
    """Put-call parity (no carry for same-day expiry): S = K + C_mid - P_mid on the strikes nearest the money."""
    est = []
    for k in set(p.strikes("C")) & set(p.strikes("P")):
        c, pp = p.ba("C", k, minute), p.ba("P", k, minute)
        if c and pp:
            cm, pm = (c[0] + c[1]) / 2, (pp[0] + pp[1]) / 2
            est.append((abs(cm - pm), k + cm - pm))
    if not est:
        return None
    est.sort()
    return float(np.median([s for _, s in est[:5]]))


def fill(bid: float, ask: float, side: str, model: str) -> float:
    mid = (bid + ask) / 2
    if model == "mid":
        return mid
    if model == "taker":
        return bid if side == "sell" else ask
    if model == "patient":
        return max(bid, mid - 0.01) if side == "sell" else min(ask, mid + 0.01)
    raise ValueError(model)


# ---------------------------------------------------------------- structures

def legs_B(S: float):
    k = float(round(S))
    return [("C", k, -1), ("P", k, -1), ("C", k + 5, 1), ("P", k - 5, 1)]


def legs_D(S: float, prev_vix: float):
    sdd = prev_vix / 100 / math.sqrt(252)
    em = 0.9 * M_RTH * sdd * math.sqrt((78 - 1 - 5) / 78 + 15 / 390) * S
    kc, kp = float(math.ceil(S + em)), float(math.floor(S - em))
    return [("C", kc, -1), ("P", kp, -1), ("C", kc + 2, 1), ("P", kp - 2, 1)]


def _value(p: QuotePanel, legs, minute: int, model: str, opening: bool):
    """Opening: net credit received. Closing: net debit paid. None if any leg lacks a quote."""
    tot = 0.0
    for r, k, q in legs:
        ba = p.ba(r, k, minute)
        if ba is None:
            return None
        short = q < 0
        if opening:
            tot += fill(*ba, "sell", model) if short else -fill(*ba, "buy", model)
        else:
            tot += fill(*ba, "buy", model) if short else -fill(*ba, "sell", model)
    return tot


def replay(p: QuotePanel, legs, entry: int, close: int, model: str, width: float, tp: float = 0.5,
           stop_mult: float = 2.0):
    val = _value(p, legs, entry, model, opening=True)
    if val is None:
        return None
    fees = len(legs) * FEE
    cr = val - fees
    if cr <= 0:
        return None
    exit_min, why, v = close, "time", None
    for t in range(entry + 1, close + 1):
        d = _value(p, legs, t, model, opening=False)
        if d is None:
            continue
        v = d + fees
        if v <= cr * tp:
            exit_min, why = t, "take 50%"
            break
        if v >= stop_mult * cr:
            exit_min, why = t, "stop"
            break
    else:
        d = _value(p, legs, close, model, opening=False)
        if d is None:
            return None
        v = d + fees
    pnl = cr - v
    return {"credit": cr, "risk": width - cr, "pnl": pnl, "ret": pnl / (width - cr), "why": why,
            "exit_minute": exit_min}


def _payoff(legs, S: float) -> float:
    """What the short structure owes at expiry (positive = loss to the seller)."""
    return -sum(q * (max(S - k, 0.0) if r == "C" else max(k - S, 0.0)) for r, k, q in legs)


def structure_vs_settlement(p: QuotePanel, legs, entry: int, model: str):
    cr = _value(p, legs, entry, model, opening=True)
    s_end = spot(p, min(p.last_minute, CLOSE - 1))
    if cr is None or s_end is None:
        return None
    pay = _payoff(legs, s_end)
    return {"credit": cr, "payoff": pay, "short_pnl": cr - pay - len(legs) * FEE, "settle": s_end}


def straddle_vs_move(p: QuotePanel, entry: int, model: str):
    S = spot(p, entry)
    if S is None:
        return None
    k = float(round(S))
    g = structure_vs_settlement(p, [("C", k, -1), ("P", k, -1)], entry, model)
    if g is None:
        return None
    return {"strike": k, "spot": S, "straddle": g["credit"], "move": abs(g["settle"] - k),
            "short_pnl": g["short_pnl"]}


# ---------------------------------------------------------------- quiet filter

def quiet_ok(bars: pd.DataFrame, day: date, price: float, window_end: int, entry: int = D_ENTRY,
             lookback: int = 14, vwap_band: float = 0.0012) -> bool:
    """bars: date, minute (bar start, ET), o h l c v. Range is measured over bars starting before window_end."""
    days = sorted(d for d in bars.date.unique() if d <= day)
    if len(days) < lookback + 1 or days[-1] != day:
        return False

    def rng(d):
        b = bars[(bars.date == d) & (bars.minute >= OPEN) & (bars.minute < window_end)]
        return (b.h.max() - b.l.min()) / b.c.iloc[-1] if len(b) else np.nan

    past = [rng(d) for d in days[-lookback - 1:-1]]
    today = rng(day)
    if np.isnan(today) or np.isnan(past).any() or today >= np.median(past):
        return False
    b = bars[(bars.date == day) & (bars.minute >= OPEN) & (bars.minute < entry)]
    if b.v.sum() <= 0:
        return False
    vwap = float((((b.h + b.l + b.c) / 3) * b.v).sum() / b.v.sum())
    return abs(price / vwap - 1) <= vwap_band


# ---------------------------------------------------------------- stats

def summ(x) -> dict:
    x = pd.Series(x, dtype=float).dropna()
    if len(x) < 3:
        return {"n": int(len(x))}
    w, l, sd = x[x > 0].sum(), -x[x < 0].sum(), x.std(ddof=1)
    return {"n": int(len(x)), "avg": float(x.mean() * 100), "win": float((x > 0).mean() * 100),
            "pf": float(w / l) if l > 0 else float("inf"),
            "t": float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else float("nan")}


def fmt(s: dict) -> str:
    if s.get("n", 0) < 3:
        return f"n={s.get('n', 0)}"
    return f"n={s['n']:4d}  avg {s['avg']:+6.2f}%  win {s['win']:4.1f}%  PF {s['pf']:4.2f}  t {s['t']:+5.2f}"


# ---------------------------------------------------------------- data plumbing

def load_vix(path: Path | None) -> pd.Series:
    if path is None or not Path(path).exists():
        path = Path(path or "research/data/vix.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(VIX_URL, path)
    v = pd.read_csv(path)
    v.columns = [c.upper() for c in v.columns]
    s = pd.Series(v["CLOSE"].values, index=pd.to_datetime(v["DATE"]).dt.date).sort_index()
    return s


def prev_close(s: pd.Series, d: date) -> float:
    before = s[s.index < d]
    return float(before.iloc[-1]) if len(before) else float("nan")


def load_spy_bars(folder: Path | None) -> pd.DataFrame | None:
    if folder is None or not Path(folder).exists():
        return None
    files = sorted(Path(folder).glob("*.parquet"))
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else None


def fetch_spy(start: date, end: date, out: Path) -> None:
    """Alpaca SIP 1-minute SPY bars (free once older than 15 minutes), one parquet per month."""
    import asyncio
    from zoneinfo import ZoneInfo
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    from agentdesk.feeds.alpaca import fetch_bars_1m
    et = ZoneInfo("America/New_York")
    out.mkdir(parents=True, exist_ok=True)
    m0 = date(start.year, start.month, 1)
    while m0 <= end:
        m1 = (m0 + timedelta(days=32)).replace(day=1)
        f = out / f"{m0:%Y-%m}.parquet"
        if not f.exists() or m1 > date.today():
            bars = asyncio.run(fetch_bars_1m("SPY", datetime.combine(m0, time(0), et), datetime.combine(min(m1, end + timedelta(days=1)), time(0), et), "sip"))
            rows = []
            for b in bars:
                t = datetime.fromtimestamp(b.t, et)
                rows.append({"date": t.date(), "minute": t.hour * 60 + t.minute, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v})
            pd.DataFrame(rows).to_parquet(f)
            print(f"{f.name}: {len(rows)} bars", flush=True)
        m0 = m1


def run(quotes: Path, out: Path, spy_dir: Path | None, vix_path: Path | None) -> dict:
    vix = load_vix(vix_path)
    bars = load_spy_bars(spy_dir)
    files = sorted(quotes.glob("*.parquet"))
    rows, skipped = [], {"half_day_or_short": 0, "no_spot": 0, "unreadable": 0}
    for f in files:
        d = date.fromisoformat(f.stem)
        try:
            p = QuotePanel(normalize(pd.read_parquet(f)))
        except Exception as e:  # noqa: BLE001
            print(f"{f.name}: unreadable ({e})", file=sys.stderr)
            skipped["unreadable"] += 1
            continue
        if p.last_minute < B_CLOSE:
            skipped["half_day_or_short"] += 1
            continue
        sB, sD = spot(p, B_ENTRY), spot(p, D_ENTRY)
        if sB is None or sD is None:
            skipped["no_spot"] += 1
            continue
        vx = prev_close(vix, d)
        base = {"date": d, "year": d.year, "era": "daily" if d >= DAILY_ERA else "pre-daily", "vix": vx}
        q_bt = q_nl = None
        if bars is not None:
            q_bt = quiet_ok(bars, d, sD, window_end=10 * 60 + 30)
            q_nl = quiet_ok(bars, d, sD, window_end=D_ENTRY)
        for model in MODELS:
            rB = replay(p, legs_B(sB), B_ENTRY, B_CLOSE, model, width=5)
            if rB:
                rows.append({**base, "book": "B", "model": model, **rB})
            if not math.isnan(vx):
                rD = replay(p, legs_D(sD, vx), D_ENTRY, D_CLOSE, model, width=2)
                if rD:
                    rows.append({**base, "book": "D", "model": model, "quiet_backtested": q_bt, "quiet_tradeable": q_nl, **rD})
            g = straddle_vs_move(p, B_ENTRY, model)
            if g:
                rows.append({**base, "book": "straddle@09:45 to settle", "model": model, "credit": g["straddle"],
                             "move": g["move"], "pnl": g["short_pnl"], "ret": g["short_pnl"] / g["straddle"], "why": "settle"})
            if not math.isnan(vx):
                h = structure_vs_settlement(p, legs_D(sD, vx), D_ENTRY, model)
                if h:
                    rows.append({**base, "book": "D condor@10:00 to settle", "model": model, "credit": h["credit"],
                                 "pnl": h["short_pnl"], "ret": h["short_pnl"] / (2 - h["credit"]), "why": "settle"})
    df = pd.DataFrame(rows)
    out.mkdir(parents=True, exist_ok=True)
    if df.empty:
        raise SystemExit(f"no usable days in {quotes} ({len(files)} files, skipped {skipped})")
    df.to_parquet(out / "trades.parquet")
    report, res = _report(df, skipped, len(files), bars is not None)
    (out / "report.md").write_text(report)
    (out / "results.json").write_text(json.dumps(res, indent=1, default=str))
    print(report)
    return res


def _report(df: pd.DataFrame, skipped: dict, n_files: int, have_bars: bool):
    res, lines = {}, []
    d0, d1 = df.date.min(), df.date.max()
    lines += [f"# Books B and D on real SPY 0DTE quotes ({d0} to {d1})", "",
              f"{n_files} quote files, {df.date.nunique()} sessions used, skipped {skipped}.",
              "Returns are % of max risk per trade (straddle row: % of the straddle premium). Fees included.", ""]
    groups = [("B", None), ("D", None)]
    if have_bars:
        groups += [("D", "quiet_tradeable"), ("D", "quiet_backtested")]
    groups += [("straddle@09:45 to settle", None), ("D condor@10:00 to settle", None)]
    for book, filt in groups:
        sub = df[df.book == book]
        if filt:
            sub = sub[sub[filt] == True]  # noqa: E712
        label = book + (f" [{filt.replace('_', ' ')} quiet filter]" if filt else "")
        lines += [f"## {label}", "", "| period | model | stats | avg $/1-lot | worst $ | avg credit $ |", "|---|---|---|---|---|---|"]
        for model in MODELS:
            sm = sub[sub.model == model]
            periods = [("all", sm)] + [(e, sm[sm.era == e]) for e in ("pre-daily", "daily")] + \
                      [(str(y), sm[sm.year == y]) for y in sorted(sm.year.unique())]
            for name, x in periods:
                if len(x) == 0:
                    continue
                s = summ(x.ret)
                s.update({"avg_usd": float(x.pnl.mean() * 100), "worst_usd": float(x.pnl.min() * 100),
                          "avg_credit_usd": float(x.credit.mean() * 100)})
                if "why" in x:
                    s["exits"] = x.why.value_counts().to_dict()
                res[f"{label}|{model}|{name}"] = s
                lines.append(f"| {name} | {model} | {fmt(s)} | {s['avg_usd']:+.1f} | {s['worst_usd']:+.0f} | {s['avg_credit_usd']:.0f} |")
        lines.append("")
    st = df[(df.book == "straddle@09:45 to settle") & (df.model == "mid")]
    if len(st):
        ratio = st.credit.sum() / st.move.sum()
        res["straddle_over_realized_mid"] = float(ratio)
        lines += [f"Go/no-go number (HANDOFF 7B): opening ATM straddle at mid / realized |move| to settlement = "
                  f"{ratio:.3f} over {len(st)} sessions (above 1 means premium was rich).", ""]
    return "\n".join(lines), res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-spy")
    f.add_argument("--start", type=date.fromisoformat, required=True)
    f.add_argument("--end", type=date.fromisoformat, required=True)
    f.add_argument("--out", type=Path, default=Path("data/spy_1m"))
    r = sub.add_parser("run")
    r.add_argument("--quotes", type=Path, default=Path("data/thetadata/spy_0dte"))
    r.add_argument("--out", type=Path, default=Path("data/thetadata/bd_results"))
    r.add_argument("--spy", type=Path, default=None)
    r.add_argument("--vix", type=Path, default=None)
    a = ap.parse_args()
    if a.cmd == "fetch-spy":
        fetch_spy(a.start, a.end, a.out)
    else:
        run(a.quotes, a.out, a.spy, a.vix)


if __name__ == "__main__":
    main()
