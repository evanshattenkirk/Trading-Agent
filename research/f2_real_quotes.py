"""Book F2 replayed on real 1-minute single-name option quotes (research/strategy_f2_prereg.md, section 6).

The rules come from agentdesk/books/f2_spreads.py, the same code the paper book runs, and are not refitted.
Signals come from Alpaca SIP 1-minute bars for F2's names; option quotes from ThetaData's US equity options history
(research/fetch_thetadata_equity.py pulls only what the signals need).

    python research/f2_real_quotes.py pull --start 2018-01-02 --end 2026-09-25     # SIP bars for F2's names (Alpaca keys)
    python research/f2_real_quotes.py signals                                       # -> research/data/f2/signals.csv
    python research/fetch_thetadata_equity.py                                       # quotes per signal (THETADATA_API_KEY)
    python research/f2_real_quotes.py run [--earnings earnings.csv]                 # -> research/f2_real_quotes.md/.json

Fill models on the whole spread: mid_frac (35% of the way from mid to natural, the paper book's model), taker
(natural) and mid. Fees: $0.04 per contract per leg per side. The stock's 1-minute bars drive C's first-day stop.
Without --earnings (a CSV of symbol,date report days) the replay can't skip reports, so holds through earnings stay
in the sample; the report says so.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentdesk.books import f2_spreads as S  # noqa: E402
from agentdesk.books.f_report import clustered_t  # noqa: E402

DATA = ROOT / "research" / "data" / "f2"
QUOTES = ROOT / "data" / "thetadata" / "f2"
FEE = 0.04                              # $ per contract per leg per side
OPEN, CLOSE = 570, 960                  # 09:30, 16:00 ET
MODELS = ("mid_frac", "taker", "mid")


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def f2_cfg() -> dict:
    from agentdesk.config import load_config
    return load_config()["books"]["F2_debit_spreads"]


# ------------------------------------------------------------------ signals from stock bars
def c_signals(day: date, bars: dict[str, list[dict]], hist: list[dict], names, cfg: dict) -> list[dict]:
    """C: F2's names with a green first 5-minute candle and RVOL5 >= rvol5_min (14-session base), the top
    max_armed_c by RVOL5; each enters on the first 1-minute bar before 10:30 ET whose high clears the OR high, at
    max(OR high, bar open)."""
    rows = []
    for s in names:
        bs = sorted((b for b in bars.get(s, []) if OPEN <= b["t"] < OPEN + 5), key=lambda b: b["t"])
        base = [h[s] for h in hist[-14:] if s in h]
        if not bs or len(base) < 14 or sum(base) <= 0:
            continue
        vol5 = sum(b["v"] for b in bs)
        rv = vol5 / (sum(base) / 14)
        o, c = bs[0]["o"], bs[-1]["c"]
        if c > o and rv >= cfg["rvol5_min"]:
            rows.append({"symbol": s, "rvol5": rv, "or_high": max(b["h"] for b in bs), "or_low": min(b["l"] for b in bs)})
    rows.sort(key=lambda r: (-r["rvol5"], r["symbol"]))
    out = []
    hh, mm = str(cfg["entry_cutoff_et"]).split(":")
    cutoff = int(hh) * 60 + int(mm)
    for r in rows[:int(cfg["max_armed_c"])]:
        for b in sorted(bars.get(r["symbol"], []), key=lambda b: b["t"]):
            if OPEN + 5 <= b["t"] < cutoff and b["h"] > r["or_high"]:
                out.append({"day": str(day), "symbol": r["symbol"], "setup": S.CALL, "minute": b["t"],
                            "spot": round(max(r["or_high"], b["o"]), 4), "or_high": r["or_high"], "or_low": r["or_low"],
                            "rvol5": round(r["rvol5"], 3)})
                break
    return out


def p_signals(day: date, bars: dict[str, list[dict]], daily: dict[str, list[dict]], names, cfg: dict) -> list[dict]:
    """P: at 15:40 ET, names up >= up_pct_p on the day with volume so far >= vol_ratio_p x the 20-day average."""
    entry = 15 * 60 + 40
    stats = {}
    for s in names:
        prior = [b for b in daily.get(s, []) if b["d"] < day]
        today = sorted((b for b in bars.get(s, []) if OPEN <= b["t"] < entry), key=lambda b: b["t"])
        stats[s] = S.day_stats(prior, today)
    return [{"day": str(day), "symbol": s, "setup": S.PUT, "minute": entry, "spot": x["last"], "or_high": None,
             "or_low": None, "chg_pct": x["chg_pct"], "vol_ratio": x["vol_ratio"]} for s, x in S.put_signals(stats, cfg)]


# ------------------------------------------------------------------ spread pricing
def spread_px(q_long, q_short, model: str, opening: bool, frac: float = 0.35) -> float:
    """A debit spread's price per share: pay it to open (long at the ask side), receive it to close."""
    mid = (q_long[0] + q_long[1]) / 2 - (q_short[0] + q_short[1]) / 2
    nat = (q_long[1] - q_short[0]) if opening else (q_long[0] - q_short[1])
    if model == "mid":
        return mid
    if model == "taker":
        return nat
    if model == "mid_frac":
        return mid + frac * (nat - mid)
    raise ValueError(model)


