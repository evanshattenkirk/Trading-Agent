"""IV recorder: which expiries it records, the iv_history rows, retries and read-only pacing."""
import asyncio
import sys
from datetime import date, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from e_fakes import FakeChains

from agentdesk.clock import at_ct
from agentdesk.iv_recorder import DEFAULTS, IVDay, IVSnapshot, expiries_for, next_events, phase
from agentdesk.journal import Journal

MON = date(2026, 10, 5)
WED, FRI1, FRI2, NOV = date(2026, 10, 7), date(2026, 10, 9), date(2026, 10, 16), date(2026, 11, 6)
NOW = at_ct(MON, time(14, 50))


def ev(sym, d, timing="pm"):
    return {"symbol": sym, "date": d, "timing": timing, "verified": True}


def chains():
    ch = FakeChains(now=NOW)
    ch.px.update(AMD=100.2, XOM=115.3)
    for sym, ks in (("AMD", [95, 100, 105]), ("XOM", [110, 115, 120])):
        for exp in (FRI1, FRI2, NOV):
            ch.chain(sym, exp, ks)
    for exp, iv in ((FRI1, 0.62), (FRI2, 0.55), (NOV, 0.48)):
        ch.set("AMD", exp, 100, "call", 2.40, 2.50, iv)
        ch.set("AMD", exp, 100, "put", 2.20, 2.30, iv - 0.02)
        ch.set("XOM", exp, 115, "call", 1.00, 1.04, 0.22)
        ch.set("XOM", exp, 115, "put", 0.90, 0.94, 0.24)
    ch.chain("AMD", WED, [95, 100, 105])                     # AMD has a Wednesday weekly before its Thursday report
    ch.set("AMD", WED, 100, "call", 1.90, 2.00, 0.66)
    ch.set("AMD", WED, 100, "put", 1.80, 1.90, 0.64)
    return ch


def test_next_events_picks_the_next_unreported_report():                      # review focus 3
    cal = [ev("AMD", date(2026, 7, 28)), ev("AMD", date(2026, 10, 8)), ev("AMD", date(2027, 1, 27)),
           ev("XOM", MON, "am"), ev("XOM", date(2026, 10, 30), "am"), ev("NFLX", MON, "pm"), ev("ZZZ", FRI1)]
    got = next_events(cal, MON, ["AMD", "XOM", "NFLX"])
    assert got["AMD"]["date"] == date(2026, 10, 8)
    assert got["XOM"]["date"] == date(2026, 10, 30)          # this morning's report is already out
    assert got["NFLX"]["date"] == MON                        # after the close today: still ahead
    assert "ZZZ" not in got


def test_expiries_for_records_front_pre_earn_and_d30():
    exps = [MON, FRI1, FRI2, NOV]
    assert expiries_for(exps, MON, None) == {"front": FRI1, "d30": NOV}
    got = expiries_for(exps, MON, ev("AMD", date(2026, 10, 14), "pm"))
    assert got == {"front": FRI1, "d30": NOV, "pre": FRI1, "earn": FRI2}


def test_snapshot_writes_one_row_per_kind_with_T():
    j, ch = Journal(None), chains()
    snap = IVSnapshot(ch, j, ["AMD", "XOM"], set())
    n, failed = asyncio.run(snap.snapshot(NOW, [ev("AMD", date(2026, 10, 8))]))
    assert failed == []
    rows = {(r[0], r[1]): r for r in j.db.execute("SELECT symbol, kind, expiry, strike, atm_iv, T, earnings_date, "
                                                  "call_bid, put_ask, dte FROM iv_history")}
    assert n == len(rows) == 6          # AMD front/pre/earn/d30, XOM front/d30
    amd = rows[("AMD", "earn")]
    assert amd[2] == "2026-10-09" and amd[3] == 100.0 and abs(amd[4] - 0.61) < 1e-9 and amd[5] == 3
    assert amd[6] == "2026-10-08" and amd[7] == 2.40 and amd[8] == 2.30 and amd[9] == 4
    assert rows[("XOM", "front")][5] is None                 # no report in the window
    assert ch.calls.count("quotes") == 2 and ch.calls.count("spots") == 1


def test_rerun_replaces_rows_instead_of_duplicating():
    j, ch = Journal(None), chains()
    snap = IVSnapshot(ch, j, ["AMD"], set())
    asyncio.run(snap.snapshot(NOW, []))
    asyncio.run(snap.snapshot(NOW + 60, []))
    assert j.db.execute("SELECT COUNT(*) FROM iv_history").fetchone()[0] == 2


