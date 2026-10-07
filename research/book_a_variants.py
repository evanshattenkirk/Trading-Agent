"""Book A variants: does any pre-registered single change give A more trades AND a positive expectancy?

Rules, variants and the pass bar are frozen in research/book_a_variants_prereg.md (committed before any result).
Each variant replays the same sessions through the unchanged live Engine (agentdesk/engine.py), 1m trigger only
(the backtest's default mode: 144t bars can't be built from 1-minute data, so every entry is a SWING).

Data (research/fetch_data.sh, then research/load_oanda.py):
  S&P 500 CFD 1-minute bars 2005-01 .. 2020-05 (data/spx_rth_1m.pkl). Each session and its warm-up history are
  re-based so the prior close = 765 (SPY today), so $1 strikes mean what they mean now.
  VIX daily closes (data/vix.csv). Option IV for a session = prior VIX close x 0.80, fed to the backtest's
  Black-Scholes model with its 0DTE skew. Only information known at entry.
  NOTE (2026-10-07): that IV is used on the calendar clock (time left / 365 days), not HANDOFF section 7's
  trading-day clock, so 0DTE calls price at about half of real (ATM $1.10 vs $2.51 at S=765, VIX 16, 09:30 ET):
  the "cheap-option model". backtest.ModelQuotes now defaults to the trading clock; this script pins
  clock="calendar" so the committed tables reproduce. See book_a_variants.md.

Fills (two runs per variant):
  mid1   buys at mid + 1c (never above the ask), sells at mid - 1c (never below the bid)
  taker  buys at the ask, sells at the bid
  Fees: sizing.fee_per_contract per contract per side, as the engine charges.

Puts: the engine only sends DOWN crosses to the exit plans, so a put would never get its cross-back exit. For
variant 2 this script mirrors it: a put's plan ignores down crosses and is sent UP crosses instead, with "ripping"
mirrored (5m histogram falling, price below VWAP, 5m below signal). The engine itself is not changed.

    python research/book_a_variants.py                  # all variants, both periods, both fill models
    python research/book_a_variants.py --days 20 --variants baseline     # plumbing check
Writes research/book_a_variants_out/{trades.csv, daily.csv, summary.json} and book_a_variants.md.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import json
import math
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentdesk import engine as engine_mod  # noqa: E402
from agentdesk.backtest import ModelQuotes, ReplayFeed, _row  # noqa: E402
from agentdesk.bars import Bar  # noqa: E402
from agentdesk.brokers.base import OrderResult  # noqa: E402
from agentdesk.brokers.paper import PaperBroker  # noqa: E402
from agentdesk.bus import Bus  # noqa: E402
from agentdesk.config import load_config  # noqa: E402
from agentdesk.exits import ExitPlan  # noqa: E402
from agentdesk.journal import Journal  # noqa: E402

DATA = ROOT / "research" / "data"
OUT = ROOT / "research" / "book_a_variants_out"
REBASE = 765.0
IV_SCALE = 0.80
WARM_DAYS = 5
PERIODS = {"in_sample": (date(2005, 1, 1), date(2014, 12, 31)), "out_of_sample": (date(2015, 1, 1), date(2020, 5, 31))}
T_PASS = 2.33           # one-sided p ~ 0.01 = 0.05 / 5 single-change variants (Bonferroni)


# ---------------------------------------------------------------- variants (frozen in the pre-registration)
def _rsi_no_cap(c):         # 1. no RSI band on 15m/5m (the 70 cap is what binds when their MACD is bullish)
    c["strategy"]["rsi_check_timeframes"] = ["1m"]


def _puts(c):               # 2. puts mirror everything
    c["strategy"]["allow_puts"] = True


def _filter_15m(c):         # 3. the 15m MACD filter only
    c["strategy"]["filter_timeframes"] = ["15m"]


def _slow_exits(c):         # 4. cross-back exits on the 5m only (before and after a scale)
    c["exits"]["swing"]["exit_on_cross_back"] = "5m"
    c["exits"]["scalp"]["exit_on_cross_back"] = "5m"


VARIANTS = {
    "baseline": [],
    "v1_rsi_no_cap": [_rsi_no_cap],
    "v2_puts": [_puts],
    "v3_filter_15m": [_filter_15m],
    "v4_slow_exits": [_slow_exits],
}
SINGLE = ["v1_rsi_no_cap", "v2_puts", "v3_filter_15m", "v4_slow_exits"]
FILLS = ("mid1", "taker")


def variant_cfg(base, name: str, combo: list[str] | None = None):
    c = copy.deepcopy(base)
    c["crew"]["enabled"] = False
    c["strategy"]["trigger_timeframes"] = ["1m"]
    for v in (combo if name == "v6_combo" else [name]):
        for f in VARIANTS[v]:
            f(c)
    return c


# ---------------------------------------------------------------- fills and put mirroring
class FillBroker(PaperBroker):
    """Every order fills at once: mid -/+ 1c inside the touch (mid1), or at the touch (taker)."""

    def __init__(self, quotes, model: str):
        super().__init__(quotes)
        self.model = model

    async def submit(self, contract, side: str, qty: int, limit: float, now: float) -> OrderResult:
        q = await self.quotes.quote(contract)
        if q is None:
            return OrderResult("rejected", message="no quote")
        if self.model == "taker":
            px = q.ask if side == "buy" else q.bid
        else:
            mid = (q.bid + q.ask) / 2
            px = min(q.ask, mid + 0.01) if side == "buy" else max(q.bid, mid - 0.01)
        return OrderResult("filled", qty, round(max(px, 0.0), 2), "bt")


class MirrorExitPlan(ExitPlan):
    """A put's plan reacts only to UP crosses (sent by MirrorEngine as mirrored=True); calls are unchanged."""

    def on_cross_down(self, tf, ripping, mirrored: bool = False):
        if (self.pos.contract.right == "put") != mirrored:
            return None
        return super().on_cross_down(tf, ripping)


