"""Synthetic check of book_a_holdout.py's quote re-centring (book_a_variants.md section 4a).

Reconstructed on 2026-10-07 from section 4a's description and research/tests/test_book_a_holdout.py: the original
one-off check was never committed and its session sample was not recorded, so this script need not reproduce the
quoted -$44 / -$37 / -$41 / +$221 a day to the dollar. It tests the same thing. On the S&P 500 1-minute bars
(2005-2020, re-based to 765) each session gets model quotes stamped like ThetaData's 1-minute rows, then book A's
baseline (1m trigger, crew off, mid -+ 1c fills) runs five times:

  reference     continuous model quotes at the replay's current price (backtest.ModelQuotes, calendar clock and
                IV = prior VIX x 0.80, exactly as book_a_variants.py prices)
  stale_start   rows priced at each minute's open (start-of-minute stamp), used as is (the holdout's first, bad run)
  stale_end     rows priced at each minute's close (end-of-minute stamp), used as is
  fixed_start   the start-stamped rows through ThetaQuotes' re-centring (the fix)
  fixed_end     the end-stamped rows through ThetaQuotes' re-centring

The fix passes when fixed_* land near the reference while stale_* show a large fake profit.

    python research/book_a_recenter_check.py --days 250     # needs research/data (fetch_data.sh, load_oanda.py)

Prints $/day per run and writes research/book_a_recenter_check.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import book_a_holdout as bh  # noqa: E402  (imports book_a_variants, which installs the put-mirroring exit plan)
import book_a_variants as bav  # noqa: E402
from agentdesk.backtest import ReplayFeed  # noqa: E402
from agentdesk.bus import Bus  # noqa: E402
from agentdesk.config import load_config  # noqa: E402
from agentdesk.journal import Journal  # noqa: E402
from agentdesk.pricing import quote_from_model  # noqa: E402

RUNS = ("reference", "stale_start", "stale_end", "fixed_start", "fixed_end")
EXPIRY_MIN = 16 * 60 + 15            # SPY 0DTE trades until 16:15 ET


def make_panel(bars, iv: float, stamp: str):
    """A QuotePanel shaped like ThetaData's rows: row m holds the model quote at minute m's open (stamp "start") or
    close ("end"), for every $1 strike within $10 of the session's range, calls and puts."""
    import pandas as pd
    from datetime import datetime
    from bd_real_quotes import QuotePanel, normalize
    lo = math.floor(min(b.l for b in bars)) - 10
    hi = math.ceil(max(b.h for b in bars)) + 10
    rows = []
    for b in bars:
        t = datetime.fromtimestamp(b.t, bh.ET)
        m = t.hour * 60 + t.minute
        s, left = (b.o, (EXPIRY_MIN - m) * 60) if stamp == "start" else (b.c, (EXPIRY_MIN - m - 1) * 60)
        for k in range(lo, hi + 1):
            for r in ("call", "put"):
                bid, ask = quote_from_model(s, k, left, iv, r)
                rows.append((m * 60_000, k * 1000, r[0].upper(), bid, ask))
    return QuotePanel(normalize(pd.DataFrame(rows, columns=["ms_of_day", "strike", "right", "bid", "ask"])))


async def _run_rows(cfg, day, hist, bars, panel, recenter: bool) -> list:
    feed = ReplayFeed(day, hist, bars)
    bh._FEED[0] = feed
    quotes = bh.ThetaQuotes(feed, panel, recenter=recenter)
    eng = bav.MirrorEngine(cfg, feed, quotes, bav.FillBroker(quotes, "mid1"), Bus(), Journal(None), "backtest")
    await eng.run()
    for pos, plan in list(eng.open):
        q = await quotes.quote(pos.contract)
        pos.realized += ((q.bid if q else 0) - pos.entry) * 100 * pos.qty
        pos.qty, pos.status, pos.exit_reason = 0, "closed", "eod mark"
        eng.closed.append(pos)
    return eng.closed


def run_day(cfg, day, hist, bars, iv: float) -> dict:
    """Net $ for the session under each of RUNS."""
    net = lambda closed: round(sum(p.realized - p.fees for p in closed), 2)      # noqa: E731
    out = {"reference": net(asyncio.run(bav.run_session(cfg, day, hist, bars, iv, "mid1")))}
    for stamp in ("start", "end"):
        panel = make_panel(bars, iv, stamp)
        out[f"stale_{stamp}"] = net(asyncio.run(_run_rows(cfg, day, hist, bars, panel, recenter=False)))
        out[f"fixed_{stamp}"] = net(asyncio.run(_run_rows(cfg, day, hist, bars, panel, recenter=True)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250, help="sessions to run, spread evenly over 2005-2020")
    ap.add_argument("--out", type=Path, default=HERE / "book_a_recenter_check.json")
    a = ap.parse_args()
    sessions, vix_close, vdays = bav.load_sessions()
    test = [i for i in range(bav.WARM_DAYS, len(sessions)) if bav.period_of(str(sessions[i][0]))]
    step = max(1, len(test) // a.days) if a.days else 1
    test = test[::step][:a.days or None]
    cfg = bav.variant_cfg(load_config(HERE.parent / "config.yaml"), "baseline")
    per_day = {}
    for i in test:
        day = sessions[i][0]
        v = bav.prior_vix(vix_close, vdays, day)
        if v is None:
            continue
        k = bav.REBASE / sessions[i - 1][5][-1]
        hist = [b for j in range(i - bav.WARM_DAYS, i) for b in bav.bars_of(sessions[j], k)]
        per_day[str(day)] = run_day(cfg, day, hist, bav.bars_of(sessions[i], k), v / 100 * bav.IV_SCALE)
    summary = {r: {"mean_day": round(sum(d[r] for d in per_day.values()) / max(1, len(per_day)), 2)} for r in RUNS}
    a.out.write_text(json.dumps({"sessions": len(per_day), "summary": summary, "per_day": per_day}, indent=1))
    for r in RUNS:
        print(f"{r:12s} $/day {summary[r]['mean_day']:+8.2f}")
    print(f"{len(per_day)} sessions -> {a.out}")


if __name__ == "__main__":
    main()
