"""Moves old rows of the journal's two big tables into yearly archive databases (tools/prune.sh runs it monthly).

journal.option_quotes (the recorder's 0DTE quotes, about 94k rows a day) and journal.rh_calls (one row per metered
Robinhood call) grow without bound. Rows older than --days (90, cut at midnight Central) move to
<archive-dir>/quotes-YYYY.db, by the row's year in Central time, into tables of the same name and columns. Per
table and year the rows are first copied in one transaction on the archive (after clearing that time range there,
so a re-run never doubles rows), then deleted in one transaction on the journal once both sides hold the same
count. A run cut short in between leaves the rows in both places and the next run repeats that range, so nothing
is lost. VACUUM runs only when the deletes freed more than 1 GB. Other journal tables are left alone.
reporting/weekly_quant.py reads the archives next to the journal, so the weekly report keeps every quote.
Standard library only.

    python tools/prune_journal.py --journal ~/.agentdesk/journal.db --archive-dir ~/.agentdesk/archive [--dry-run]
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
TABLES = ("option_quotes", "rh_calls")
VACUUM_BYTES = 1 << 30


def cutoff(now: float, days: int) -> float:
    d = datetime.fromtimestamp(now, CT).date() - timedelta(days=days)
    return datetime(d.year, d.month, d.day, tzinfo=CT).timestamp()


def _year_start(year: int) -> float:
    return datetime(year, 1, 1, tzinfo=CT).timestamp()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, CT).date().isoformat()


def _count(db, schema: str, table: str, lo: float, hi: float) -> int:
    return db.execute(f"SELECT count(*) FROM {schema}.{table} WHERE ts >= ? AND ts < ?", (lo, hi)).fetchone()[0]


def _copy_range(db, table: str, cols: list, lo: float, hi: float) -> int:
    """Copies main.table rows in [lo, hi) to arch.table in one transaction; returns how many."""
    names = ", ".join(cols)
    db.execute(f"CREATE TABLE IF NOT EXISTS arch.{table} AS SELECT {names} FROM main.{table} WHERE 0")
    have = {r[1] for r in db.execute(f"PRAGMA arch.table_info({table})")}
    for c in cols:
        if c not in have:
            db.execute(f"ALTER TABLE arch.{table} ADD COLUMN {c}")
    db.execute(f"CREATE INDEX IF NOT EXISTS arch.{table}_ts ON {table}(ts)")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(f"DELETE FROM arch.{table} WHERE ts >= ? AND ts < ?", (lo, hi))
        db.execute(f"INSERT INTO arch.{table} ({names}) SELECT {names} FROM main.{table} WHERE ts >= ? AND ts < ?",
                   (lo, hi))
        n, got = _count(db, "main", table, lo, hi), _count(db, "arch", table, lo, hi)
        if n != got:
            raise RuntimeError(f"{table}: copied {got} rows of {n}")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return n


def _delete_range(db, table: str, lo: float, hi: float) -> int:
    """Deletes main.table rows in [lo, hi), only while the archive holds as many rows for that range."""
    db.execute("BEGIN IMMEDIATE")
    try:
        n, got = _count(db, "main", table, lo, hi), _count(db, "arch", table, lo, hi)
        if n != got:
            raise RuntimeError(f"{table}: the archive holds {got} rows of {n}; nothing deleted")
        db.execute(f"DELETE FROM main.{table} WHERE ts >= ? AND ts < ?", (lo, hi))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return n


def prune(journal, archive_dir, days: int = 90, now: float | None = None, dry_run: bool = False, out=print) -> dict:
    journal, archive_dir = Path(journal).expanduser(), Path(archive_dir).expanduser()
    tag = " (dry run)" if dry_run else ""
    if not journal.exists():
        out(f"prune: no journal at {journal}")
        return {"moved": {}, "freed": 0, "vacuum": False}
    cut = cutoff(time.time() if now is None else now, days)
    db = sqlite3.connect(str(journal), timeout=60, isolation_level=None)     # explicit transactions only
    moved: dict = {}
    try:
        for table in TABLES:
            cols = [r[1] for r in db.execute(f"PRAGMA main.table_info({table})")]
            if "ts" not in cols:
                continue
            first, n = db.execute(f"SELECT min(ts), count(*) FROM main.{table} WHERE ts < ?", (cut,)).fetchone()
            if not n:
                out(f"prune: {table}: nothing before {_day(cut)}")
                continue
            for year in range(datetime.fromtimestamp(first, CT).year, datetime.fromtimestamp(cut - 1, CT).year + 1):
                lo, hi = _year_start(year), min(_year_start(year + 1), cut)
                k = _count(db, "main", table, lo, hi)
                if not k:
                    continue
                dest = archive_dir / f"quotes-{year}.db"
                out(f"prune: {table}: move {k:,} rows from {_day(max(lo, first))} to {_day(hi - 1)} into {dest}{tag}")
                if dry_run:
                    moved[f"{table} {year}"] = k
                    continue
                archive_dir.mkdir(parents=True, exist_ok=True)
                db.execute("ATTACH DATABASE ? AS arch", (str(dest),))
                try:
                    _copy_range(db, table, cols, lo, hi)
                    moved[f"{table} {year}"] = _delete_range(db, table, lo, hi)
                finally:
                    db.execute("DETACH DATABASE arch")
        freed = db.execute("PRAGMA freelist_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
        vacuum = not dry_run and bool(moved) and freed > VACUUM_BYTES
        if vacuum:
            out(f"prune: VACUUM the journal ({freed / 2**30:.1f} GB free)")
            try:
                db.execute("VACUUM")
            except sqlite3.OperationalError as ex:
                out(f"prune: VACUUM skipped ({ex}); the next run tries again")
                vacuum = False
        elif moved and not dry_run:
            out(f"prune: {freed / 2**20:,.0f} MB free in the journal; VACUUM waits for more than 1 GB")
    finally:
        db.close()
    return {"moved": moved, "freed": freed, "vacuum": vacuum}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--journal", default="~/.agentdesk/journal.db")
    ap.add_argument("--archive-dir", default="~/.agentdesk/archive")
    ap.add_argument("--days", type=int, default=90, help="keep this many days of rows in the journal")
    ap.add_argument("--dry-run", action="store_true", help="print what would move, change nothing")
    a = ap.parse_args(argv)
    prune(a.journal, a.archive_dir, a.days, dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
