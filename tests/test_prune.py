"""tools/prune_journal.py (old option_quotes / rh_calls rows -> yearly archive DBs) and tools/prune.sh (old logs and
sessions gzipped, big launchd logs rotated), on temporary directories."""
import gzip
import importlib.util
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reporting import weekly_quant as wq

ROOT = Path(__file__).resolve().parent.parent
CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 7, 16, 0, tzinfo=CT).timestamp()       # cutoff for 90 days: 2026-07-09 00:00 CT

spec = importlib.util.spec_from_file_location("prune_journal", ROOT / "tools" / "prune_journal.py")
pj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pj)


def ts(day: str, hm: str = "09:00") -> float:
    return datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=CT).timestamp()


DAYS = ["2025-12-30", "2026-01-02", "2026-03-10", "2026-07-08", "2026-07-09", "2026-10-06"]


@pytest.fixture
def journal(tmp_path):
    p = tmp_path / "journal.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE option_quotes (ts REAL, expiry TEXT, strike REAL, right TEXT, bid REAL, ask REAL, spot REAL)")
    db.execute("CREATE TABLE rh_calls (ts REAL, tool TEXT, ms REAL, ok INTEGER, kind TEXT, err TEXT, tag TEXT)")
    db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, session TEXT, closed_ts REAL)")
    for d in DAYS:
        for hm in ("08:45", "14:30"):
            for k, right, bid in ((765.0, "call", 1.9), (765.0, "put", 1.9)):
                db.execute("INSERT INTO option_quotes VALUES (?,?,?,?,?,?,?)",
                           (ts(d, hm), d, k, right, bid if hm == "08:45" else 0.1, bid + 0.2 if hm == "08:45" else 0.2,
                            765.0 if hm == "08:45" else 767.0))
        db.execute("INSERT INTO rh_calls VALUES (?,?,?,?,?,?,?)", (ts(d), "get_option_quotes", 120.0, 1, "q", None, "rec"))
        db.execute("INSERT INTO trades (session, closed_ts) VALUES (?, ?)", (d, ts(d)))
    db.commit()
    db.close()
    return p


def rows(path, table, order="ts"):
    db = sqlite3.connect(path)
    try:
        return db.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
    finally:
        db.close()


def test_old_rows_move_to_yearly_archives(journal, tmp_path):
    arch = tmp_path / "archive"
    before = {t: rows(journal, t) for t in ("option_quotes", "rh_calls")}
    out = []
    r = pj.prune(journal, arch, days=90, now=NOW, out=out.append)
    cut = ts("2026-07-09", "00:00")
    for t in ("option_quotes", "rh_calls"):
        assert rows(journal, t) == [x for x in before[t] if x[0] >= cut]            # recent rows stay
        moved = rows(arch / "quotes-2025.db", t) + rows(arch / "quotes-2026.db", t)
        assert moved == [x for x in before[t] if x[0] < cut]                        # old rows, each exactly once
        assert all(datetime.fromtimestamp(x[0], CT).year == 2025 for x in rows(arch / "quotes-2025.db", t))
    assert len(rows(journal, "trades", order="id")) == len(DAYS)                     # other tables untouched
    assert r["moved"]["option_quotes 2026"] == 12 and r["moved"]["rh_calls 2025"] == 1
    assert r["vacuum"] is False and any("quotes-2026.db" in line for line in out)


def test_a_second_run_moves_nothing_and_a_dry_run_changes_nothing(journal, tmp_path):
    arch = tmp_path / "archive"
    out = []
    pj.prune(journal, arch, now=NOW, dry_run=True, out=out.append)
    assert not arch.exists() and len(rows(journal, "option_quotes")) == 4 * len(DAYS)
    assert any("move" in line for line in out)
    pj.prune(journal, arch, now=NOW, out=lambda s: None)
    snap = (rows(arch / "quotes-2026.db", "option_quotes"), rows(journal, "option_quotes"))
    r = pj.prune(journal, arch, now=NOW, out=lambda s: None)
    assert r["moved"] == {} and snap == (rows(arch / "quotes-2026.db", "option_quotes"), rows(journal, "option_quotes"))


def test_a_run_cut_short_after_the_copy_is_repeated_without_doubles(journal, tmp_path, monkeypatch):
    arch = tmp_path / "archive"
    real = pj._delete_range

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(pj, "_delete_range", boom)
    with pytest.raises(KeyboardInterrupt):
        pj.prune(journal, arch, now=NOW, out=lambda s: None)
    assert len(rows(journal, "option_quotes")) == 4 * len(DAYS)                     # nothing deleted yet
    monkeypatch.setattr(pj, "_delete_range", real)
    pj.prune(journal, arch, now=NOW, out=lambda s: None)
    moved = rows(arch / "quotes-2025.db", "option_quotes") + rows(arch / "quotes-2026.db", "option_quotes")
    assert len(moved) == 4 * 4 and len(set(moved)) == 4 * 4                          # 4 old days, no doubles
    assert len(rows(journal, "option_quotes")) == 4 * 2