def leg_ok(q, cfg: dict) -> str | None:
    if q is None:
        return "no quote"
    b, a = q
    if b <= 0:
        return "zero bid"
    mid = (b + a) / 2
    if a - b > max(float(cfg["tick_exempt"]), float(cfg["max_leg_spread_pct"]) * mid) + 1e-9:
        return f"spread {(a - b) / mid:.0%} of mid"
    return None


def build(panel, spot: float, setup: str, minute: int, cfg: dict) -> tuple[dict | None, str | None]:
    """Strikes and prices at entry on the entry day's panel (bd_real_quotes.QuotePanel), or (None, why)."""
    both = sorted(set(panel.strikes("C")) & set(panel.strikes("P")))
    if not both:
        return None, "no strikes"
    k = min(both, key=lambda x: (abs(x - spot), x))
    qc, qp = panel.ba("C", k, minute), panel.ba("P", k, minute)
    for label, q in (("ATM call", qc), ("ATM put", qp)):
        why = leg_ok(q, cfg)
        if why:
            return None, f"{label} {k:g}: {why}"
    straddle = sum(q[0] + q[1] for q in (qc, qp)) / 2
    r = "C" if setup == S.CALL else "P"
    short = S.spread_strikes(set(panel.strikes(r)), k, S.RIGHT[setup], straddle * float(cfg["width_straddle_x"]),
                             int(cfg["min_steps"]))
    if short is None:
        return None, "chain too short"
    ql, qs = (qc if r == "C" else qp), panel.ba(r, short, minute)
    why = leg_ok(qs, cfg)
    if why:
        return None, f"short leg {short:g}: {why}"
    width = abs(short - k)
    debit = {m: spread_px(ql, qs, m, True, float(cfg["fill_frac"])) for m in MODELS}
    why = S.structure_problem(round(debit["mid_frac"], 2), width, cfg)
    if why:
        return None, why
    return {"right": r, "long": k, "short": short, "width": width, "straddle": straddle, "debit": debit}, None


def replay(sig: dict, panels: dict[str, object], stock: dict[str, list[dict]], exit_day: str, cfg: dict,
           model: str, half_day: bool = False) -> dict | None:
    """Walk the spread minute by minute from entry to exit under one fill model. `panels` maps each session (str
    date) to its QuotePanel; `stock` maps each session to the name's 1-minute bars (C's first-day stop);
    `half_day` says whether the exit day closes at 13:00 ET (the exit then moves to 12:45, as in the paper book)."""
    day0 = sig["day"]
    got, why = build(panels[day0], sig["spot"], sig["setup"], sig["minute"], cfg)
    if got is None:
        return {"skipped": why}
    r, kl, ks, width = got["right"], got["long"], got["short"], got["width"]
    entry = got["debit"][model]
    xt = S.exit_time_et(sig["setup"], cfg, half_day)
    exit_m = xt.hour * 60 + xt.minute
    lows = {b["t"]: b["l"] for b in stock.get(day0, [])}
    out_px, why_out, when = None, None, None
    for d in sorted(k for k in panels if day0 <= k <= exit_day):
        p = panels[d]
        start = sig["minute"] + 1 if d == day0 else OPEN
        stop_at = exit_m if d == exit_day else min(CLOSE - 1, p.last_minute)
        for m in range(start, stop_at + 1):
            ql, qs = p.ba(r, kl, m), p.ba(r, ks, m)
            if d == day0 and sig["setup"] == S.CALL and sig.get("or_low") and m in lows and lows[m] <= sig["or_low"]:
                if ql and qs:
                    out_px, why_out, when = spread_px(ql, qs, model, False, float(cfg["fill_frac"])), "thesis stop", (d, m)
                    break
            if not (ql and qs):
                continue
            mark = spread_px(ql, qs, "mid", False)
            it = S.tp_stop(entry, mark, width, cfg)
            if it is not None or (d == exit_day and m == exit_m):
                out_px = spread_px(ql, qs, model, False, float(cfg["fill_frac"]))
                why_out, when = (it.reason.split(":")[0] if it else "time exit"), (d, m)
                break
        if out_px is not None:
            break
    if out_px is None:
        return {"skipped": "no quotes to exit on"}
    fees = FEE * 2 * 2
    pnl = (out_px - entry) * 100 - fees
    return {"day": day0, "symbol": sig["symbol"], "setup": sig["setup"], "model": model, "long": kl, "short": ks,
            "width": width, "entry": round(entry, 4), "exit": round(out_px, 4), "exit_why": why_out,
            "exit_at": f"{when[0]} {when[1] // 60:02d}:{when[1] % 60:02d}", "pnl": round(pnl, 2),
            "ret": pnl / (entry * 100) if entry > 0 else 0.0, "debit_width": round(entry / width, 3)}


