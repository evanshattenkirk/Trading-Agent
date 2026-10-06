"""Download, for each trading day d, ThetaData 1-minute NBBO quotes of SPY's first listed expiry after d.

Standalone research script, like fetch_thetadata_spy0dte.py (same creds, venv and retry rules). One pull serves two
replays: book G's real-quote check (`strategies_new_quotes.py --next-quotes`) and H4, the overnight 1DTE iron fly
(`h4_overnight_fly.py`, rules in book_h_candidates_prereg.md section 4).

    .venv/bin/python research/fetch_thetadata_spy_next.py --smoke          # one recent day, printed
    .venv/bin/python research/fetch_thetadata_spy_next.py                  # full resumable pull, 2016 onward

Output: data/thetadata/spy_next/YYYY-MM-DD.parquet, named by the trade date d (not the expiry), all strikes, calls
and puts, 09:30-16:00 ET, with an `expiration` column. Manifest and failures CSVs sit next to the folder. Rerunning
skips days already on disk. Expect roughly the size of the 0DTE pull (about 1 GB).
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
OUT_DIR = ROOT / "data" / "thetadata" / "spy_next"
FAIL_CSV = ROOT / "data" / "thetadata" / "spy_next_failures.csv"
MANIFEST_CSV = ROOT / "data" / "thetadata" / "spy_next_manifest.csv"
log = logging.getLogger("fetch_spy_next")


def next_expiry_after(day: dt.date, expirations) -> dt.date | None:
    later = [e for e in expirations if e > day]
    return min(later) if later else None


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
                strike="*", right="both", start_time="09:30:00", end_time="16:00:00",
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
    ap.add_argument("--smoke", action="store_true", help="fetch one day and print it; write nothing")
    ap.add_argument("--day", type=dt.date.fromisoformat, help="day for --smoke (default: the last weekday)")
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
    if a.smoke:
        day = a.day or (dt.date.today() - dt.timedelta(days=1))
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
        e = next_expiry_after(day, exps)
        print("options subscription:", client.options_subscription, "| day", day, "-> next expiry", e)
        df = fetch(client, day, e) if e else None
        print("no data" if df is None else df.head(10).to_string())
        print("rows:", 0 if df is None else len(df))
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    end = a.end or (dt.date.today() - dt.timedelta(days=1))
    todo = []
    for d in base.trading_days(a.start, end):
        e = next_expiry_after(d, exps)
        if e is not None and not (OUT_DIR / f"{d.isoformat()}.parquet").exists():
            todo.append((d, e))
    log.info("%d days to fetch", len(todo))
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
