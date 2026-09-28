"""Book F replication backtest (docs/BOOK_F_HANDOFF.md section 5). Pre-registered: the spec is section 3.

Run from the repo root, with ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY in .env:

    python research/strategy_f_intraday.py                    # pull (resumable) + backtest + report
    python research/strategy_f_intraday.py --pull-only        # just download; re-run any time to resume
    python research/strategy_f_intraday.py --report-only      # backtest the cached data

Data (Alpaca /v2/stocks/bars, feed=sip, adjustment=split), cached under research/data/f_intraday/ (gitignored):
  constituents.csv        today's S&P 500 list (datasets/s-and-p-500-companies); survivorship bias, stated in the report
  daily/SYM.csv           daily bars 2015-09 -> end, `date,open,high,low,close,volume` (also the input for the
                          2016-2026 rerun of research/strategy_f.py)
  or/YYYY/DATE.csv.gz     09:30-09:34 ET 1-minute bars for every S&P name plus the extras (RVOL5 needs all of them)
  day/YYYY/DATE.csv.gz    09:35-16:00 ET 1-minute bars for the day's candidates under the loosest sensitivity
                          (RVOL5 >= 1.5, top 10 green), which covers every variant below
Each file is written atomically, so an interrupted pull resumes where it stopped.

Rules come from agentdesk/books/f_stocks_in_play.py, the same code the paper book runs. Fills follow the
PaperEquityBroker bar rules with 2 bp slippage per side and no commission. Known differences from paper, stated
in the report: no macro-event skip (no historical calendar), today's S&P list (survivorship), half-days detected
from the data (last bar before 15:00 ET).

Outputs: research/strategy_f_intraday_results.json, research/strategy_f_intraday.md, research/strategy_f_equity.png
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentdesk.books import f_stocks_in_play as F  # noqa: E402

DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
CONSTITUENTS_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
PRIMARY = {"rvol5_min": 2.0, "top_n": 5, "first_candle": "green", "entry_cutoff_et": "10:30", "stop_atr_frac": 0.10,
           "exit_et": "15:55", "risk_per_trade": 25, "max_notional": 1000, "max_positions": 5, "daily_loss": 75,
           "universe": {"min_price": 10, "min_atr": 0.50, "min_dollar_vol_20d": 100_000_000,
                        "top_sp500_by_dollar_vol": 130, "extra": list(F.AI_LIST)}}
LOOSEST = {"rvol5_min": 1.5, "top_n": 10}
SENSITIVITY = ([("rvol5_min", v) for v in (1.5, 3.0)] + [("top_n", v) for v in (3, 10)]
               + [("stop_atr_frac", v) for v in (0.05, 0.20)] + [("slip_bp", 5)])
OR_START, SCAN, CUTOFF = 570, 575, 630          # minutes after midnight ET: 09:30, 09:35, 10:30
BALANCE = 10_000


# ------------------------------------------------------------------ Alpaca
class HttpxGet:
    def __init__(self):
        import httpx
        k, s = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
        if not k or not s:
            raise SystemExit("Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env")
        self.c = httpx.Client(headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}, timeout=60)

    def get(self, url, params):
        for i in range(8):
            try:
                r = self.c.get(url, params=params)
            except Exception as ex:           # network blip: back off and retry
                print(f"  network error ({ex}); retry {i + 1}", flush=True)
                time.sleep(2 ** min(i, 5))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(max(float(r.headers.get("retry-after", 0) or 0), 2 ** min(i, 5)))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Alpaca kept failing for {params.get('symbols', '')[:60]}")


def _minute_et(iso: str) -> tuple[date, int]:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(F.ET)
    return d.date(), d.hour * 60 + d.minute


class AlpacaHistory:
    def __init__(self, http, per_min: int = 190):
        self.http, self.gap, self._last, self.calls = http, 60.0 / per_min, 0.0, 0

    def bars(self, symbols, timeframe: str, start: str, end: str) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = defaultdict(list)
        token = None
        while True:
            p = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "end": end, "feed": "sip",
                 "adjustment": "split", "limit": 10000, "sort": "asc"}
            if token:
                p["page_token"] = token
            wait = self._last + self.gap - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            j = self.http.get(DATA_URL, p)
            for sym, bs in (j.get("bars") or {}).items():
                for b in bs:
                    d, m = _minute_et(b["t"])
                    row = {"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
                    out[sym].append({"d": d, **row} if timeframe == "1Day" else {"t": m, **row})
            token = j.get("next_page_token")
            if not token:
                return dict(out)


# ------------------------------------------------------------------ cache
class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def daily_path(self, sym: str) -> Path:
        return self.root / "daily" / f"{sym}.csv"

    def or_path(self, d: date) -> Path:
        return self.root / "or" / str(d.year) / f"{d}.csv.gz"

    def day_path(self, d: date) -> Path:
        return self.root / "day" / str(d.year) / f"{d}.csv.gz"

    @staticmethod
    def _atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)

    def write_daily(self, sym: str, rows: list[dict]) -> None:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow([r["d"].isoformat(), r["o"], r["h"], r["l"], r["c"], r["v"]])
        self._atomic(self.daily_path(sym), buf.getvalue().encode())

    def read_daily(self, sym: str) -> list[dict]:
        with open(self.daily_path(sym)) as f:
            return [{"d": date.fromisoformat(r["date"]), "o": float(r["open"]), "h": float(r["high"]),
                     "l": float(r["low"]), "c": float(r["close"]), "v": float(r["volume"])} for r in csv.DictReader(f)]

    def write_bars(self, path: Path, bars: dict[str, list[dict]]) -> None:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["symbol", "t", "o", "h", "l", "c", "v"])
        for sym in sorted(bars):
            for b in bars[sym]:
                w.writerow([sym, b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]])
        self._atomic(path, gzip.compress(buf.getvalue().encode()))

    def read_bars(self, path: Path) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = defaultdict(list)
        with gzip.open(path, "rt") as f:
            for r in csv.DictReader(f):
                out[r["symbol"]].append({"t": int(r["t"]), "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
                                         "c": float(r["c"]), "v": float(r["v"])})
        return dict(out)


def _iso(d: date, minute: int) -> str:
    return datetime.combine(d, datetime.min.time(), F.ET).replace(hour=minute // 60, minute=minute % 60) \
        .astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def pull_or_windows(store: Store, h: AlpacaHistory, symbols: list[str], dates: list[date], batch: int = 100) -> None:
    for i, d in enumerate(dates):
        if store.or_path(d).exists():
            continue
        got: dict[str, list[dict]] = {}
        for grp in _chunks(sorted(symbols), batch):
            got.update(h.bars(grp, "1Min", _iso(d, OR_START), _iso(d, SCAN - 1)))
        store.write_bars(store.or_path(d), {s: [b for b in bs if OR_START <= b["t"] < SCAN] for s, bs in got.items()})
        if i % 50 == 0:
            print(f"  OR windows {d} ({i + 1}/{len(dates)}, {h.calls} calls)", flush=True)


def pull_daily(store: Store, h: AlpacaHistory, symbols: list[str], start: date, end: date) -> None:
    todo = [s for s in symbols if not store.daily_path(s).exists()]
    for grp in _chunks(sorted(todo), 50):
        got = h.bars(grp, "1Day", f"{start}T00:00:00Z", f"{end}T23:59:59Z")
        for s in grp:
            store.write_daily(s, got.get(s, []))
        print(f"  daily bars: {len(grp)} symbols ({h.calls} calls)", flush=True)


def pull_candidate_days(store: Store, h: AlpacaHistory, data, sp500, extras, dates: list[date]) -> None:
    loose = {**PRIMARY, **LOOSEST}
    hist: list[dict] = []
    for i, d in enumerate(dates):
        orb = store.read_bars(store.or_path(d)) if store.or_path(d).exists() else {}
        if not store.day_path(d).exists():
            uni = universe_for_day(d, data, sp500, extras, loose)
            res = scan_day(uni, orb, hist[-14:], loose)
            syms = [r.symbol for r in res.picks]
            got = h.bars(syms, "1Min", _iso(d, SCAN), _iso(d, 16 * 60)) if syms else {}
            store.write_bars(store.day_path(d), {s: [b for b in bs if SCAN <= b["t"] < 16 * 60] for s, bs in got.items()})
        hist.append({s: sum(b["v"] for b in bs) for s, bs in orb.items()})
        if i % 100 == 0:
            print(f"  candidate days {d} ({i + 1}/{len(dates)}, {h.calls} calls)", flush=True)


# ------------------------------------------------------------------ point-in-time universe and scan
_FEATS: dict[int, tuple] = {}


def _features(data: dict[str, list[dict]]) -> dict[str, dict[date, dict]]:
    """Per symbol and date: ATR14 through the prior close, prior 20-day mean dollar volume, prior close, and the
    number of prior daily bars. Only data before the date is used."""
    key = id(data)
    if key in _FEATS and _FEATS[key][0] is data:       # the id alone can be reused by a new dict
        return _FEATS[key][1]
    out = {}
    for sym, bars in data.items():
        feats, trs, atr, dv = {}, [], None, []
        for i, b in enumerate(bars):
            feats[b["d"]] = {"atr": atr, "dv20": sum(dv[-20:]) / 20 if len(dv) >= 20 else None,
                             "close": bars[i - 1]["c"] if i else None, "n": i}
            if i:
                pc = bars[i - 1]["c"]
                tr = max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc))
                trs.append(tr)
                if len(trs) == 14:
                    atr = sum(trs) / 14
                elif len(trs) > 14:
                    atr = (atr * 13 + tr) / 14
            dv.append(b["c"] * b["v"])
        out[sym] = feats
    _FEATS[key] = (data, out)
    return out


def universe_for_day(today: date, data, sp500, extras, cfg) -> dict[str, dict]:
    u = cfg["universe"]
    feats = _features(data)
    ok = {s: f[today] for s, f in feats.items() if today in f and f[today]["n"] >= 20 and f[today]["dv20"]}
    top = sorted((s for s in ok if s in sp500), key=lambda s: -ok[s]["dv20"])[:u["top_sp500_by_dollar_vol"]]
    names = set(top) | {s for s in extras if s in ok}
    return {s: {"atr": ok[s]["atr"], "dv20": ok[s]["dv20"], "close": ok[s]["close"]} for s in names
            if ok[s]["close"] >= u["min_price"] and ok[s]["atr"] is not None and ok[s]["atr"] >= u["min_atr"]
            and ok[s]["dv20"] >= u["min_dollar_vol_20d"]}


def scan_day(uni: dict, or_bars: dict, hist: list[dict], cfg) -> F.ScanResult:
    rows = [F.scan_row(s, info, or_bars.get(s, []), [h[s] for h in hist if s in h]) for s, info in uni.items()]
    return F.rank_candidates([r for r in rows if r is not None], cfg)


# ------------------------------------------------------------------ one day
def simulate_day(picks: list, bars_by_sym: dict, cfg, slip_bp: float = 2.0, half_day: bool = False) -> list[dict]:
    """Section 3 on 1-minute bars: buy-stop at the OR high (limit x1.0005) until 10:30 ET, stop at fill - frac x ATR,
    exit at the 15:55 ET open (12:55 on half-days), daily loss halts the book and flattens it. One entry per name."""
    ex_t = F.exit_time_et(cfg, half_day)
    exit_m = ex_t.hour * 60 + ex_t.minute
    by = {s: {b["t"]: b for b in bars_by_sym.get(s, [])} for s in [p.symbol for p in picks]}
    armed = {p.symbol: p for p in picks}
    sends: dict[str, int] = defaultdict(int)
    pos: dict[str, dict] = {}
    last: dict[str, float] = {}
    trades, realized, halted = [], 0.0, False

    def close(sym, px, why):
        nonlocal realized
        p = pos.pop(sym)
        pnl = (px - p["entry"]) * p["qty"]
        realized += pnl
        trades.append({**p, "exit": px, "why": why, "pnl": pnl, "bp": (px / p["entry"] - 1) * 1e4,
                       "r": (px - p["entry"]) / (p["entry"] - p["stop"])})

    for m in range(SCAN, exit_m + 1):
        for p in picks:
            s = p.symbol
            b = by[s].get(m)
            if s in pos and m == exit_m:
                close(s, F._dn(b["o"] if b else last.get(s, pos[s]["entry"]), slip_bp), f"exit {ex_t:%H:%M}")
                continue
            if b is None or m == exit_m:
                continue
            last[s] = b["c"]
            if s in pos:
                px = F.bar_stop_fill(b, pos[s]["stop"], slip_bp)
                if px is not None:
                    close(s, px, "stop")
            elif s in armed and not halted and m < CUTOFF and len(pos) < cfg["max_positions"] and b["h"] > p.or_high:
                limit = F.entry_limit(p.or_high)
                qty = F.shares_for(limit, limit - cfg["stop_atr_frac"] * p.atr, cfg)
                if qty < 1:
                    armed.pop(s)
                    continue
                got = F.bar_entry_then_stop(b, p.or_high, limit, p.atr, cfg, slip_bp)
                if got is None:
                    sends[s] += 1
                    if sends[s] >= 3:
                        armed.pop(s)
                    continue
                armed.pop(s)
                fill, stop, out = got
                pos[s] = {"symbol": s, "rank": p.rank, "rvol5": p.rvol5, "entry": fill, "stop": stop, "qty": qty,
                          "entry_t": m}
                if out is not None:
                    close(s, out, "stop")
            if not halted and realized <= -abs(cfg["daily_loss"]):
                halted = True
                for o in list(pos):
                    close(o, F._dn(last.get(o, pos[o]["entry"]), slip_bp), "daily loss halt")
    for o in list(pos):                                           # data ended early: flat at the last print
        close(o, F._dn(last.get(o, pos[o]["entry"]), slip_bp), "end of data")
    return trades


# ------------------------------------------------------------------ stats
def clustered_t(xs: list[float], groups: list) -> float | None:
    n = len(xs)
    g = defaultdict(float)
    if n < 2:
        return None
    m = sum(xs) / n
    for x, k in zip(xs, groups):
        g[k] += x - m
    G = len(g)
    if G < 2:
        return None
    var = G / (G - 1) * sum(v * v for v in g.values()) / (n * n)
    return m / math.sqrt(var) if var > 0 else None


def summarize(trades: list[dict], all_days: list[date], balance: float = BALANCE) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0}
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    by_day, by_month = defaultdict(float), defaultdict(float)
    for t in trades:
        by_day[t["day"]] += t["pnl"]
        by_month[f"{t['day']:%Y-%m}"] += t["pnl"]
    series = [by_day.get(d, 0.0) for d in all_days] or [0.0]
    mu = sum(series) / len(series)
    sd = math.sqrt(sum((x - mu) ** 2 for x in series) / max(1, len(series) - 1))
    wd = min(by_day, key=by_day.get)
    wm = min(by_month, key=by_month.get)
    return {"trades": n, "days_traded": len(by_day), "win_rate": len(wins) / n,
            "mean_r": sum(t["r"] for t in trades) / n, "mean_bp": sum(t["bp"] for t in trades) / n,
            "pf": (sum(wins) / -sum(losses)) if sum(losses) < 0 else None,
            "t": clustered_t([t["r"] for t in trades], [t["day"] for t in trades]),
            "pnl": sum(t["pnl"] for t in trades), "ann_return": mu * 252 / balance,
            "sharpe": mu / sd * math.sqrt(252) if sd > 0 else None,
            "worst_day": {"day": str(wd), "pnl": by_day[wd]}, "worst_month": {"month": wm, "pnl": by_month[wm]}}


def passes(primary: dict, first: dict, second: dict) -> bool:
    """Section 5 pass bar: PF >= 1.1, t > 2, and positive in both 2016-2020 and 2021-2026."""
    return bool(primary.get("pf") and primary["pf"] >= 1.1 and primary.get("t") and primary["t"] > 2
                and first.get("mean_r", 0) > 0 and second.get("mean_r", 0) > 0)


# ------------------------------------------------------------------ backtest over the cache
def backtest(store: Store, data, sp500, extras, dates: list[date], cfg, slip_bp: float = 2.0) -> list[dict]:
    trades, hist = [], []
    for d in dates:
        orb = store.read_bars(store.or_path(d)) if store.or_path(d).exists() else {}
        if len(hist) >= 14 and orb and store.day_path(d).exists():
            uni = universe_for_day(d, data, sp500, extras, cfg)
            res = scan_day(uni, orb, hist[-14:], cfg)
            bars = store.read_bars(store.day_path(d))
            half = bool(bars) and max(b["t"] for bs in bars.values() for b in bs) < 15 * 60
            for t in simulate_day(res.picks, bars, cfg, slip_bp, half):
                trades.append({**t, "day": d})
        hist.append({s: sum(b["v"] for b in bs) for s, bs in orb.items()})
    return trades


def report(store: Store, data, sp500, extras, dates: list[date], out_dir: Path) -> dict:
    trades = backtest(store, data, sp500, extras, dates, PRIMARY)
    first = [t for t in trades if t["day"].year <= 2020]
    second = [t for t in trades if t["day"].year >= 2021]
    d1 = [d for d in dates if d.year <= 2020]
    d2 = [d for d in dates if d.year >= 2021]
    res = {"spec": "docs/BOOK_F_HANDOFF.md section 3 (primary, pre-registered)", "period": [str(dates[0]), str(dates[-1])],
           "primary": summarize(trades, dates), "2016_2020": summarize(first, d1), "2021_2026": summarize(second, d2),
           "ai_list_2023_2026": summarize([t for t in trades if t["day"].year >= 2023 and t["symbol"] in F.AI_LIST],
                                          [d for d in dates if d.year >= 2023]),
           "by_year": {y: summarize([t for t in trades if t["day"].year == y], [d for d in dates if d.year == y])
                       for y in sorted({d.year for d in dates})},
           "caveats": ["Survivorship bias: today's S&P 500 list is used for every year.",
                       "No macro-event scan skip (no historical event calendar).",
                       "Half-days detected from the data (last bar before 15:00 ET).",
                       "Fills on 1-minute bars: 2 bp slippage per side, no commission; entry and stop in one bar = stop."],
           "sensitivity": {}}
    res["pass"] = passes(res["primary"], res["2016_2020"], res["2021_2026"])
    for k, v in SENSITIVITY:
        cfg = {**PRIMARY, **({k: v} if k != "slip_bp" else {})}
        tr = backtest(store, data, sp500, extras, dates, cfg, slip_bp=v if k == "slip_bp" else 2.0)
        res["sensitivity"][f"{k}={v}"] = summarize(tr, dates)
    (out_dir / "strategy_f_intraday_results.json").write_text(json.dumps(res, indent=1, default=str))
    _write_md(res, out_dir / "strategy_f_intraday.md")
    _equity_png(trades, out_dir / "strategy_f_equity.png")
    return res


def _fmt(s: dict) -> str:
    if not s.get("trades"):
        return "no trades"
    f = lambda x, p=2: "n/a" if x is None else f"{x:.{p}f}"
    return (f"{s['trades']} trades, win {100 * s['win_rate']:.1f}%, mean R {f(s['mean_r'], 3)}, mean {f(s['mean_bp'], 1)} bp, "
            f"PF {f(s['pf'])}, t {f(s['t'])}, ann {100 * s['ann_return']:.1f}%, Sharpe {f(s['sharpe'])}")


def _write_md(res: dict, path: Path) -> None:
    p = res["primary"]
    verdict = "PASS" if res["pass"] else "FAIL"
    lines = [f"**{verdict}**: book F replication, {res['period'][0]} to {res['period'][1]}, primary spec after 2 bp costs. "
             + ("F runs in paper as a candidate for promotion." if res["pass"]
                else "F still runs in paper, but only as a logging experiment. Not tuned to pass."), "",
             "Pass bar: PF >= 1.1, t > 2 (clustered by day), and positive in both 2016-2020 and 2021-2026.", "",
             f"- Primary: {_fmt(p)}", f"- 2016-2020: {_fmt(res['2016_2020'])}", f"- 2021-2026: {_fmt(res['2021_2026'])}",
             f"- AI/memory list, 2023-2026: {_fmt(res['ai_list_2023_2026'])}"]
    if p.get("trades"):
        lines.append(f"- Worst day {p['worst_day']['day']} ${p['worst_day']['pnl']:.0f}; worst month "
                     f"{p['worst_month']['month']} ${p['worst_month']['pnl']:.0f}")
    lines += ["", "By year:", ""] + [f"- {y}: {_fmt(s)}" for y, s in res["by_year"].items()]
    lines += ["", "Sensitivity (reported, never selected from):", ""] + [f"- {k}: {_fmt(s)}" for k, s in res["sensitivity"].items()]
    lines += ["", "Caveats:", ""] + [f"- {c}" for c in res["caveats"]]
    path.write_text("\n".join(lines) + "\n")


def _equity_png(trades: list[dict], path: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: skipping the equity curve PNG (pip install matplotlib)")
        return
    by_day = defaultdict(float)
    for t in trades:
        by_day[t["day"]] += t["pnl"]
    ds = sorted(by_day)
    eq, x = [], BALANCE
    for d in ds:
        x += by_day[d]
        eq.append(x)
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(ds, eq, lw=1)
    ax.set_title("Book F replication: paper equity at section 3 sizing ($10k start)")
    ax.set_ylabel("$")
    fig.tight_layout()
    fig.savefig(path, dpi=120)


# ------------------------------------------------------------------ main
def load_constituents(store: Store) -> set[str]:
    p = store.root / "constituents.csv"
    if not p.exists():
        import httpx
        r = httpx.get(CONSTITUENTS_URL, timeout=60)
        r.raise_for_status()
        Store._atomic(p, r.content)
    with open(p) as f:
        return {row["Symbol"].strip() for row in csv.DictReader(f)}


def main() -> None:
    from agentdesk.config import _load_dotenv
    _load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-04")
    ap.add_argument("--end", default="2026-09-25")
    ap.add_argument("--data", default=str(ROOT / "research" / "data" / "f_intraday"))
    ap.add_argument("--pull-only", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    store = Store(Path(a.data))
    sp500 = load_constituents(store)
    extras = list(F.AI_LIST)
    symbols = sorted(sp500 | set(extras) | {"SPY"})
    h = AlpacaHistory(HttpxGet()) if not a.report_only else None
    if h:
        print(f"Daily bars for {len(symbols)} symbols ...", flush=True)
        pull_daily(store, h, symbols, start - timedelta(days=120), end)
    data = {s: store.read_daily(s) for s in symbols if store.daily_path(s).exists()}
    dates = [b["d"] for b in data.get("SPY", []) if start <= b["d"] <= end]
    if not dates:
        raise SystemExit("No SPY daily bars cached; run without --report-only first.")
    names = sorted((sp500 | set(extras)) & set(data))
    if h:
        print(f"Opening-range windows for {len(names)} names over {len(dates)} sessions ...", flush=True)
        pull_or_windows(store, h, names, dates)
        print("Candidate full days ...", flush=True)
        pull_candidate_days(store, h, data, sp500, extras, dates)
        print(f"Pull complete: {h.calls} Alpaca calls this run.", flush=True)
    if a.pull_only:
        return
    res = report(store, data, sp500, extras, dates, ROOT / "research")
    print((ROOT / "research" / "strategy_f_intraday.md").read_text())
    print(f"Daily CSVs for the strategy_f.py rerun: {store.root / 'daily'}")
    print(f"verdict: {'PASS' if res['pass'] else 'FAIL'}")


if __name__ == "__main__":
    main()