def test_vacuum_only_past_the_threshold(journal, tmp_path, monkeypatch):
    assert pj.prune(journal, tmp_path / "a1", now=NOW, out=lambda s: None)["vacuum"] is False
    monkeypatch.setattr(pj, "VACUUM_BYTES", -1)
    assert pj.prune(journal, tmp_path / "a2", now=NOW + 400 * 86400, out=lambda s: None)["vacuum"] is True


def test_missing_journal_or_tables_are_fine(tmp_path):
    assert pj.prune(tmp_path / "none.db", tmp_path / "a", now=NOW, out=lambda s: None)["moved"] == {}
    p = tmp_path / "empty.db"
    sqlite3.connect(p).close()
    assert pj.prune(p, tmp_path / "a", now=NOW, out=lambda s: None)["moved"] == {}


def test_weekly_report_still_sees_archived_quotes(journal, tmp_path):
    """The report's since-start straddle numbers and book A's quote lookups read the archives next to the journal."""
    a = tmp_path / "archive"                                       # the report looks in <journal dir>/archive
    before = wq.straddle_vs_realized(journal, "2000-01-01", "2026-10-09")
    near = wq.QuoteBook(journal).near("2026-03-10", 765.0, "call", ts("2026-03-10", "08:45"))
    assert before["days"] == len(DAYS) and near is not None
    pj.prune(journal, a, now=NOW, out=lambda s: None)
    assert len(rows(journal, "option_quotes")) < 4 * len(DAYS)
    assert wq.straddle_vs_realized(journal, "2000-01-01", "2026-10-09") == before
    assert wq.QuoteBook(journal).near("2026-03-10", 765.0, "call", ts("2026-03-10", "08:45")) == near


def test_cli(journal, tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "prune_journal.py"), "--journal", str(journal),
                        "--archive-dir", str(tmp_path / "archive"), "--days", "90", "--dry-run"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "dry run" in r.stdout and "option_quotes" in r.stdout


# ---------------------------------------------------------------- tools/prune.sh

def old(p: Path, days: int, text="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    t = time.time() - days * 86400
    os.utime(p, (t, t))


def test_prune_sh_gzips_old_logs_and_sessions_and_rotates_big_launchd_logs(tmp_path):
    ad = tmp_path / ".agentdesk"
    old(ad / "paper" / "2026-08-01.log", 40)
    old(ad / "paper" / "2026-10-06.log", 1)
    old(ad / "sessions" / "2026-08-01.snapshot.json", 40, "{}")
    old(ad / "sessions" / "2026-08-01.events.jsonl", 40)
    old(ad / "sessions" / "2026-10-06.snapshot.json", 1, "{}")
    old(ad / "cache-iex-vs-sip" / "SPY_trades_v2_sip_2026-08-01.npz", 40)
    big = ad / "review" / "launchd.err.log"
    big.parent.mkdir(parents=True)
    big.write_bytes(b"e" * (10 * 1024 * 1024 + 1))
    small = ad / "recorder" / "launchd.out.log"
    old(small, 1)
    env = {**os.environ, "AGENTDESK_HOME": str(ad), "PRUNE_PY": sys.executable}
    r = subprocess.run(["bash", str(ROOT / "tools" / "prune.sh"), "--dry-run"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "gzip" in r.stdout and (ad / "paper" / "2026-08-01.log").exists()        # dry run: printed, not done
    r = subprocess.run(["bash", str(ROOT / "tools" / "prune.sh")], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (ad / "paper" / "2026-08-01.log.gz").exists() and not (ad / "paper" / "2026-08-01.log").exists()
    assert (ad / "paper" / "2026-10-06.log").exists()
    assert (ad / "sessions" / "2026-08-01.snapshot.json.gz").exists()
    assert (ad / "sessions" / "2026-08-01.events.jsonl.gz").exists()
    assert (ad / "sessions" / "2026-10-06.snapshot.json").exists()
    assert not (ad / "cache-iex-vs-sip" / "SPY_trades_v2_sip_2026-08-01.npz").exists()
    assert big.stat().st_size == 0 and len(gzip.decompress((big.parent / "launchd.err.log.1.gz").read_bytes())) > 10 * 1024 * 1024
    assert small.read_text() == "x"
    assert "no journal" in r.stdout                                                   # the DB step ran too