class MirrorEngine(engine_mod.Engine):
    async def _on_bar(self, b: Bar) -> None:
        seen = []
        orig = self.sig.on_bar_close

        def spy(bar):
            ev = orig(bar)
            seen.append(ev)
            return ev

        self.sig.on_bar_close = spy
        try:
            await super()._on_bar(b)
        finally:
            self.sig.on_bar_close = orig
        cross = seen[0] if seen else None
        if cross is None or cross.direction != "up":
            return
        st = self.sig.tf["5m"]
        m5 = st.live()[0]
        ripping = bool(m5 and st.last and st.prev and m5.hist <= st.last.hist <= st.prev.hist and not m5.bull
                       and self.price is not None and self.vwap.value is not None and self.price < self.vwap.value)
        for pos, plan in list(self.open):
            if pos.contract.right == "put":
                intent = plan.on_cross_down(cross.tf, ripping, mirrored=True)
                if intent:
                    await self._run(self.exit(pos, plan, intent, cross.ts), "exit")


engine_mod.ExitPlan = MirrorExitPlan       # the engine builds plans by this name; calls behave exactly as before


# ---------------------------------------------------------------- data
def load_sessions():
    import pandas as pd
    df = pd.read_pickle(DATA / "spx_rth_1m.pkl")
    vix = pd.read_csv(DATA / "vix.csv")
    vix_close = {date.fromisoformat(d): float(c) for d, c in zip(vix["DATE"], vix["CLOSE"])}
    sessions = []
    for day, g in df.groupby("day", sort=True):
        ts = g["et"].map(lambda x: x.timestamp()).to_numpy()
        sessions.append((day, ts, g["open"].to_numpy(), g["high"].to_numpy(), g["low"].to_numpy(),
                         g["close"].to_numpy(), g["volume"].to_numpy()))
    vdays = sorted(vix_close)
    return sessions, vix_close, vdays


def prior_vix(vix_close, vdays, day: date) -> float | None:
    import bisect
    i = bisect.bisect_left(vdays, day) - 1
    return vix_close[vdays[i]] if i >= 0 else None


def bars_of(sess, k: float) -> list[Bar]:
    _, ts, o, h, l, c, v = sess
    return [Bar("1m", float(t), o[i] * k, h[i] * k, l[i] * k, c[i] * k, float(max(v[i], 1)), 0, float(t) + 60)
            for i, t in enumerate(ts)]


