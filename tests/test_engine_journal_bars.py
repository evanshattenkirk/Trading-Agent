"""Engine fixes from the 2026-10-06 review: a journal write that fails never loses the trade or its event (M12),
scale-outs round half up and roll back cleanly (D2, L7), the spread step never goes ITM from ATM, pre-market prints
stay out of the 144t series, and a mid-day restart continues today's bars instead of leaving a hole (M11)."""
import asyncio
import logging
import sqlite3
import sys
from datetime import date, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.exits import ExitIntent

from test_safety import NOW, engine, open_pos, run


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    real = asyncio.sleep

    async def no_wait(_s, *a, **k):
        await real(0)
    monkeypatch.setattr(asyncio, "sleep", no_wait)


def taps(e) -> list[dict]:
    seen: list[dict] = []
    e.bus.taps.append(seen.append)
    return seen


# --------------------------------------------------------------------------- M12: record_trade failure
def failing_journal(e, fails: int):
    real = e.journal.record_trade
    calls = []

    def record_trade(*a, **k):
        calls.append(a)
        if len(calls) <= fails:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    e.journal.record_trade = record_trade
    return calls


def test_a_locked_journal_is_retried_once_and_the_trade_lands():
    e = engine()
    pos = open_pos(e, qty=2)
    calls = failing_journal(e, fails=1)
    seen = taps(e)
    run(e.exit(pos, e.open[0][1], ExitIntent(2, "test", urgent=True), NOW))
    assert len(calls) == 2 and len(e.journal.trades()) == 1
    assert [x for x in seen if x["type"] == "trade_closed"]
    assert not e._streaks and e._errors == 0


def test_a_journal_that_keeps_failing_still_emits_trade_closed_and_never_counts_as_an_error(caplog):
    e = engine()
    pos = open_pos(e, qty=2)
    calls = failing_journal(e, fails=99)
    seen = taps(e)
    with caplog.at_level(logging.ERROR, logger="agentdesk.engine"):
        run(e._guard(e.exit(pos, e.open[0][1], ExitIntent(2, "test", urgent=True), NOW), "manage"))
    assert len(calls) == 2 and e.journal.trades() == []
    closed = [x for x in seen if x["type"] == "trade_closed"]
    assert closed and closed[0]["pos"]["id"] == pos.id
    assert not e.open and e.closed == [pos]
    assert not e._streaks and e._errors == 0 and not e.risk.st.halted        # no strike toward the safety halt
    assert "not journaled" in caplog.text and f"'id': {pos.id}" in caplog.text     # the trade dict is in the log
    assert any(x["type"] == "log" and x["level"] == "error" and "not journaled" in x["msg"] for x in seen)