def test_missing_iv_is_stored_as_null():                                       # review focus 4
    j, ch = Journal(None), chains()
    for exp in (FRI1, FRI2, NOV):
        ch.set("XOM", exp, 115, "call", 1.00, 1.04, None)
        ch.set("XOM", exp, 115, "put", 0.90, 0.94, None)
    asyncio.run(IVSnapshot(ch, j, ["XOM"], set()).snapshot(NOW, []))
    assert {r[0] for r in j.db.execute("SELECT atm_iv FROM iv_history")} == {None}


def test_a_failing_symbol_is_reported_and_the_rest_recorded():
    j, ch = Journal(None), chains()
    del ch.px["XOM"]
    n, failed = asyncio.run(IVSnapshot(ch, j, ["AMD", "XOM"], set()).snapshot(NOW, []))
    assert failed == ["XOM"] and n == 2


def test_warm_lists_every_needed_expiry():
    ch = chains()
    failed = asyncio.run(IVSnapshot(ch, Journal(None), ["AMD", "XOM"], set()).warm(MON, [ev("AMD", date(2026, 10, 8))]))
    assert failed == []
    assert ch.calls.count("strikes") == 6          # AMD front/d30/pre/earn (front=pre=WED listed twice) + XOM 2


def test_iv_history_returns_earlier_cycles_at_the_same_T():
    j = Journal(None)
    rows = [{"day": d, "ts": 0, "symbol": "AMD", "kind": "earn", "atm_iv": iv, "T": 3, "earnings_date": e}
            for d, iv, e in (("2026-01-23", 0.40, "2026-01-28"), ("2026-04-24", 0.45, "2026-04-29"),
                             ("2026-07-23", 0.50, "2026-07-28"), ("2026-10-05", 0.61, "2026-10-08"))]
    rows.append({"day": "2026-07-24", "ts": 0, "symbol": "AMD", "kind": "earn", "atm_iv": 0.55, "T": 2,
                 "earnings_date": "2026-07-28"})
    j.record_iv(rows)
    assert j.iv_history("AMD", "earn", 3, "2026-10-08") == [0.40, 0.45, 0.50]



def at(h, m, d=MON):
    return at_ct(d, time(h, m))


def test_phases_follow_the_ct_clock():
    s = dict(DEFAULTS)
    assert [phase(at(h, m), s) for h, m in ((13, 29), (13, 30), (14, 49), (14, 50), (14, 59), (15, 0))] == \
        ["wait", "list", "list", "quote", "quote", "closed"]
    assert phase(at(14, 50, date(2026, 10, 10)), s) == "closed"          # Saturday


class Snap:
    def __init__(self, fail_first=()):
        self.warmed, self.shots, self.fail = 0, [], list(fail_first)
        self.chains = FakeChains()

    async def warm(self, today, cal):
        self.warmed += 1
        return []

    async def snapshot(self, now, cal, symbols=None):
        self.shots.append((now, symbols))
        failed, self.fail = self.fail, []
        return 3, failed


def test_ivday_lists_once_then_quotes_retries_failures_and_logs_the_day():
    logs, snap = [], Snap(fail_first=["XOM"])

    async def cal(today):
        return []
    lp, qp = object(), object()
    day = IVDay(snap, cal, dict(DEFAULTS), list_pacer=lp, quote_pacer=qp, log_fn=logs.append)

    async def go():
        for t in (at(13, 29), at(13, 30), at(13, 31)):
            await day.step(t)
        assert snap.warmed == 1 and snap.chains.pacer is lp
        await day.step(at(14, 50))
        assert snap.shots == [(at(14, 50), None)] and day.left == ["XOM"] and snap.chains.pacer is qp
        await day.step(at(14, 50) + 10)                  # inside retry_sec: nothing
        await day.step(at(14, 50) + 30)
        assert snap.shots[-1] == (at(14, 50) + 30, ["XOM"]) and day.done
        await day.step(at(14, 55))
        assert len(snap.shots) == 2
    asyncio.run(go())
    assert logs == [{"iv_day": "2026-10-05", "rows": 6, "failed": []}]


def test_ivday_logs_what_is_still_missing_at_the_stop_time_and_resets_next_day():
    logs, snap = [], Snap(fail_first=["XOM"])

    async def cal(today):
        return []
    day = IVDay(snap, cal, {**DEFAULTS, "retry_sec": 10_000}, log_fn=logs.append)

    async def go():
        await day.step(at(14, 50))
        await day.step(at(15, 0))
        await day.step(at(14, 50, date(2026, 10, 6)))
    asyncio.run(go())
    assert logs == [{"iv_day": "2026-10-05", "rows": 3, "failed": ["XOM"]},
                    {"iv_day": "2026-10-06", "rows": 3, "failed": []}]      # the new day starts clean and records all
    assert day.day == date(2026, 10, 6) and len(snap.shots) == 2 and snap.shots[-1][1] is None