# ---------------------------------------------------------------- one session
async def run_session(cfg, day: date, hist: list[Bar], bars: list[Bar], iv: float, fill: str) -> list:
    feed = ReplayFeed(day, hist, bars)
    quotes = ModelQuotes(feed, iv, clock="calendar")     # the committed tables' (cheap-option) clock; see the docstring
    eng = MirrorEngine(cfg, feed, quotes, FillBroker(quotes, fill), Bus(), Journal(None), "backtest")
    await eng.run()
    for pos, plan in list(eng.open):            # anything left is marked at the bid
        q = await quotes.quote(pos.contract)
        pos.realized += ((q.bid if q else 0) - pos.entry) * 100 * pos.qty
        pos.qty, pos.status, pos.exit_reason = 0, "closed", "eod mark"
        eng.closed.append(pos)
    return eng.closed


def run_chunk(job):
    """One (variant, fill) over a list of session indices. Runs in a worker process."""
    name, combo, fill, idxs = job
    base = load_config(ROOT / "config.yaml")
    cfg = variant_cfg(base, name, combo)
    sessions, vix_close, vdays = _CACHE["data"]
    rows, daily = [], []
    for i in idxs:
        day = sessions[i][0]
        v = prior_vix(vix_close, vdays, day)
        k = REBASE / sessions[i - 1][5][-1]
        hist = [b for j in range(i - WARM_DAYS, i) for b in bars_of(sessions[j], k)]
        closed = asyncio.run(run_session(cfg, day, hist, bars_of(sessions[i], k), v / 100 * IV_SCALE, fill))
        for p in closed:
            r = _row(p, day)
            r["side"] = p.contract.right
            rows.append(r)
        daily.append((str(day), round(sum(p.realized - p.fees for p in closed), 2), len(closed)))
    return name, fill, rows, daily


_CACHE: dict = {}


def _init_worker():
    _CACHE["data"] = load_sessions()


# ---------------------------------------------------------------- stats
def tstat(xs: list[float]) -> tuple[float, float]:
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else 0.0), 0.0
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m, (m / (sd / math.sqrt(n)) if sd > 0 else 0.0)


def period_of(day: str) -> str | None:
    d = date.fromisoformat(day)
    for p, (a, b) in PERIODS.items():
        if a <= d <= b:
            return p
    return None


def stats(daily: list[tuple], rows: list[dict], base_daily: dict | None) -> dict:
    out = {}
    for p in PERIODS:
        ds = [d for d in daily if period_of(d[0]) == p]
        if not ds:
            continue
        nets = [d[1] for d in ds]
        m, t = tstat(nets)
        tr = [r for r in rows if period_of(r["day"]) == p]
        wins = sum(r["net"] for r in tr if r["net"] > 0)
        losses = -sum(r["net"] for r in tr if r["net"] <= 0)
        s = {"sessions": len(ds), "trades": len(tr), "trades_per_day": round(len(tr) / len(ds), 2),
             "net": round(sum(nets), 2), "mean_day": round(m, 2), "t_day": round(t, 2),
             "per_trade": round(sum(nets) / len(tr), 2) if tr else 0.0,
             "win_rate": round(sum(1 for r in tr if r["net"] > 0) / len(tr), 3) if tr else 0.0,
             "profit_factor": round(wins / losses, 2) if losses > 0 else None}
        if base_daily:
            diffs = [d[1] - base_daily[d[0]] for d in ds if d[0] in base_daily]
            dm, dt = tstat(diffs)
            s["vs_baseline_mean_day"], s["vs_baseline_t"] = round(dm, 2), round(dt, 2)
        by = defaultdict(list)
        for r in tr:
            by[r["exit"]].append(r["net"])
        s["by_exit"] = {k: {"n": len(v), "net": round(sum(v), 2)} for k, v in sorted(by.items())}
        out[p] = s
    return out


