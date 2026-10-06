"""Fetch the public CBOE data for V1 (research/vix_carry_prereg.md) into data/cboe. Standalone research script; run
on the Mac (cdn.cboe.com isn't reachable from the cloud sessions). Resumable: contracts already saved are skipped.

    .venv/bin/python research/fetch_cboe_vx.py --smoke     # one recent contract + VIX3M, printed; writes nothing
    .venv/bin/python research/fetch_cboe_vx.py             # VX monthly futures 2007-11..2027-02, VIX, VIX3M
    .venv/bin/python research/fetch_cboe_vx.py --svxy      # also SVXY daily closes from Alpaca (keys from .env)

Output: data/cboe/vx/VX_YYYY-MM.csv (one per monthly contract, named by contract month), data/cboe/vx_manifest.csv
(the URL used for each), data/cboe/VIX_History.csv, data/cboe/VIX3M_History.csv, data/cboe/svxy_daily.csv.
Per-expiry files come from CBOE's current historical-data CDN; older contracts from its CFE archive. If CBOE has
moved a file, fix the URL here (a data-source fix, not a rule change) and rerun.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vix_carry import nyse_session, vx_settlement  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "cboe"
URL_NEW = "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_{d}.csv"
URL_OLD = "https://cdn.cboe.com/resources/futures/archive/volume-and-price/CFE_{code}{yy}_VX.csv"
URL_IDX = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
MONTH_CODES = "FGHJKMNQUVXZ"
FIRST, LAST = date(2007, 11, 1), date(2027, 2, 28)
UA = {"User-Agent": "Mozilla/5.0 (agentdesk research script)"}


def months(lo: date, hi: date) -> list[tuple[int, int]]:
    out, y, m = [], lo.year, lo.month
    while (y, m) <= (hi.year, hi.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def archive_url(y: int, m: int) -> str:
    return URL_OLD.format(code=MONTH_CODES[m - 1], yy=f"{y % 100:02d}")


def get(url: str, tries: int = 3) -> str | None:
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2 ** i)
    return None


def looks_like_vx(text: str | None) -> bool:
    if not text or "trade date" not in text.lower():
        return False
    return sum(1 for line in text.splitlines() if line[:1].isdigit()) >= 5


def candidates(y: int, m: int) -> list[str]:
    s = vx_settlement(y, m, nyse_session)
    days = [s] + [s + timedelta(days=k) for k in (-1, 1, -2, 2) if (s + timedelta(days=k)).weekday() < 5]
    return [URL_NEW.format(d=d.isoformat()) for d in days] + [archive_url(y, m)]


def fetch_contract(y: int, m: int) -> tuple[str, str] | None:
    for url in candidates(y, m):
        text = get(url)
        if looks_like_vx(text):
            return url, text
        time.sleep(0.2)
    return None


def fetch_indexes(out: Path) -> None:
    for name in ("VIX", "VIX3M"):
        text = get(URL_IDX.format(name=name))
        if not text or "date" not in text.lower():
            raise SystemExit(f"{name} history not found at {URL_IDX.format(name=name)}")
        (out / f"{name}_History.csv").write_text(text)
        print(f"{name}: {len(text.splitlines()) - 1} rows")


def fetch_svxy(out: Path) -> None:
    sys.path.insert(0, str(ROOT))
    from agentdesk.config import _load_dotenv
    from agentdesk.feeds.f_data import alpaca_bars
    _load_dotenv(ROOT / ".env")
    end = (date.today() - timedelta(days=1)).isoformat()
    bars = asyncio.run(alpaca_bars(["SVXY"], "1Day", "2018-01-01", end, feed="sip"))["SVXY"]
    with open(out / "svxy_daily.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "close"])
        for b in bars:
            w.writerow([b["t"][:10], b["c"]])
    print(f"SVXY: {len(bars)} daily bars")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--svxy", action="store_true", help="also SVXY daily closes from Alpaca (validation only)")
    a = ap.parse_args()
    if a.smoke:
        today = date.today()
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        got = fetch_contract(y, m)
        print("contract", y, m, "->", got[0] if got else "NOT FOUND")
        if got:
            print("\n".join(got[1].splitlines()[:5]))
        old = fetch_contract(2008, 3)
        print("archive 2008-03 ->", old[0] if old else "NOT FOUND")
        text = get(URL_IDX.format(name="VIX3M"))
        print("VIX3M ->", "\n".join(text.splitlines()[:3]) if text else "NOT FOUND")
        return 0
    vx = OUT / "vx"
    vx.mkdir(parents=True, exist_ok=True)
    manifest = OUT / "vx_manifest.csv"
    rows = list(csv.DictReader(open(manifest))) if manifest.exists() else []
    done = {r["month"] for r in rows}
    missing = []
    for y, m in months(FIRST, LAST):
        key = f"{y}-{m:02d}"
        if key in done and (vx / f"VX_{key}.csv").exists():
            continue
        got = fetch_contract(y, m)
        if not got:
            missing.append(key)
            print(f"{key}: not found")
            continue
        url, text = got
        (vx / f"VX_{key}.csv").write_text(text)
        n = sum(1 for line in text.splitlines() if line[:1].isdigit())
        rows.append({"month": key, "rule_settle": vx_settlement(y, m, nyse_session).isoformat(), "url": url,
                     "rows": n})
        print(f"{key}: {n} rows from {url}")
        time.sleep(0.3)
    with open(manifest, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["month", "rule_settle", "url", "rows"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["month"]))
    fetch_indexes(OUT)
    if a.svxy:
        fetch_svxy(OUT)
    future = [k for k in missing if k > date.today().strftime("%Y-%m")]
    past = [k for k in missing if k not in future]
    print(f"done; missing past contracts: {past or 'none'}; not yet listed: {len(future)}")
    return 1 if past else 0


if __name__ == "__main__":
    raise SystemExit(main())
