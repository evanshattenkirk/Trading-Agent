"""Book A data fidelity: free IEX feed (8-print "144t") vs full SIP tape (real 144t).

Replays the same sessions through the unchanged Engine twice and compares what book A sees:

  iexK    IEX prints, IEX 1m warm-up, 144t series built from K IEX prints, one run per --iex-ticks value
          (default 8 and 5: the original setting and the one paper runs since 2026-09-28)
  sip144  what Algo Trader Plus ($99/mo) would give: SIP prints, SIP warm-up, real 144-print bars
  iexN    diagnostic: IEX prints with the tick size re-calibrated to that day's measured IEX share
          (144 x IEX/SIP print ratio). Uses same-day information, so it only answers "would a better
          constant fix it?", not what paper would have done.

Reported per day and in total:
  - IEX share of SIP prints (overall and by half hour) and the implied tick size
  - MACD cross-up agreement on 1m / 5m / 15m / 144t against sip144 (matched within a tolerance)
  - share of RTH seconds where each timeframe's MACD bull/bear state agrees with sip144
  - entry signals (filled entries plus signals risk skipped), matched by time, and whether the setup agrees
  - trades and P&L (Black-Scholes model option prices, same model for every variant, so the delta is
    driven only by the data feed)

Needs Alpaca keys in the environment (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY). Free-plan SIP history
is allowed once it is older than 15 minutes. Read-only: no broker, no orders, PaperBroker fills only.

    python research/iex_vs_sip.py --days 10 --end 2026-09-25      # reads keys from ~/Trading-Agent/.env
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import os
import sys
import time as _time
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentdesk.backtest import ModelQuotes, ReplayFeed, _row, summarize  # noqa: E402
from agentdesk.bars import Bar, Trade  # noqa: E402
from agentdesk.brokers.paper import PaperBroker  # noqa: E402
from agentdesk.bus import Bus  # noqa: E402
from agentdesk.clock import CT, at_ct, is_rth, session_date  # noqa: E402
from agentdesk.config import load_config  # noqa: E402
from agentdesk.engine import Engine  # noqa: E402
from agentdesk.journal import Journal  # noqa: E402

TFS = ("1m", "5m", "15m", "144t")
CROSS_TOL = {"1m": 60, "5m": 300, "15m": 900, "144t": 60}   # seconds; one bar for time TFs
ENTRY_TOL = 120                                                # = strategy.confirm_window_sec


# ---------------------------------------------------------------- pure helpers (unit-tested)
def match_events(a: list[float], b: list[float], tol: float) -> list[tuple[float, float]]:
    """Greedy one-to-one match of two sorted timestamp lists; each pair is within tol seconds."""
    a, b = sorted(a), sorted(b)
    pairs, j = [], 0
    for x in a:
        while j < len(b) and b[j] < x - tol:
            j += 1
        if j < len(b) and abs(b[j] - x) <= tol:
            pairs.append((x, b[j]))
            j += 1
    return pairs


def agreement(ref: list[float], alt: list[float], tol: float) -> dict:
    """recall = share of ref events alt also has; precision = share of alt events ref has."""
    m = len(match_events(ref, alt, tol))
    union = len(ref) + len(alt) - m
    return {"ref": len(ref), "alt": len(alt), "matched": m,
            "recall": round(m / len(ref), 3) if ref else None,
            "precision": round(m / len(alt), 3) if alt else None,
            "jaccard": round(m / union, 3) if union else None,
            "median_lag_s": _median([y - x for x, y in match_events(ref, alt, tol)])}


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 4)


def state_agreement(ref: list[tuple[float, bool]], alt: list[tuple[float, bool]], start: float, end: float) -> float | None:
    """Share of [start, end) where two step functions (ts, bull) agree. Undefined before a series' first
    flip, so the comparison window opens once both have one."""
    if not ref or not alt:
        return None
    t0 = max(ref[0][0], alt[0][0], start)
    if t0 >= end:
        return None
    pts = sorted({t for t, _ in ref} | {t for t, _ in alt} | {t0, end})
    pts = [t for t in pts if t0 <= t <= end]

    def at(series, t):
        v = None
        for ts, s in series:
            if ts <= t:
                v = s
            else:
                break
        return v

    same = 0.0
    for lo, hi in zip(pts, pts[1:]):
        if at(ref, lo) == at(alt, lo):
            same += hi - lo
    return round(same / (end - t0), 4)


def print_share(iex_ts: list[float], sip_ts: list[float], day: date, bucket_min: int = 30) -> dict:
    """IEX prints as a share of SIP prints, overall and per bucket (CT clock labels)."""
    t0 = at_ct(day, time(8, 30))
    nb = int(390 / bucket_min)
    ci, cs = [0] * nb, [0] * nb
    for arr, cnt in ((iex_ts, ci), (sip_ts, cs)):
        for t in arr:
            k = int((t - t0) // (bucket_min * 60))
            if 0 <= k < nb:
                cnt[k] += 1
    buckets = {}
    for k in range(nb):
        lab = datetime.fromtimestamp(t0 + k * bucket_min * 60, CT).strftime("%H:%M")
        buckets[lab] = round(ci[k] / cs[k], 4) if cs[k] else None
    share = len(iex_ts) / len(sip_ts) if sip_ts else None
    return {"iex_prints": len(iex_ts), "sip_prints": len(sip_ts), "share": round(share, 4) if share else None,
            "implied_tick": round(144 * share, 1) if share else None, "by_bucket": buckets}


def compare_entries(ref: list[dict], alt: list[dict], tol: float = ENTRY_TOL) -> dict:
    """ref/alt: [{ts, setup}]; matched by time, then checked for the same setup."""
    rts = [e["ts"] for e in ref]
    ats = [e["ts"] for e in alt]
    pairs = match_events(rts, ats, tol)
    rmap = {e["ts"]: e for e in ref}
    amap = {e["ts"]: e for e in alt}
    same_setup = sum(1 for x, y in pairs if rmap[x]["setup"] == amap[y]["setup"])
    out = agreement(rts, ats, tol)
    out["same_setup"] = same_setup
    by = {}
    for s in ("SWING", "SCALP"):
        r = [e["ts"] for e in ref if e["setup"] == s]
        a = [e["ts"] for e in alt if e["setup"] == s]
        by[s] = agreement(r, a, tol)
    out["by_setup"] = by
    return out


# ---------------------------------------------------------------- engine replay
class RecBus(Bus):
    """Keeps the events this comparison needs; drops the dashboard stream."""
    KEEP = {"cross", "skip", "position", "trade_closed"}

    def __init__(self):
        super().__init__()
        self.events: list[dict] = []

    def emit(self, type_: str, ts: float, **data) -> None:
        if type_ in self.KEEP and (type_ != "position" or data.get("event") == "open"):
            self.events.append({"type": type_, "ts": ts, **data})


async def _aiter(ts, px, sz):
    for a, b, c in zip(ts, px, sz):
        yield Trade(float(a), float(b), float(c))


async def replay(cfg, day: date, history: list[Bar], trades: tuple, tick_size: int, iv: float) -> dict:
    cfg = copy.deepcopy(cfg)
    cfg["crew"]["enabled"] = False
    cfg["strategy"]["tick_bar_effective"] = tick_size          # series keeps the "144t" name, as in live
    feed = ReplayFeed(day, history, None, _aiter(*trades))
    quotes = ModelQuotes(feed, iv)
    bus = RecBus()
    eng = Engine(cfg, feed, quotes, PaperBroker(quotes), bus, Journal(None), "backtest")
    await eng.run()
    for pos, plan in list(eng.open):
        q = await quotes.quote(pos.contract)
        pos.realized += ((q.bid if q else 0) - pos.entry) * 100 * pos.qty
        pos.qty, pos.status, pos.exit_reason = 0, "closed", "eod mark"
        eng.closed.append(pos)
    ev = bus.events
    crosses = {tf: {"up": [e["ts"] for e in ev if e["type"] == "cross" and e["tf"] == tf and e["dir"] == "up"],
                    "state": [(e["ts"], e["dir"] == "up") for e in ev if e["type"] == "cross" and e["tf"] == tf]}
               for tf in TFS}
    signals = sorted([{"ts": e["ts"], "setup": e["pos"]["setup"], "taken": True} for e in ev if e["type"] == "position"]
                     + [{"ts": e["ts"], "setup": e["setup"], "taken": False, "why": e["why"]} for e in ev if e["type"] == "skip"],
                     key=lambda x: x["ts"])
    return {"crosses": crosses, "signals": signals, "trades": [_row(p, day) for p in eng.closed]}


def compare_day(day: date, runs: dict, share: dict) -> dict:
    ref = runs["sip144"]
    start, end = at_ct(day, time(8, 30)), at_ct(day, time(15, 0))
    out = {"day": str(day), "print_share": share, "variants": {}}
    for name, r in runs.items():
        v = {"trades": len(r["trades"]), "net": round(sum(t["net"] for t in r["trades"]), 2),
             "signals": len(r["signals"]),
             "by_setup": {s: {"n": sum(1 for t in r["trades"] if t["setup"] == s),
                              "net": round(sum(t["net"] for t in r["trades"] if t["setup"] == s), 2)} for s in ("SWING", "SCALP")}}
        if name != "sip144":
            v["cross_up"] = {tf: agreement(ref["crosses"][tf]["up"], r["crosses"][tf]["up"], CROSS_TOL[tf]) for tf in TFS}
            v["state_agree"] = {tf: state_agreement(ref["crosses"][tf]["state"], r["crosses"][tf]["state"], start, end) for tf in TFS}
            v["signals_vs_sip"] = compare_entries(ref["signals"], r["signals"])
            v["taken_vs_sip"] = compare_entries([s for s in ref["signals"] if s["taken"]], [s for s in r["signals"] if s["taken"]])
        out["variants"][name] = v
    return out


def roll_up(days: list[dict], trades: dict[str, list[dict]]) -> dict:
    """Pool per-day comparisons into totals (sums of matched/ref/alt, not averages of ratios)."""
    tot = {"days": len(days), "variants": {}}
    shares = [d["print_share"]["share"] for d in days if d["print_share"]["share"]]
    tot["iex_share_median"] = _median(shares)
    tot["iex_share_range"] = [min(shares), max(shares)] if shares else None
    tot["implied_tick_median"] = round(144 * tot["iex_share_median"], 1) if shares else None
    for name in days[0]["variants"] if days else []:
        v = {"summary": summarize(trades.get(name, []))}
        if name != "sip144":
            for key in ("cross_up",):
                v[key] = {}
                for tf in TFS:
                    ref = sum(d["variants"][name][key][tf]["ref"] for d in days)
                    alt = sum(d["variants"][name][key][tf]["alt"] for d in days)
                    m = sum(d["variants"][name][key][tf]["matched"] for d in days)
                    v[key][tf] = {"ref": ref, "alt": alt, "matched": m,
                                  "recall": round(m / ref, 3) if ref else None,
                                  "precision": round(m / alt, 3) if alt else None,
                                  "jaccard": round(m / (ref + alt - m), 3) if ref + alt - m else None}
            v["state_agree_mean"] = {tf: _mean([d["variants"][name]["state_agree"][tf] for d in days]) for tf in TFS}
            for key in ("signals_vs_sip", "taken_vs_sip"):
                ref = sum(d["variants"][name][key]["ref"] for d in days)
                alt = sum(d["variants"][name][key]["alt"] for d in days)
                m = sum(d["variants"][name][key]["matched"] for d in days)
                ss = sum(d["variants"][name][key]["same_setup"] for d in days)
                v[key] = {"ref": ref, "alt": alt, "matched": m, "same_setup": ss,
                          "recall": round(m / ref, 3) if ref else None,
                          "precision": round(m / alt, 3) if alt else None,
                          "jaccard": round(m / (ref + alt - m), 3) if ref + alt - m else None}
            ref_net = [d["variants"]["sip144"]["net"] for d in days]
            alt_net = [d["variants"][name]["net"] for d in days]
            diffs = [a - r for a, r in zip(alt_net, ref_net)]
            v["daily_net_delta_vs_sip"] = {"total": round(sum(diffs), 2), "mean": _mean(diffs), "sd": _sd(diffs),
                                           "t": _t(diffs), "days_worse": sum(1 for x in diffs if x < 0)}
        tot["variants"][name] = v
    return tot


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _sd(xs):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return round(math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1)), 2)


def _t(xs):
    s = _sd(xs)
    return round((sum(xs) / len(xs)) / (s / math.sqrt(len(xs))), 2) if s else None


# ---------------------------------------------------------------- data (Alpaca, cached)
async def fetch_trades_retry(symbol: str, start: datetime, end: datetime, feed: str):
    """Same request as feeds.alpaca.fetch_trades, with backoff on 429/5xx (a SIP day is 100+ pages)."""
    import httpx
    from agentdesk.feeds.alpaca import DATA, _headers, _ts
    token, pages = None, 0
    async with httpx.AsyncClient(headers=_headers(), timeout=60) as c:
        while True:
            params = {"start": start.isoformat(), "end": end.isoformat(), "feed": feed, "limit": 10000, "sort": "asc"}
            if token:
                params["page_token"] = token
            for attempt in range(6):
                try:
                    r = await c.get(f"{DATA}/v2/stocks/{symbol}/trades", params=params)
                except httpx.TransportError:
                    r = None
                if r is not None and r.status_code == 200:
                    break
                if r is not None and r.status_code not in (429, 500, 502, 503, 504):
                    r.raise_for_status()
                await asyncio.sleep(2 ** attempt)
            else:
                raise SystemExit(f"Alpaca trades {feed} {start.date()}: gave up after retries")
            j = r.json()
            for t in j.get("trades") or []:
                yield _ts(t["t"]), float(t["p"]), float(t["s"])
            pages += 1
            if pages % 25 == 0:
                print(f"  {start.date()} {feed}: {pages} pages", flush=True)
            token = j.get("next_page_token")
            if not token:
                return


async def load_trades(day: date, feed: str, cache: Path):
    import numpy as np
    f = cache / f"SPY_trades_{feed}_{day}.npz"
    if not f.exists():
        start = datetime.fromtimestamp(at_ct(day, time(8, 30)), timezone.utc)
        end = datetime.fromtimestamp(at_ct(day, time(15, 0)), timezone.utc)
        ts, px, sz = [], [], []
        async for a, b, c in fetch_trades_retry("SPY", start, end, feed):
            ts.append(a); px.append(b); sz.append(c)
        np.savez_compressed(f, ts=np.array(ts), px=np.array(px), sz=np.array(sz))
    d = np.load(f)
    return d["ts"], d["px"], d["sz"]


async def load_bars(feed: str, start: datetime, end: datetime, cache: Path) -> list[Bar]:
    from agentdesk.feeds.alpaca import fetch_bars_1m
    f = cache / f"SPY_1m_{feed}_{start.date()}_{end.date()}.json"
    if not f.exists():
        bars = await fetch_bars_1m("SPY", start, end, feed)
        f.write_text(json.dumps([[b.t, b.o, b.h, b.l, b.c, b.v, b.n] for b in bars]))
    return [Bar("1m", t, o, h, l, c, v, int(n), t + 60) for t, o, h, l, c, v, n in json.loads(f.read_text())]


async def main(args) -> None:
    if args.env:
        from agentdesk.config import _load_dotenv
        _load_dotenv(Path(os.path.expanduser(args.env)))     # keys stay inside this process
    cfg = load_config(args.config)
    iex_ticks = [int(x) for x in str(args.iex_ticks).split(",") if x.strip()]
    cache = Path(os.path.expanduser(args.cache))
    cache.mkdir(parents=True, exist_ok=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    warm = cfg["data"]["history_days"]
    end = datetime.combine(date.fromisoformat(args.end), time(23, 59), timezone.utc) if args.end \
        else datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=int(args.days * 1.5) + warm * 2 + 6)
    bars = {f: await load_bars(f, start, end, cache) for f in ("iex", "sip")}
    by_day = {f: defaultdict(list) for f in bars}
    for f, bs in bars.items():
        for b in bs:
            by_day[f][session_date(b.t)].append(b)
    days = sorted(by_day["sip"])
    test_days = days[warm:][-args.days:]
    per_day, trades = [], defaultdict(list)
    for d in test_days:
        t0 = _time.time()
        i = days.index(d)
        hist = {f: [b for dd in days[max(0, i - warm):i] for b in by_day[f].get(dd, [])] for f in bars}
        tr = {f: await load_trades(d, f, cache) for f in ("iex", "sip")}
        share = print_share(list(tr["iex"][0]), list(tr["sip"][0]), d)
        n_eq = max(1, round(share["implied_tick"] or iex_ticks[0]))
        runs = {"sip144": await replay(cfg, d, hist["sip"], tr["sip"], cfg["strategy"]["tick_bar_size"], args.iv)}
        for k in iex_ticks:
            runs[f"iex{k}"] = await replay(cfg, d, hist["iex"], tr["iex"], k, args.iv)
        if not args.no_diag:
            runs["iexN"] = await replay(cfg, d, hist["iex"], tr["iex"], n_eq, args.iv)
        cd = compare_day(d, runs, share)
        if "iexN" in cd["variants"]:
            cd["variants"]["iexN"]["tick"] = n_eq
        per_day.append(cd)
        for name, r in runs.items():
            trades[name] += [{**t, "variant": name} for t in r["trades"]]
        (out / "signals").mkdir(exist_ok=True)
        (out / "signals" / f"{d}.json").write_text(json.dumps({k: v["signals"] for k, v in runs.items()}, indent=1))
        v = cd["variants"]
        line = f"{d}  IEX share {share['share']:.3f} (tick~{share['implied_tick']})  " \
               f"sip144 {v['sip144']['trades']:2d} tr {v['sip144']['net']:+8.2f}"
        for k in iex_ticks:
            x = v[f"iex{k}"]
            line += f"  | iex{k} {x['trades']:2d} tr {x['net']:+8.2f} 144t recall {x['cross_up']['144t']['recall']}"
        print(line + f"  ({_time.time() - t0:.0f}s)", flush=True)
    total = roll_up(per_day, trades)
    total["run"] = {"days": [str(d) for d in test_days], "iv": args.iv, "iex_ticks": iex_ticks,
                    "options": "Black-Scholes model (same for every variant)", "generated": datetime.now(timezone.utc).isoformat()}
    (out / "per_day.json").write_text(json.dumps(per_day, indent=1))
    (out / "summary.json").write_text(json.dumps(total, indent=1))
    import csv
    rows = [t for name in trades for t in trades[name]]
    if rows:
        with open(out / "trades.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(json.dumps({k: v for k, v in total.items() if k != "variants"}, indent=1))
    for name, v in total["variants"].items():
        s = v["summary"]
        print(name, {k: s.get(k) for k in ("trades", "net", "win_rate", "profit_factor", "expectancy")},
              {k: v.get(k) for k in ("signals_vs_sip", "daily_net_delta_vs_sip")})
    print(f"-> {out}/summary.json, per_day.json, trades.csv, signals/")


def cli(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--end", default=None, help="last session YYYY-MM-DD (default: now - 20 min)")
    p.add_argument("--iv", type=float, default=0.16)
    p.add_argument("--iex-ticks", default="8,5", help="comma list of prints per '144t' bar on IEX, one run each")
    p.add_argument("--no-diag", action="store_true", help="skip the re-calibrated iexN diagnostic run")
    p.add_argument("--cache", default="~/.agentdesk/cache-iex-vs-sip")
    p.add_argument("--out", default="research/iex_vs_sip_out")
    p.add_argument("--config", default=None)
    p.add_argument("--env", default="~/Trading-Agent/.env", help="dotenv with the Alpaca keys (not printed)")
    asyncio.run(main(p.parse_args(argv)))


if __name__ == "__main__":
    cli()
