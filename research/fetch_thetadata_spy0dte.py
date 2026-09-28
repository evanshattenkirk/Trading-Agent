"""Download 1-minute NBBO quotes for SPY same-day-expiry (0DTE) options from ThetaData.

Standalone research script; not part of the engine. Needs THETADATA_API_KEY in the repo .env
and a Python 3.12+ venv with `thetadata pandas pyarrow`.

    .venv/bin/python research/fetch_thetadata_spy0dte.py --smoke            # one recent Friday
    .venv/bin/python research/fetch_thetadata_spy0dte.py                    # full resumable pull

Output: data/thetadata/spy_0dte/YYYY-MM-DD.parquet (one file per 0DTE session, all strikes, calls+puts).
Failures go to data/thetadata/spy_0dte_failures.csv; the day list and per-day stats to
data/thetadata/spy_0dte_manifest.csv. Rerunning skips days already on disk.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import grpc
import pandas as pd
from thetadata import ThetaClient
from thetadata.errors import NoDataFoundError

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "thetadata" / "spy_0dte"
FAIL_CSV = ROOT / "data" / "thetadata" / "spy_0dte_failures.csv"
MANIFEST_CSV = ROOT / "data" / "thetadata" / "spy_0dte_manifest.csv"
SYMBOL = "SPY"

# gRPC codes worth retrying; anything else (auth, bad request) fails the day immediately.
TRANSIENT = {
    grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.RESOURCE_EXHAUSTED,
    grpc.StatusCode.INTERNAL, grpc.StatusCode.UNKNOWN, grpc.StatusCode.ABORTED,
}

log = logging.getLogger("fetch_spy0dte")
_csv_lock = threading.Lock()


def nyse_holidays(year: int) -> set[dt.date]:
    """Full-day NYSE closures (rules in force 2018+), plus one-off closures."""
    def nth_weekday(month, weekday, n):
        d = dt.date(year, month, 1)
        d += dt.timedelta(days=(weekday - d.weekday()) % 7)
        return d + dt.timedelta(weeks=n - 1)

    def last_weekday(month, weekday):
        d = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
        return d - dt.timedelta(days=(d.weekday() - weekday) % 7)

    def observed(d):
        # Saturday -> Friday, Sunday -> Monday; NYSE skips the Friday for New Year's Day.
        if d.weekday() == 5:
            return None if (d.month, d.day) == (1, 1) else d - dt.timedelta(days=1)
        if d.weekday() == 6:
            return d + dt.timedelta(days=1)
        return d

    def easter(y):
        a, b, c = y % 19, y // 100, y % 100
        d, e = b // 4, b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = c // 4, c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month = (h + l - 7 * m + 114) // 31
        day = (h + l - 7 * m + 114) % 31 + 1
        return dt.date(y, month, day)

    days = {
        observed(dt.date(year, 1, 1)),
        nth_weekday(1, 0, 3),                      # MLK
        nth_weekday(2, 0, 3),                      # Presidents
        easter(year) - dt.timedelta(days=2),       # Good Friday
        last_weekday(5, 0),                        # Memorial
        observed(dt.date(year, 7, 4)),
        nth_weekday(9, 0, 1),                      # Labor
        nth_weekday(11, 3, 4),                     # Thanksgiving
        observed(dt.date(year, 12, 25)),
    }
    if year >= 2022:
        days.add(observed(dt.date(year, 6, 19)))  # Juneteenth
    days |= {d for d in (dt.date(2018, 12, 5), dt.date(2025, 1, 9)) if d.year == year}
    # New Year's Day falling on a Saturday is not observed on the prior Friday.
    return {d for d in days if d is not None}


def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    hol = set().union(*(nyse_holidays(y) for y in range(start.year, end.year + 1)))
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in hol:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def make_client() -> ThetaClient:
    return ThetaClient(dataframe_type="pandas", dotenv_path=ROOT / ".env")


def list_expirations(client: ThetaClient) -> set[dt.date]:
    df = client.option_list_expirations(SYMBOL)
    col = "expiration" if "expiration" in df.columns else df.columns[-1]
    return {pd.Timestamp(x).date() for x in df[col]}


def fetch_day(client: ThetaClient, day: dt.date, retries: int = 6) -> pd.DataFrame | None:
    """1-minute quotes 09:30-16:00 ET, all strikes, both rights, for the expiry == day. None if no data."""
    delay = 5.0
    for attempt in range(1, retries + 1):
        try:
            return client.option_history_quote(
                symbol=SYMBOL, expiration=day, date=day, interval="1m",
                strike="*", right="both", start_time="09:30:00", end_time="16:00:00",
            )
        except NoDataFoundError:
            return None
        except grpc.RpcError as e:
            if e.code() not in TRANSIENT or attempt == retries:
                raise
            log.warning("%s attempt %d: %s %s; retrying in %.0fs", day, attempt, e.code().name,
                        (e.details() or "")[:200], delay)
        except (ConnectionError, TimeoutError) as e:
            if attempt == retries:
                raise
            log.warning("%s attempt %d: %r; retrying in %.0fs", day, attempt, e, delay)
        time.sleep(delay)
        delay = min(delay * 2, 300)
    return None


def append_csv(path: Path, header: list[str], row: list) -> None:
    with _csv_lock:
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(header)
            w.writerow(row)


def save_day(client: ThetaClient, day: dt.date, pause: float) -> tuple[str, int, int]:
    """Returns (status, rows, bytes). Writes atomically so a killed run never leaves a partial file."""
    t0 = time.time()
    df = fetch_day(client, day)
    time.sleep(pause)
    if df is None or len(df) == 0:
        return "no_data", 0, 0
    path = OUT_DIR / f"{day.isoformat()}.parquet"
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)
    size = path.stat().st_size
    log.info("%s rows=%d strikes=%d size=%.1fMB %.1fs", day, len(df),
             df["strike"].nunique() if "strike" in df.columns else -1, size / 1e6, time.time() - t0)
    return "ok", len(df), size


def smoke(client: ThetaClient, day: dt.date | None) -> None:
    print("options subscription:", client.options_subscription)
    print("stock subscription:  ", client.stock_subscription)
    print("index subscription:  ", client.index_subscription)
    exps = sorted(list_expirations(client))
    print(f"SPY expirations: {len(exps)} from {exps[0]} to {exps[-1]}")
    if day is None:
        today = dt.date.today()
        day = today - dt.timedelta(days=(today.weekday() - 4) % 7 or 7)  # most recent past Friday
    t0 = time.time()
    df = fetch_day(client, day)
    print(f"{day}: fetched in {time.time() - t0:.1f}s")
    if df is None:
        print("no data")
        return
    pd.set_option("display.width", 200, "display.max_columns", 30)
    print("rows:", len(df))
    print("dtypes:\n", df.dtypes)
    print(df.head(8))
    if "strike" in df.columns:
        s = df["strike"]
        print(f"strikes: {s.nunique()} unique, min {s.min()}, max {s.max()}")
    if "timestamp" in df.columns:
        ts = df["timestamp"]
        print("timestamp first/last:", ts.min(), ts.max(), "tz:", getattr(ts.dt, "tz", None))
        print("minutes per contract:", df.groupby(["strike", "right"]).size().describe().to_dict())
    tmp = OUT_DIR.parent / "_smoke.parquet"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(tmp, index=False, compression="zstd")
    print(f"parquet size for one day: {tmp.stat().st_size / 1e6:.2f} MB")
    tmp.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smoke", action="store_true", help="print subscription + one day, write nothing")
    ap.add_argument("--day", type=dt.date.fromisoformat, help="day for --smoke (default: last Friday)")
    ap.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2016, 1, 1))
    ap.add_argument("--end", type=dt.date.fromisoformat, default=None, help="default: yesterday")
    ap.add_argument("--workers", type=int, default=2, help="concurrent requests")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds to sleep after each request")
    ap.add_argument("--retry-failed", action="store_true", help="also retry days listed in the failures CSV")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("thetadata").setLevel(logging.WARNING)  # its INFO log dumps the auth response
    logging.getLogger("httpx").setLevel(logging.WARNING)

    client = make_client()
    if args.smoke:
        smoke(client, args.day)
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    end = args.end or (dt.date.today() - dt.timedelta(days=1))
    exps = list_expirations(client)
    sessions = trading_days(args.start, end)
    zero_dte = [d for d in sessions if d in exps]
    no_0dte = [d for d in sessions if d not in exps]
    first = min(zero_dte) if zero_dte else None
    log.info("options sub=%s; %d sessions %s..%s; %d with a same-day expiry (first %s), %d without",
             client.options_subscription, len(sessions), args.start, end, len(zero_dte), first, len(no_0dte))

    failed_before = set()
    if FAIL_CSV.exists() and not args.retry_failed:
        failed_before = {r["date"] for r in csv.DictReader(FAIL_CSV.open())}
    done = {p.stem for p in OUT_DIR.glob("*.parquet")}
    todo = [d for d in zero_dte if d.isoformat() not in done and d.isoformat() not in failed_before]
    log.info("%d already on disk, %d previously failed (skipped), %d to fetch",
             len(done), len(failed_before), len(todo))

    t0, n_ok, n_bytes = time.time(), 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(save_day, client, d, args.pause): d for d in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            d = futs[fut]
            try:
                status, rows, size = fut.result()
                n_ok += status == "ok"
                n_bytes += size
                append_csv(MANIFEST_CSV, ["date", "status", "rows", "bytes", "fetched_at"],
                           [d.isoformat(), status, rows, size, dt.datetime.now().isoformat(timespec="seconds")])
            except Exception as e:  # noqa: BLE001 - log and keep going; rerun picks it up
                log.error("%s FAILED: %r", d, e)
                append_csv(FAIL_CSV, ["date", "error", "at"],
                           [d.isoformat(), repr(e)[:500], dt.datetime.now().isoformat(timespec="seconds")])
            if i % 25 == 0 or i == len(todo):
                el = time.time() - t0
                log.info("progress %d/%d ok=%d %.0fMB elapsed %.0fmin eta %.0fmin", i, len(todo), n_ok,
                         n_bytes / 1e6, el / 60, el / i * (len(todo) - i) / 60)
    log.info("DONE in %.1f min: %d days saved this run, %.0f MB", (time.time() - t0) / 60, n_ok, n_bytes / 1e6)
    return 0


if __name__ == "__main__":
    sys.exit(main())
