"""KP1 fetchers: MARA Pool blocks and network heights (mempool.space), Coinbase Exchange daily candles.

Run: `python research/kp1_fetch.py` (about 15 minutes at one request a second). Writes
research/data/kp1/mara_blocks.csv (height, timestamp, reward_sats), network_heights.csv (quarter, start_ts,
height_at_start) and coinbase_daily.csv (product, day, close, volume, usd_volume), plus a copy in
/mnt/project-files/research/kp1/ when that folder exists. Rules: research/kp1_kpi_prereg.md.
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "kp1"
SHARED = Path("/mnt/project-files/research/kp1")
MEMPOOL = "https://mempool.space/api/v1/mining"
POOL_BLOCKS = f"{MEMPOOL}/pool/marapool/blocks"
COINBASE = "https://api.exchange.coinbase.com/products"
FIRST_HEIGHT = 677222  # last block before 2021Q2: the 2021Q3 naive baseline needs all of 2021Q2
PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "LTC-USD", "ADA-USD", "LINK-USD",
            "AVAX-USD", "USDT-USD")
WINDOW_DAYS = 300  # Coinbase returns at most 300 candles a call


def quarter_bounds(q: str) -> tuple[int, int]:
    """UTC calendar quarter as unix seconds [start, end)."""
    year, n = int(q[:4]), int(q[5])
    start = datetime(year, 3 * n - 2, 1, tzinfo=timezone.utc)
    end = datetime(year + (n == 4), 1 if n == 4 else 3 * n + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def quarters(first: str, last: str) -> list[str]:
    out, (y, n) = [], (int(first[:4]), int(first[5]))
    while f"{y}Q{n}" <= last:
        out.append(f"{y}Q{n}")
        y, n = (y + 1, 1) if n == 4 else (y, n + 1)
    return out


def blocks_in_quarter(blocks, q: str):
    start, end = quarter_bounds(q)
    return [b for b in blocks if start <= b[1] < end]


def fetch_mara_blocks(get) -> list[tuple[int, int, int]]:
    """Every MARA Pool block from height FIRST_HEIGHT up, as (height, unix_ts, reward_sats), oldest first.

    Each page holds the blocks below the height in its URL; the next page starts at the page's lowest height.
    """
    seen: dict[int, tuple[int, int, int]] = {}
    url = POOL_BLOCKS
    last_low = float("inf")
    while True:
        page = get(url)
        if not page:
            break
        for b in page:
            if b["height"] >= FIRST_HEIGHT:
                seen[b["height"]] = (b["height"], int(b["timestamp"]), int(b["extras"]["reward"]))
        low = min(b["height"] for b in page)
        if low < FIRST_HEIGHT or low >= last_low:
            break
        last_low = low
        url = f"{POOL_BLOCKS}/{low}"
    return sorted(seen.values())


def height_at(get, ts: int) -> int:
    """Height of the last block mined at or before ts."""
    return int(get(f"{MEMPOOL}/blocks/timestamp/{ts}")["height"])


def fetch_coinbase_daily(get, product: str, start: date, end: date) -> list[tuple[date, float, float]]:
    """Daily (day, close, volume in base units) for days in [start, end), oldest first; missing days are absent."""
    out: dict[date, tuple[date, float, float]] = {}
    s = start
    while s < end:
        e = min(s + timedelta(days=WINDOW_DAYS - 1), end - timedelta(days=1))
        url = f"{COINBASE}/{product}/candles?granularity=86400&start={s.isoformat()}&end={e.isoformat()}"
        for t, _low, _high, _open, close, volume in get(url):
            d = datetime.fromtimestamp(t, tz=timezone.utc).date()
            if start <= d < end:
                out[d] = (d, float(close), float(volume))
        s = e + timedelta(days=1)
    return [out[d] for d in sorted(out)]


def usd_volume(rows) -> dict[date, float]:
    return {d: close * volume for d, close, volume in rows}


def _http_get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "agentdesk-research"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            time.sleep(1.0)
            return json.loads(body)
        except Exception as exc:  # network hiccup or rate limit: back off and retry
            if attempt == 4:
                raise
            print(f"retry {url}: {exc}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    blocks = fetch_mara_blocks(_http_get)
    with open(DATA / "mara_blocks.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["height", "timestamp", "reward_sats"])
        w.writerows(blocks)
    print(f"mara blocks: {len(blocks)}")
    with open(DATA / "network_heights.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["quarter", "start_ts", "height_at_start"])
        for q in quarters("2021Q1", "2026Q4"):
            start, _ = quarter_bounds(q)
            w.writerow([q, start, height_at(_http_get, start)])
    with open(DATA / "coinbase_daily.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product", "day", "close", "volume", "usd_volume"])
        for p in PRODUCTS:
            rows = fetch_coinbase_daily(_http_get, p, date(2021, 1, 1), date(2026, 10, 1))
            w.writerows([p, d.isoformat(), c, v, c * v] for d, c, v in rows)
            print(f"{p}: {len(rows)} days")
    if SHARED.parent.exists():
        SHARED.mkdir(parents=True, exist_ok=True)
        for name in ("mara_blocks.csv", "network_heights.csv", "coinbase_daily.csv"):
            shutil.copy(DATA / name, SHARED / name)


if __name__ == "__main__":
    main()
