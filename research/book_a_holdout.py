"""Book A variants on the holdout (book_a_variants_prereg.md section 5): real SPY data and real 0DTE quotes.

Runs on Evan's Mac, where the data lives (all read-only, no broker, no orders):
  data/spy_1m/*.parquet                  SPY SIP 1-minute bars (research/bd_real_quotes.py fetch-spy)
  data/thetadata/spy_0dte/<date>.parquet ThetaData SPY 0DTE 1-minute NBBO quotes (research/fetch_thetadata_spy0dte.py)
  ~/.agentdesk/cache-iex-vs-sip/         SIP prints of the 60-session IEX vs SIP study (research/iex_vs_sip.py)

Modes
  quotes  2020-06-01 onward (never touched by the 2005-2020 run): SPY SIP 1-minute bars through the unchanged
          Engine, 1m trigger only (every entry a SWING), option bid/ask from ThetaData.
  ticks   the 60 cached SIP sessions: SIP prints cleaned as in iex_vs_sip.py, real 144-print bars, full spec
          (1m + 144t, SWING and SCALP), option bid/ask from ThetaData. This is where v5 (SWING only) is tested.

Quotes: an option is priced at the ThetaData row of the minute BEFORE the current one, so a quote is never from
the future whichever end of the minute ThetaData stamps it with (it can be up to two minutes old). A contract with
no quote that day can't be entered; the engine skips it.
Fills (same as book_a_variants.py): mid1 = mid -/+ 1c inside the touch; taker = bid/ask.

    python research/book_a_holdout.py quotes --variants baseline,v4_slow_exits
    python research/book_a_holdout.py ticks --variants baseline,v5_swing_only,v4_slow_exits
    python research/book_a_holdout.py quotes --combo v1_rsi_no_cap,v4_slow_exits     # adds v6_combo
Round 2 variants (r2a_stop35, r2b_spy_stop, r2c_itm1) are frozen in research/book_a_round2_prereg.md.
Writes research/book_a_holdout_out/<mode>/{trades.csv, daily.csv, summary.json} (small; commit them).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))

import book_a_variants as bav  # noqa: E402  (also installs the put-mirroring exit plan)
from agentdesk.backtest import ReplayFeed, _row  # noqa: E402
from agentdesk.bars import Bar, Trade  # noqa: E402
from agentdesk.bus import Bus  # noqa: E402
from agentdesk import engine as engine_mod  # noqa: E402
from agentdesk.config import load_config  # noqa: E402
from agentdesk.exits import ExitIntent  # noqa: E402
from agentdesk.feeds.base import Quote, QuoteSource  # noqa: E402
from agentdesk.journal import Journal  # noqa: E402

ET = ZoneInfo("America/New_York")
HOLDOUT_START = date(2020, 6, 1)
OUT = ROOT / "research" / "book_a_holdout_out"
T_PASS = 2.33


def _swing_only(c):         # 5. SWING only: a fresh 1m cross is required, 144t-only crosses (SCALP) are skipped
    c["strategy"]["enabled_setups"] = ["SWING"]


# Round 2 (book_a_round2_prereg.md): ideas that came from the 2005-2020 results, so they are tested here only.
def _stop35(c):             # r2a: premium stop -35% instead of -20%
    c["exits"]["stop_loss_pct"] = 0.35


def _spy_stop(c):           # r2b: no premium stop; sell everything when SPY trades 0.10% against the entry price
    c["exits"]["stop_loss_pct"] = 0.99
    c["exits"]["spy_stop_pct"] = 0.001


def _itm1(c):               # r2c: 1 ITM strike all day (no OTM, no level step-out)
    c["strikes"]["schedule"] = [{"from": "08:30", "base": -1, "max": -1}]


VARIANTS = dict(bav.VARIANTS, v5_swing_only=[_swing_only], r2a_stop35=[_stop35], r2b_spy_stop=[_spy_stop],
                r2c_itm1=[_itm1])
_FEED: list = [None]        # the session's feed, for r2b's SPY stop (one session at a time per process)


class SpyStopPlan(bav.MirrorExitPlan):
    """r2b: exit when SPY is spy_stop_pct below the entry price (above it for a put). Off unless the key is set."""

    def __init__(self, ecfg, pos):
        super().__init__(ecfg, pos)
        self.spy_stop = ecfg.get("spy_stop_pct")
        self.spot0 = _FEED[0].px if (_FEED[0] is not None and self.spy_stop) else None

    def on_quote(self, bid, ask, now):
        if self.spot0 and self.pos.qty > 0:
            px = _FEED[0].px
            hit = px <= self.spot0 * (1 - self.spy_stop) if self.pos.contract.right == "call" \
                else px >= self.spot0 * (1 + self.spy_stop)
            if hit:
                self.pos.bid, self.pos.ask = bid, ask
                self.pos.mark = round((bid + ask) / 2, 3) if ask > 0 else bid
                return ExitIntent(self.pos.qty, f"SPY stop {self.spy_stop:.2%}", urgent=True)
        return super().on_quote(bid, ask, now)


engine_mod.ExitPlan = SpyStopPlan


def variant_cfg(base, name: str, combo: list[str] | None, ticks: bool):
    import copy
    c = copy.deepcopy(base)
    c["crew"]["enabled"] = False
    if ticks:
        c["strategy"]["tick_bar_effective"] = c["strategy"]["tick_bar_size"]     # real 144-print bars on SIP
    else:
        c["strategy"]["trigger_timeframes"] = ["1m"]
    for v in (combo if name == "v6_combo" else [name]):
        for f in VARIANTS[v]:
            f(c)
    return c


# ---------------------------------------------------------------- real quotes
class ThetaQuotes(QuoteSource):
    name = "thetadata"

    def __init__(self, feed: ReplayFeed, panel):
        self.feed, self.panel = feed, panel

    async def quote(self, c) -> Quote | None:
        t = datetime.fromtimestamp(self.feed.t, ET)
        m = min(max(t.hour * 60 + t.minute - 1, 570), 960)       # the previous minute's row
        ba = self.panel.ba("C" if c.right == "call" else "P", float(c.strike), m)
        if ba is None or ba[1] <= 0:
            return None
        return Quote(ba[0], ba[1], self.feed.t)


def load_panel(qdir: Path, d: date):
    import pandas as pd
    from bd_real_quotes import QuotePanel, normalize
    f = qdir / f"{d}.parquet"
    return QuotePanel(normalize(pd.read_parquet(f))) if f.exists() else None


def load_spy_1m(folder: Path) -> dict[date, list[Bar]]:
    import pandas as pd
    df = pd.concat([pd.read_parquet(f) for f in sorted(folder.glob("*.parquet"))], ignore_index=True)
    df = df[(df.minute >= 570) & (df.minute < 960)]
    out: dict[date, list[Bar]] = {}
    for d, g in df.groupby("date", sort=True):
        d = d if isinstance(d, date) else pd.Timestamp(d).date()
        base = datetime.combine(d, time(0), ET).timestamp()
        out[d] = [Bar("1m", base + m * 60, o, h, l, c, float(max(v, 1)), 0, base + m * 60 + 60)
                  for m, o, h, l, c, v in zip(g.minute, g.o, g.h, g.l, g.c, g.v)]
    return out


async def _aiter(ts, px, sz):
    for a, b, c in zip(ts, px, sz):
        yield Trade(float(a), float(b), float(c))


async def run_session(cfg, day, hist, bars, trades, panel, fill) -> list:
    feed = ReplayFeed(day, hist, bars, _aiter(*trades) if trades is not None else None)
    _FEED[0] = feed
    quotes = ThetaQuotes(feed, panel)
    eng = bav.MirrorEngine(cfg, feed, quotes, bav.FillBroker(quotes, fill), Bus(), Journal(None), "backtest")
    await eng.run()
    for pos, plan in list(eng.open):
        q = await quotes.quote(pos.contract)
        pos.realized += ((q.bid if q else 0) - pos.entry) * 100 * pos.qty
        pos.qty, pos.status, pos.exit_reason = 0, "closed", "eod mark"
        eng.closed.append(pos)
    return eng.closed


def tstat(xs):
    return bav.tstat(xs)


def summarize(daily, rows, base_daily) -> dict:
    nets = [d[1] for d in daily]
    m, t = tstat(nets)
    wins = sum(r["net"] for r in rows if r["net"] > 0)
    losses = -sum(r["net"] for r in rows if r["net"] <= 0)
    s = {"sessions": len(daily), "trades": len(rows), "trades_per_day": round(len(rows) / max(1, len(daily)), 2),
         "net": round(sum(nets), 2), "mean_day": round(m, 2), "t_day": round(t, 2),
         "per_trade": round(sum(nets) / len(rows), 2) if rows else 0.0,
         "win_rate": round(sum(1 for r in rows if r["net"] > 0) / len(rows), 3) if rows else 0.0,
         "profit_factor": round(wins / losses, 2) if losses > 0 else None}
    if base_daily:
        diffs = [d[1] - base_daily[d[0]] for d in daily if d[0] in base_daily]
        dm, dt = tstat(diffs)
        s["vs_baseline_mean_day"], s["vs_baseline_t"] = round(dm, 2), round(dt, 2)
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["quotes", "ticks"])
    ap.add_argument("--variants", default="baseline")
    ap.add_argument("--combo", default="", help="comma list of variants for v6_combo")
    ap.add_argument("--spy", type=Path, default=ROOT / "data" / "spy_1m")
    ap.add_argument("--quotes", type=Path, default=ROOT / "data" / "thetadata" / "spy_0dte")
    ap.add_argument("--cache", default="~/.agentdesk/cache-iex-vs-sip")
    ap.add_argument("--days", type=int, default=0, help="only the first N sessions (plumbing check)")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()

    ticks = a.mode == "ticks"
    names = [x for x in a.variants.split(",") if x]
    combo = [x for x in a.combo.split(",") if x]
    if combo:
        names.append("v6_combo")
    if "baseline" not in names:
        names.insert(0, "baseline")
    base = load_config(ROOT / "config.yaml")
    warm = base["data"]["history_days"]
    bars = load_spy_1m(a.spy)
    all_days = sorted(d for d, b in bars.items() if len(b) >= 370)
    if ticks:
        import numpy as np
        from iex_vs_sip import clean_trades
        cache = Path(a.cache).expanduser()
        days = sorted(date.fromisoformat(f.stem.rsplit("_", 1)[1]) for f in cache.glob("SPY_trades_v2_sip_*.npz"))
    else:
        days = [d for d in all_days if d >= HOLDOUT_START]
    days = [d for d in days if (a.quotes / f"{d}.parquet").exists() and d in bars and all_days.index(d) >= warm]
    if a.days:
        days = days[:a.days]
    print(f"{a.mode}: {len(days)} sessions {days[0]}..{days[-1]}, variants {names}", flush=True)

    res = {n: {f: {"rows": [], "daily": []} for f in bav.FILLS} for n in names}
    for k, d in enumerate(days):
        i = all_days.index(d)
        hist = [b for dd in all_days[i - warm:i] for b in bars[dd]]
        panel = load_panel(a.quotes, d)
        trades = None
        if ticks:
            z = np.load(cache / f"SPY_trades_v2_sip_{d}.npz")
            trades, _ = clean_trades(z["ts"], z["px"], z["sz"], z["c"])
        for n in names:
            cfg = variant_cfg(base, n, combo, ticks)
            for f in bav.FILLS:
                closed = asyncio.run(run_session(cfg, d, hist, None if ticks else bars[d], trades, panel, f))
                for p in closed:
                    r = _row(p, d)
                    r["side"] = p.contract.right
                    res[n][f]["rows"].append(r)
                res[n][f]["daily"].append((str(d), round(sum(p.realized - p.fees for p in closed), 2), len(closed)))
        if (k + 1) % 25 == 0:
            print(f"  {k + 1}/{len(days)} {d}", flush=True)

    out = a.out / a.mode
    out.mkdir(parents=True, exist_ok=True)
    summary = {"mode": a.mode, "sessions": len(days), "first": str(days[0]), "last": str(days[-1]),
               "combo": combo, "variants": {}}
    for n in names:
        st = {f: summarize(res[n][f]["daily"], res[n][f]["rows"],
                           None if n == "baseline" else {x[0]: x[1] for x in res["baseline"][f]["daily"]})
              for f in bav.FILLS}
        ok = {"mid1_t": st["mid1"]["t_day"] >= T_PASS, "taker_positive": st["taker"]["mean_day"] > 0}
        ok["pass"] = all(ok.values())
        summary["variants"][n] = {"stats": st, "pass": ok}
        for f in bav.FILLS:
            x = st[f]
            print(f"{n:15s} {f:5s} trades/day {x['trades_per_day']:5.2f}  net {x['net']:+9.0f}  $/day {x['mean_day']:+7.2f} "
                  f"t {x['t_day']:+5.2f}  $/trade {x['per_trade']:+6.2f}  vs base t {x.get('vs_baseline_t', 0):+5.2f}")
        print(n, "PASS" if ok["pass"] else "fail", flush=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    with open(out / "daily.csv", "w") as fh:
        fh.write("variant,fill,day,net,trades\n")
        for n in names:
            for f in bav.FILLS:
                for x in res[n][f]["daily"]:
                    fh.write(f"{n},{f},{x[0]},{x[1]},{x[2]}\n")
    with open(out / "trades.csv", "w", newline="") as fh:
        w = None
        for n in names:
            for f in bav.FILLS:
                for r in res[n][f]["rows"]:
                    r = {"variant": n, "fill": f, **r}
                    if w is None:
                        w = csv.DictWriter(fh, fieldnames=list(r))
                        w.writeheader()
                    w.writerow(r)
    print(f"-> {out}")


if __name__ == "__main__":
    main()