def passes(st: dict) -> dict:
    """Pre-registered bar (book_a_variants_prereg.md section 3)."""
    m, tk = st["mid1"], st["taker"]
    ok = {
        "is_mid1_positive": m.get("in_sample", {}).get("mean_day", -1) > 0,
        "oos_mid1_t": m.get("out_of_sample", {}).get("t_day", 0) >= T_PASS,
        "oos_taker_positive": tk.get("out_of_sample", {}).get("mean_day", -1) > 0,
    }
    ok = {k: bool(v) for k, v in ok.items()}
    ok["pass"] = all(ok.values())
    return ok


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default=",".join(list(VARIANTS) + ["v6_combo"]))
    ap.add_argument("--days", type=int, default=0, help="only the first N test sessions (plumbing check)")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    sessions, _, _ = load_sessions()
    test = [i for i in range(WARM_DAYS, len(sessions)) if period_of(str(sessions[i][0]))]
    if a.days:
        test = test[:a.days]
    names = a.variants.split(",")
    import os
    workers = a.workers or max(1, (os.cpu_count() or 2))
    chunks = [test[i::workers * 2] for i in range(workers * 2)]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    results: dict = {}
    combo = None

    def run(name, combo_list=None):
        jobs = [(name, combo_list, f, ch) for f in FILLS for ch in chunks if ch]
        with ProcessPoolExecutor(workers, initializer=_init_worker) as ex:
            for n, f, rows, daily in ex.map(run_chunk, jobs):
                r = results.setdefault(n, {}).setdefault(f, {"rows": [], "daily": []})
                r["rows"] += rows
                r["daily"] += daily
        for f in FILLS:
            r = results[name][f]
            r["daily"].sort()
            r["rows"].sort(key=lambda x: (x["day"], x["open"]))
        print(f"{datetime.now():%H:%M:%S} {name} done", flush=True)

    for n in names:
        if n == "v6_combo":
            continue
        run(n)
    summary = {}
    for n in [x for x in names if x != "v6_combo"]:
        st = {f: stats(results[n][f]["daily"], results[n][f]["rows"],
                       None if n == "baseline" else {d[0]: d[1] for d in results["baseline"][f]["daily"]})
              for f in FILLS}
        summary[n] = {"stats": st, "pass": passes(st)}
    if "v6_combo" in names:
        # combo = the single variants that beat the baseline in-sample at mid1 (chosen on 2005-14 only)
        combo = [v for v in SINGLE if v in summary
                 and summary[v]["stats"]["mid1"].get("in_sample", {}).get("vs_baseline_mean_day", -1) > 0]
        if combo:
            run("v6_combo", combo)
            st = {f: stats(results["v6_combo"][f]["daily"], results["v6_combo"][f]["rows"],
                           {d[0]: d[1] for d in results["baseline"][f]["daily"]}) for f in FILLS}
            summary["v6_combo"] = {"members": combo, "stats": st, "pass": passes(st)}
        else:
            summary["v6_combo"] = {"members": [], "note": "no single variant beat the baseline in-sample"}

    with open(out / "trades.csv", "w", newline="") as fh:
        w = None
        for n, byf in results.items():
            for f, r in byf.items():
                for row in r["rows"]:
                    row = {"variant": n, "fill": f, **row}
                    if w is None:
                        w = csv.DictWriter(fh, fieldnames=list(row))
                        w.writeheader()
                    w.writerow(row)
    with open(out / "daily.csv", "w", newline="") as fh:
        fh.write("variant,fill,day,net,trades\n")
        for n, byf in results.items():
            for f, r in byf.items():
                for d in r["daily"]:
                    fh.write(f"{n},{f},{d[0]},{d[1]},{d[2]}\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    for n, s in summary.items():
        if "stats" not in s:
            print(n, s)
            continue
        for f in FILLS:
            for p, x in s["stats"][f].items():
                print(f"{n:15s} {f:5s} {p:13s} trades/day {x['trades_per_day']:5.2f}  net {x['net']:+10.0f}  "
                      f"$/day {x['mean_day']:+7.2f} t {x['t_day']:+5.2f}  $/trade {x['per_trade']:+6.2f}  "
                      f"vsbase t {x.get('vs_baseline_t', 0):+5.2f}")
        print(n, "PASS" if s["pass"]["pass"] else "fail", s["pass"])


if __name__ == "__main__":
    main()