# ------------------------------------------------------------------ stats and report
def summary(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"trades": 0}
    pnl = [r["pnl"] for r in rows]
    rets = [r["ret"] for r in rows]
    win, loss = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x <= 0)
    return {"trades": n, "win_rate": sum(1 for x in pnl if x > 0) / n, "mean_ret": sum(rets) / n,
            "pf": win / loss if loss > 0 else None, "t": clustered_t(rets, [r["day"] for r in rows]),
            "pnl_per_lot": round(sum(pnl), 2)}


def passes(full: dict, first: dict, second: dict) -> bool:
    return bool(full.get("pf") and full["pf"] >= 1.1 and full.get("t") and full["t"] > 2
                and first.get("mean_ret", 0) > 0 and second.get("mean_ret", 0) > 0)


def report(results: list[dict], skipped: dict, have_earnings: bool, out: Path) -> dict:
    res = {"prereg": "research/strategy_f2_prereg.md", "earnings_skipped": have_earnings, "setups": {},
           "skipped": skipped}
    for setup in (S.CALL, S.PUT):
        res["setups"][setup] = {}
        for model in MODELS:
            rows = [r for r in results if r["setup"] == setup and r["model"] == model]
            first = [r for r in rows if r["day"] < "2022-01-01"]
            second = [r for r in rows if r["day"] >= "2022-01-01"]
            block = {"full": summary(rows), "2018_2021": summary(first), "2022_2026": summary(second)}
            block["pass"] = passes(block["full"], block["2018_2021"], block["2022_2026"])
            res["setups"][setup][model] = block
    (out / "f2_real_quotes.json").write_text(json.dumps(res, indent=1, default=str))
    f = lambda x, p=3: "n/a" if x is None else f"{x:.{p}f}"
    line = lambda s: ("no trades" if not s.get("trades") else
                      f"{s['trades']} trade{'' if s['trades'] == 1 else 's'}, win {100 * s['win_rate']:.1f}%, "
                      f"mean {100 * s['mean_ret']:.1f}% of the debit, PF {f(s['pf'], 2)}, t {f(s['t'], 2)}, "
                      f"P&L per lot ${s['pnl_per_lot']:+.0f}")
    lines = ["# Book F2 on real single-name option quotes", "",
             f"Pre-registration: `{res['prereg']}`. Judged at taker fills (the bar: PF >= 1.1, t > 2 clustered by "
             "day, positive in both 2018-2021 and 2022-2026); mid_frac is the paper book's own model.", ""]
    if not have_earnings:
        lines += ["Earnings holds are NOT excluded (no --earnings file): reports inside the hold are in the sample, "
                  "which the paper book would have skipped.", ""]
    for setup, name in ((S.CALL, "C, call debit spreads"), (S.PUT, "P, put debit spreads")):
        lines += [f"## {name}", ""]
        for model in MODELS:
            b = res["setups"][setup][model]
            lines += [f"- {model}: {line(b['full'])}{' (passes)' if b['pass'] else ''}",
                      f"  - 2018-2021: {line(b['2018_2021'])}", f"  - 2022-2026: {line(b['2022_2026'])}"]
        lines.append("")
    lines += ["Skipped signals:", ""] + [f"- {k}: {v}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])]
    (out / "f2_real_quotes.md").write_text("\n".join(lines) + "\n")
    return res


# ------------------------------------------------------------------ disk
def load_panels(path: Path):
    """One signal's quote file (fetch_thetadata_equity.py) -> {session: QuotePanel}."""
    import pandas as pd
    bd = _load("bd_real_quotes", "bd_real_quotes.py")
    raw = pd.read_parquet(path)
    out = {}
    for d, g in raw.groupby("session"):
        out[str(d)[:10]] = bd.QuotePanel(bd.normalize(g.drop(columns=["session"])))
    return out


def load_earnings(path: Path | None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    if path:
        with open(path) as f:
            for row in csv.DictReader(f):
                out[row["symbol"].strip().upper()].add(row["date"].strip()[:10])
    return out


def run(signals_csv: Path, quotes_dir: Path, store, out: Path, earnings: Path | None) -> dict:
    from datetime import date as _d
    from nyse_calendar import half_days_between, holidays_between
    cfg = f2_cfg()
    earn = load_earnings(earnings)
    results, skipped = [], defaultdict(int)
    with open(signals_csv) as f:
        sigs = list(csv.DictReader(f))
    for s in sigs:
        s["minute"], s["spot"] = int(s["minute"]), float(s["spot"])
        s["or_low"] = float(s["or_low"]) if s.get("or_low") not in (None, "", "None") else None
        d0 = _d.fromisoformat(s["day"])
        x = S.exit_day(d0, s["setup"], holidays_between(d0.year, d0.year + 1), cfg)
        xd, half = str(x), x in half_days_between(x.year, x.year)
        if earn and any(s["day"] <= e <= xd for e in earn.get(s["symbol"], ())):
            skipped["report inside the hold"] += 1
            continue
        qp = quotes_dir / f"{s['symbol']}_{s['day']}_{s['setup']}.parquet"
        if not qp.exists():
            skipped["no quote file (no expiry 5-12 days out, or not fetched)"] += 1
            continue
        panels = load_panels(qp)
        if s["day"] not in panels:
            skipped["no entry-day quotes"] += 1
            continue
        stock = {d: store.read_bars(store.day_path(_d.fromisoformat(d))).get(s["symbol"], []) for d in panels
                 if store.day_path(_d.fromisoformat(d)).exists()}
        for model in MODELS:
            r = replay(s, panels, stock, xd, cfg, model, half)
            if r is None or "skipped" in r:
                if model == "mid_frac":
                    skipped[(r or {}).get("skipped", "?").split(":")[0]] += 1
                continue
            results.append(r)
    return report(results, dict(skipped), bool(earn), out)


def pull(start: date, end: date, data_dir: Path) -> None:
    R = _load("strategy_f_intraday", "strategy_f_intraday.py")
    from agentdesk.config import _load_dotenv
    _load_dotenv(ROOT / ".env")
    store = R.Store(data_dir)
    names = sorted({s.upper() for s in f2_cfg()["universe"]})
    h = R.AlpacaHistory(R.HttpxGet())
    R.pull_daily(store, h, names + ["SPY"], date.fromordinal(start.toordinal() - 60), end)
    dates = [b["d"] for b in store.read_daily("SPY") if start <= b["d"] <= end]
    for i, d in enumerate(dates):
        if store.day_path(d).exists():
            continue
        got = h.bars(names, "1Min", R._iso(d, OPEN), R._iso(d, CLOSE))
        store.write_bars(store.day_path(d), {s: [b for b in bs if OPEN <= b["t"] < CLOSE] for s, bs in got.items()})
        if i % 100 == 0:
            print(f"  F2 day bars {d} ({i + 1}/{len(dates)}, {h.calls} calls)", flush=True)


def signals(data_dir: Path, out_csv: Path) -> int:
    from nyse_calendar import half_days_between
    R = _load("strategy_f_intraday", "strategy_f_intraday.py")
    store = R.Store(data_dir)
    cfg = f2_cfg()
    names = sorted({s.upper() for s in cfg["universe"]})
    daily = {s: store.read_daily(s) for s in names if store.daily_path(s).exists()}
    dates = [b["d"] for b in store.read_daily("SPY")]
    half = half_days_between(dates[0].year, dates[-1].year) if dates else set()
    rows, hist = [], []
    for d in dates:
        if not store.day_path(d).exists():
            hist.append({})
            continue
        bars = store.read_bars(store.day_path(d))
        rows += c_signals(d, bars, hist, names, cfg)
        if d not in half:                   # the paper book skips P's 15:40 scan on 13:00-close days
            rows += p_signals(d, bars, daily, names, cfg)
        hist.append({s: sum(b["v"] for b in bs if OPEN <= b["t"] < OPEN + 5) for s, bs in bars.items()})
    cols = ["day", "symbol", "setup", "minute", "spot", "or_high", "or_low", "rvol5", "chg_pct", "vol_ratio"]
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) for c in cols})
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull")
    p.add_argument("--start", default="2018-01-02")
    p.add_argument("--end", default="2026-09-25")
    sub.add_parser("signals")
    r = sub.add_parser("run")
    r.add_argument("--earnings", default=None, help="CSV with symbol,date report days")
    a = ap.parse_args()
    if a.cmd == "pull":
        pull(date.fromisoformat(a.start), date.fromisoformat(a.end), DATA)
    elif a.cmd == "signals":
        print(f"{signals(DATA, DATA / 'signals.csv')} signals -> {DATA / 'signals.csv'}")
    else:
        R = _load("strategy_f_intraday", "strategy_f_intraday.py")
        res = run(DATA / "signals.csv", QUOTES, R.Store(DATA), ROOT / "research", Path(a.earnings) if a.earnings else None)
        print((ROOT / "research" / "f2_real_quotes.md").read_text())
        print("C passes at taker:", res["setups"][S.CALL]["taker"]["pass"], "| P passes at taker:",
              res["setups"][S.PUT]["taker"]["pass"])


if __name__ == "__main__":
    main()
