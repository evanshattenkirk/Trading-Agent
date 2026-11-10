"""R1 data that only the Mac can fetch (research/r1_retail_search_prereg.md section 3): SEC 8-K Item 2.02 report times
for the 19 retailers, and Alpaca SIP daily bars (split-adjusted) for them and SPY. SEC isn't reachable from cloud
sessions and the Alpaca keys live in the Mac's .env. Standalone research script; it changes nothing else.

    cd ~/Trading-Agent && .venv/bin/python research/fetch_retail_mac.py
    # -> research/data/x1/retail_earnings.csv, research/data/x1/retail_daily.csv

Report times use fetch_earnings_edgar.py's rules unchanged (AAPL/KO time-zone anchors, 45-day clustering, sessions),
only from 2014-01-01 and for R1's names. The anchors are pulled too and stay in the file.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from datetime import date
from pathlib import Path

# the repo root is the nearest parent holding agentdesk/ (the script also runs from a copy in research/data/x1)
ROOT = next(p for p in Path(__file__).resolve().parents if (p / "agentdesk").is_dir())
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))

import fetch_earnings_edgar as F  # noqa: E402

RETAIL = ["WMT", "COST", "HD", "LOW", "TGT", "BBY", "DG", "DLTR", "TJX", "ROST",
          "KSS", "M", "ULTA", "TSCO", "DKS", "BJ", "BURL", "AZO", "ORLY"]
FIRST = date(2014, 1, 1)
PRICE_START, PRICE_END = "2015-12-01", "2026-11-10"
OUT = ROOT / "research" / "data" / "x1"


def earnings(out: Path) -> None:
    F.FIRST = FIRST                                   # the fetcher reads its start date from this module global
    names = RETAIL + [s for s in F.ANCHORS if s not in RETAIL]
    ciks = F.ticker_ciks(F.get_json(F.TICKERS_URL), names)
    missing = sorted(set(names) - set(ciks))
    if missing:
        raise SystemExit(f"no CIK for {missing}")
    filings = {}
    for s in names:
        ids = [ciks[s]] + F.EXTRA_CIKS.get(s, [])
        filings[s] = F.merge_filings([F.all_filings(c) for c in ids])
        print(f"{s}: CIK {ids}, {len(F.results_filings(filings[s]))} 8-K 2.02 filings in total")
        time.sleep(0.2)
    by_zone = {z: [e for s in names for e in F.events_for(s, filings[s], z)] for z in ("utc", "et")}
    zone = F.pick_zone(by_zone)
    if zone is None:
        raise SystemExit("neither time-zone reading passes the AAPL/KO anchors; check EDGAR's acceptanceDateTime")
    events = sorted(by_zone[zone], key=lambda e: (e["symbol"], e["accepted_et"]))
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=F.FIELDS)
        w.writeheader()
        w.writerows(F.to_csv_rows(events))
    print(f"stamps read as {zone}; {sum(e['status'] == 'kept' for e in events)} kept reports -> {out}")


def prices(out: Path) -> None:
    from agentdesk.config import load_config          # loads .env (Alpaca keys)
    from agentdesk.feeds.f_data import alpaca_bars
    load_config()
    bars = asyncio.run(alpaca_bars(RETAIL + ["SPY"], "1Day", PRICE_START, PRICE_END, feed="sip"))
    n = 0
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "date", "close"])
        for sym in sorted(bars):
            for b in bars[sym]:
                w.writerow([sym, b["t"][:10], b["c"]])
                n += 1
    print(f"{n} daily bars for {len(bars)} symbols -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=OUT)
    ap.add_argument("--skip-prices", action="store_true")
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    earnings(a.out_dir / "retail_earnings.csv")
    if not a.skip_prices:
        prices(a.out_dir / "retail_daily.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
