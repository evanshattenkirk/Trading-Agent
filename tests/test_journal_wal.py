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


OLD_SCHEMA = """
CREATE TABLE option_quotes (ts REAL, expiry TEXT, strike REAL, right TEXT, bid REAL, ask REAL, spot REAL);
CREATE TABLE rh_calls (ts REAL, tool TEXT, ms REAL, ok INTEGER, kind TEXT, err TEXT, tag TEXT);
INSERT INTO option_quotes VALUES (1.0, '2026-09-28', 660, 'call', 1.0, 1.1, 660.2);
INSERT INTO rh_calls VALUES (1.0, 'get_option_quotes', 120.0, 1, 'ok', NULL, 'recorder');
"""


def _indexes(db) -> dict:
    out = {}
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"):
        out[name] = [r[2] for r in db.execute(f"PRAGMA index_info({name})")]
    return out


def test_indexes_are_added_to_an_existing_journal_and_reopening_is_idempotent(tmp_path):
    path = tmp_path / "j.db"
    c = sqlite3.connect(path)
    c.executescript(OLD_SCHEMA)
    c.close()
    Journal(path).db.close()
    j = Journal(path)                                        # second open: CREATE INDEX IF NOT EXISTS, no error
    cols = sorted(_indexes(j.db).values())
    assert ["expiry", "ts"] in cols and ["ts"] in cols and ["tag", "ts"] in cols
    assert j.db.execute("SELECT COUNT(*) FROM option_quotes").fetchone()[0] == 1


def test_journal_creates_rh_calls_exactly_as_the_recorder_does(tmp_path):
    a, b = sqlite3.connect(":memory:"), Journal(None).db
    a.execute(recorder.RH_CALLS)
    info = lambda db: [r[1:3] for r in db.execute("PRAGMA table_info(rh_calls)")]
    assert info(a) == info(b)


def _plan(db, sql, args=()):
    return " ".join(r[-1] for r in db.execute("EXPLAIN QUERY PLAN " + sql, args))


def test_hot_queries_use_the_indexes():
    db = Journal(None).db
    assert "USING" in _plan(db, "SELECT ts,strike,right,bid,ask FROM option_quotes WHERE expiry=? AND ts>=? "
                                "ORDER BY ts DESC LIMIT 400", ("2026-09-28", 0))          # Vol desk, latest_straddle
    assert "USING" in _plan(db, "SELECT COUNT(*), COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM option_quotes "
                                "WHERE ts>=? AND ts<?", (0, 1))                         # recorder.day_summary
    assert "USING" in _plan(db, "SELECT ts, tool, ms, ok, kind FROM rh_calls WHERE tag=? AND ts>=? AND ts<?",
                            ("recorder", 0, 1))


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


# --------------------------------------------------------------------------- M15: the straddle query reads its windows only
def _old_straddle_vs_realized(path, start, end, entry_ct="08:45", exit_ct="14:30"):
    """The report's straddle check as it was before the windowed query (it read every row in the date range)."""
    from datetime import datetime
    db = wq._connect(path)
    lo = datetime.fromisoformat(f"{start}T00:00").replace(tzinfo=wq.CT).timestamp()
    hi = datetime.fromisoformat(f"{end}T23:59").replace(tzinfo=wq.CT).timestamp()
    rows = db.execute("SELECT ts, expiry, strike, right, bid, ask, spot FROM option_quotes WHERE ts BETWEEN ? AND ? "
                      "ORDER BY ts", (lo, hi)).fetchall()
    db.close()
    by_day: dict = {}
    for r in rows:
        day = wq._ct(r["ts"]).date().isoformat()
        if r["expiry"] == day:
            by_day.setdefault(day, {}).setdefault(r["ts"], []).append(r)
    out = []
    for day, snaps in sorted(by_day.items()):
        e_at = datetime.fromisoformat(f"{day}T{entry_ct}").replace(tzinfo=wq.CT).timestamp()
        x_at = datetime.fromisoformat(f"{day}T{exit_ct}").replace(tzinfo=wq.CT).timestamp()
        entry = None
        for ts in sorted(s for s in snaps if e_at <= s <= e_at + 300):
            rs = snaps[ts]
            spot = rs[0]["spot"]
            k = min({r["strike"] for r in rs}, key=lambda s: abs(s - spot))
            legs = {r["right"]: r for r in rs if r["strike"] == k and r["bid"] > 0 and r["ask"] >= r["bid"]}
            if "call" in legs and "put" in legs:
                entry = (spot, sum((l["bid"] + l["ask"]) / 2 for l in legs.values()))
                break
        after = [s for s in snaps if s >= x_at]
        if entry is None or not after:
            continue
        move = abs(snaps[min(after)][0]["spot"] - entry[0])
        out.append({"day": day, "straddle": entry[1], "move": move, "edge": entry[1] - move})
    return out


