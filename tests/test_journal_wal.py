"""The journal is shared by two writers (engine, standalone recorder) and read by the weekly report: WAL plus a 30 s
busy timeout on every connection, indexes on the big tables, and a failed trade insert that never leaves a
half-written transaction behind (review plan 2026-10-06, M12, M15, I14)."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import recorder
from agentdesk.exits import Contract, Position
from agentdesk.journal import Journal, use_wal
from reporting import weekly_quant as wq


def closed_pos(entry=1.00, exit_px=1.20):
    p = Position(Contract("SPY", "2026-09-28", 660.0, "call"), "SWING", 2, entry, 1_000.0)
    p.realized, p.fees, p.closed_ts, p.status, p.exit_reason = (exit_px - entry) * 200, 0.16, 1_060.0, "closed", "tp"
    return p


def test_journal_opens_in_wal_with_a_30s_busy_timeout(tmp_path):
    j = Journal(tmp_path / "j.db")
    assert j.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert j.db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    assert Journal(None).db.execute("PRAGMA journal_mode").fetchone()[0] == "memory"     # sim / tests: no file


def test_recorder_connections_use_wal_too(tmp_path):
    m = recorder.CallMeter(tmp_path / "j.db")          # the probe and iv-snapshot open the meter before any Journal
    assert m.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert m.db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_weekly_report_reads_a_wal_journal_while_the_engine_holds_it_and_after(tmp_path):
    path = tmp_path / "journal.db"
    j = Journal(path)
    j.record_trade("2026-09-28", "paper", closed_pos())
    assert (tmp_path / "journal.db-wal").exists()            # the row may live only in the WAL file so far
    rows = wq.load_trades(path)                              # read-only URI connection, as the Friday report opens it
    assert len(rows) == 1 and rows[0]["pnl"] == pytest.approx(40.0 - 0.16)
    j.db.close()                                             # last writer gone: SQLite checkpoints and drops -wal/-shm
    rows = wq.load_trades(path)
    assert len(rows) == 1


def test_wal_switch_that_cannot_get_the_lock_is_not_fatal(tmp_path, caplog):
    path = tmp_path / "j.db"
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("CREATE TABLE x (a)")
    other.execute("BEGIN")
    other.execute("SELECT * FROM x").fetchall()              # a read transaction blocks the switch to WAL
    db = sqlite3.connect(path, timeout=0.1)
    use_wal(db)                                              # logs and carries on in the old mode
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert "WAL" in caplog.text
    other.execute("COMMIT")
    db.close()
    j = Journal(path)                                        # the next open switches
    assert j.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


class CommitFailsOnce:
    """A connection whose first commit raises, as a `database is locked` at COMMIT would."""

    def __init__(self, db):
        self.db, self.failed = db, False

    def execute(self, *a):
        return self.db.execute(*a)

    def rollback(self):
        self.db.rollback()

    def commit(self):
        if not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("database is locked")
        self.db.commit()


def test_a_failed_trade_insert_is_rolled_back_so_a_retry_writes_one_row(tmp_path):
    j = Journal(tmp_path / "j.db")
    real = j.db
    j.db = CommitFailsOnce(real)
    with pytest.raises(sqlite3.OperationalError):
        j.record_trade("2026-09-28", "paper", closed_pos())
    j.record_trade("2026-09-28", "paper", closed_pos())        # the engine's retry
    assert real.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_a_locked_journal_fails_the_insert_then_the_retry_lands(tmp_path):
    path = tmp_path / "j.db"
    j = Journal(path, timeout=0.1)
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")                           # the recorder mid-write
    with pytest.raises(sqlite3.OperationalError):
        j.record_trade("2026-09-28", "paper", closed_pos())
    other.execute("COMMIT")
    j.record_trade("2026-09-28", "paper", closed_pos())
    assert len(j.trades()) == 1
