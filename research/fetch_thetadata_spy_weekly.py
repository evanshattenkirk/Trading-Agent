"""Download, for each week's last session d, ThetaData 1-minute NBBO quotes (15:30-16:00 ET) of the SPY expiry on the
last session of the following week. Entry quotes for N3 (rules in research/n3_weekly_put_spread_prereg.md).

Standalone research script, like fetch_thetadata_spy_next.py (same creds, venv and retry rules; Mac only).

    .venv/bin/python research/fetch_thetadata_spy_weekly.py --smoke          # the latest week, printed
    .venv/bin/python research/fetch_thetadata_spy_weekly.py                  # full resumable pull, 2016 onward

Output: data/thetadata/spy_weekly/YYYY-MM-DD.parquet, named by entry day d, all strikes, calls and puts, with an
`expiration` column. About 560 small files. Manifest and failures CSVs sit next to the folder; reruns skip days on disk.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "thetadata" / "spy_weekly"
FAIL_CSV = ROOT / "data" / "thetadata" / "spy_weekly_failures.csv"
MANIFEST_CSV = ROOT / "data" / "thetadata" / "spy_weekly_manifest.csv"
log = logging.getLogger("fetch_spy_weekly")


def week_pairs(days, expirations) -> list[tuple[dt.date, dt.date]]:
    """(d, e): d the last trading day of a calendar week, e the last trading day of the next week when it is a
    listed expiry. Weeks whose e is missing are left out."""
    last: dict[tuple[int, int], dt.date] = {}
    for d in sorted(days):
        last[tuple(d.isocalendar()[:2])] = d
    exps = set(expirations)
    out = []
    for d in sorted(last.values()):
        nxt = d + dt.timedelta(days=7)
        e = last.get(tuple(nxt.isocalendar()[:2]))
        if e is not None and e in exps:
            out.append((d, e))
    return out


def _base():
    """The 0DTE fetcher's client, calendar and retry codes (imports thetadata + grpc, so only on the Mac)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fetch_thetadata_spy0dte as base
    return base


def fetch(client, day: dt.date, expiry: dt.date, retries: int = 6):
    import grpc
    from thetadata.errors import NoDataFoundError
    base = _base()
    delay = 5.0
    for attempt in range(1, retries + 1):
        try:
            df = client.option_history_quote(
                symbol=base.SYMBOL, expiration=expiry, date=day, interval="1m",
                strike="*", right="both", start_time="15:30:00", end_time="16:00:00",
            )
            if df is not None and len(df) and "expiration" not in {c.lower() for c in df.columns}:
                df["expiration"] = expiry.isoformat()
            return df
        except NoDataFoundError:
            return None
        except grpc.RpcError as e:
            if e.code() not in base.TRANSIENT or attempt == retries:
                raise
            log.warning("%s attempt %d: %s; retrying in %.0fs", day, attempt, e.code().name, delay)
        except (ConnectionError, TimeoutError) as e:
            if attempt == retries:
                raise
            log.warning("%s attempt %d: %r; retrying in %.0fs", day, attempt, e, delay)
        time.sleep(delay)
        delay = min(delay * 2, 300)
    return None


def save(client, day: dt.date, expiry: dt.date, pause: float) -> tuple[str, int, int]:
    t0 = time.time()
    df = fetch(client, day, expiry)
    time.sleep(pause)
    if df is None or len(df) == 0:
        return "no_data", 0, 0
    path = OUT_DIR / f"{day.isoformat()}.parquet"
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)
    log.info("%s expiry %s rows=%d %.1fMB %.1fs", day, expiry, len(df), path.stat().st_size / 1e6, time.time() - t0)
    return "ok", len(df), path.stat().st_size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smoke", action="store_true", help="fetch the latest week and print it; write nothing")
    ap.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2016, 1, 1))
    ap.add_argument("--end", type=dt.date.fromisoformat, default=None, help="default: yesterday")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--pause", type=float, default=0.5)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("thetadata").setLevel(logging.WARNING)  # its INFO log dumps the auth response
    logging.getLogger("httpx").setLevel(logging.WARNING)
    base = _base()
    client = base.make_client()
    exps = base.list_expirations(client)
    end = a.end or (dt.date.today() - dt.timedelta(days=1))
    pairs = week_pairs(base.trading_days(a.start, end), exps)
    if a.smoke:
        d, e = pairs[-1]
        print("options subscription:", client.options_subscription, "| entry", d, "-> expiry", e)
        df = fetch(client, d, e)
        print("no data" if df is None else df.head(10).to_string())
        print("rows:", 0 if df is None else len(df))
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    todo = [(d, e) for d, e in pairs if not (OUT_DIR / f"{d.isoformat()}.parquet").exists()]
    log.info("%d weeks to fetch", len(todo))
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(save, client, d, e, a.pause): (d, e) for d, e in todo}
        for f in as_completed(futs):
            d, e = futs[f]
            try:
                status, rows, size = f.result()
                base.append_csv(MANIFEST_CSV, ["date", "expiration", "status", "rows", "bytes"],
                                [d.isoformat(), e.isoformat(), status, rows, size])
            except Exception as exc:  # noqa: BLE001
                log.error("%s failed: %r", d, exc)
                base.append_csv(FAIL_CSV, ["date", "expiration", "error"], [d.isoformat(), e.isoformat(), repr(exc)[:300]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