def _quotes_db(path):
    """Five sessions recorded every 10 s from 08:30 to 15:00 CT, with the edge cases the windows must keep."""
    from datetime import datetime
    import random
    rng = random.Random(3)
    j = Journal(path)

    def at(day, hms):
        return datetime.fromisoformat(f"{day}T{hms}").replace(tzinfo=wq.CT).timestamp()

    def snap(t, day, spot, expiry=None, call_bid=None):
        k0 = round(spot)
        rows = []
        for k in range(k0 - 2, k0 + 3):
            for right in ("call", "put"):
                mid = max(0.05, 2.0 - 0.4 * abs(k - spot) + rng.uniform(-0.05, 0.05))
                bid = call_bid if (call_bid is not None and right == "call" and k == k0) else round(mid - 0.02, 2)
                rows.append((t, expiry or day, float(k), right, bid, round(mid + 0.02, 2), spot))
        return rows

    rows = []
    days = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-11-02"]      # 11-02: after the DST change
    for i, day in enumerate(days):
        t, stop = at(day, "08:30:00"), at(day, "15:00:00")
        spot = 760.0 + i
        while t <= stop:
            hm = datetime.fromtimestamp(t, wq.CT).strftime("%H:%M")
            spot += rng.uniform(-0.2, 0.2)
            skip = ((day == "2026-10-27" and "14:30" <= hm < "14:41")         # exit falls back to 14:41
                    or (day == "2026-10-28" and "08:45" <= hm <= "08:50")      # no entry in the window: day dropped
                    or (day == "2026-10-29" and hm >= "14:30"))               # no exit at all (but see 23:59:30)
            if not skip:
                bad = day == "2026-10-26" and hm == "08:45"                    # zero bid at 08:45: entry at the next snapshot
                rows += snap(t, day, round(spot, 2), call_bid=0.0 if bad else None)
                rows += snap(t, day, round(spot, 2), expiry="2026-11-20")      # another expiry never counts
            t += 30
    rows += snap(at("2026-10-29", "23:59:30"), "2026-10-29", 771.0)            # late row on a mid-range day: still the exit
    rows += snap(at("2026-11-02", "23:59:30"), "2026-11-02", 700.0)            # after `end` 23:59: never read
    j.record_quotes(rows)
    return days


class CountingConn:
    def __init__(self, db, seen):
        self.db, self.seen = db, seen

    def execute(self, sql, args=()):
        return CountingCursor(self.db.execute(sql, args), self.seen)

    def close(self):
        self.db.close()


class CountingCursor:
    def __init__(self, cur, seen):
        self.cur, self.seen = cur, seen

    def fetchall(self):
        r = self.cur.fetchall()
        self.seen.append(len(r))
        return r

    def fetchone(self):
        r = self.cur.fetchone()
        self.seen.append(1 if r is not None else 0)
        return r


def test_straddle_report_reads_only_its_windows_with_identical_numbers(tmp_path, monkeypatch):
    path = tmp_path / "journal.db"
    _quotes_db(path)
    total = sqlite3.connect(path).execute("SELECT COUNT(*) FROM option_quotes").fetchone()[0]
    for start, end in [("2000-01-01", "2026-11-02"), ("2026-10-27", "2026-10-29"), ("2026-10-26", "2026-10-26"),
                       ("2026-11-03", "2026-11-06")]:
        want = _old_straddle_vs_realized(path, start, end)
        got = wq.straddle_vs_realized(path, start, end)
        assert [d["day"] for d in got.get("per_day", [])] == [d["day"] for d in want]
        for g, w in zip(got.get("per_day", []), want):
            assert g == pytest.approx(w)
    full = wq.straddle_vs_realized(path, "2000-01-01", "2026-11-02")
    assert [d["day"] for d in full["per_day"]] == ["2026-10-26", "2026-10-27", "2026-10-29", "2026-11-02"]
    seen: list[int] = []
    real = wq._connect
    monkeypatch.setattr(wq, "_connect", lambda p: CountingConn(real(p), seen))
    wq.straddle_vs_realized(path, "2000-01-01", "2026-11-02")
    assert sum(seen) < total / 20                    # the 08:45-08:50 rows and one exit snapshot a day, not the table
