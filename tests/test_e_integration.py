"""Book E inside the engine: HostGroup risk sharing, paper-only build, watchdog and shutdown exemptions."""
import asyncio
import logging
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from books_fakes import FakeEngine, FakeQuotes
from e_fakes import FakeChains
from test_e_host import MON, at, make, tick

from agentdesk import lifecycle
from agentdesk.books.e_host import EHost, build_e
from agentdesk.books.group import HostGroup
from agentdesk.books.host import BookHost
from agentdesk.config import load_config
from agentdesk.engine import Engine
from agentdesk.proposals import TWEAKS, validate


def test_group_shares_one_account_and_sums_open_risk():
    h, eng, ch = make()
    bh = BookHost(eng, eng.cfg, books=[])
    bh.books = [types.SimpleNamespace(open=[], letter="B")]           # enabled, nothing open
    g = HostGroup(bh, None, h)
    tick(h, ch, at(MON, 14, 45))
    assert h.account is bh.account and g.open_risk() == 627.0
    assert h.other_risk() == 0.0 and bh.open_risk() == 627.0
    assert [b.letter for b in g.books] == ["B", "E"]


def test_group_without_bookhost_still_shares_risk_between_f_and_e():
    h, eng, ch = make()
    f = types.SimpleNamespace(account=h.account.__class__(), other_risk=lambda: 0.0, open_risk=lambda: 100.0,
                              book=types.SimpleNamespace(letter="F1"))
    g = HostGroup(None, f, h)
    assert f.account is h.account
    assert h.other_risk() == 100.0 and f.other_risk() == 0.0 and g.open_risk() == 100.0


def test_books_event_lists_e_and_f():                                          # review focus 5
    h, eng, ch = make()
    bh = BookHost(eng, eng.cfg, books=[])
    HostGroup(bh, None, h)
    assert [b["book"] for b in bh.summary()] == ["A", "E"]


def test_build_e_is_paper_only_and_idle_in_sim():
    cfg = load_config()
    eng = FakeEngine(FakeQuotes(), cfg)
    assert build_e(eng, cfg, provider="sim") is None
    assert build_e(eng, cfg, provider="alpaca", rh=None) is None
    h = build_e(eng, cfg, provider="alpaca", chains=FakeChains())
    assert isinstance(h, EHost) and h.broker is not eng.broker and h.exec.reviewer is None
    rh = object()
    h = build_e(eng, cfg, mode="live", provider="alpaca", rh=rh, chains=FakeChains())
    assert h.exec.reviewer is rh and h.broker is not eng.broker          # live only reviews; fills stay paper
    cfg["books"]["E_earnings_iv"]["paper_only"] = False
    with pytest.raises(SystemExit):
        build_e(eng, cfg, provider="alpaca", chains=FakeChains())


def test_watchdog_ignores_overnight_e_positions():                              # review focus 1
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 45))
    trips = []
    ns = types.SimpleNamespace(wd={"quote_stale_sec": 10, "reconcile_sec": 0}, open=[], books=h,
                               _trip=lambda reason, now: trips.append(reason),
                               broker=types.SimpleNamespace(live=False))
    Engine._watchdog(ns, at(MON, 14, 45) + 18 * 3600)                  # 08:45 next day
    assert trips == []


def test_shutdown_leaves_e_positions_out_of_the_still_open_error(caplog):
    h, eng, ch = make()
    tick(h, ch, at(MON, 14, 45))
    engine = types.SimpleNamespace(open=[], books=h, broker=types.SimpleNamespace(live=False))

    async def flatten(reason):
        await h.flatten(reason, at(MON, 15, 9))
    engine.flatten = flatten
    with caplog.at_level(logging.WARNING):
        asyncio.run(lifecycle._sell_open_positions(engine, 2.0))
    assert len(h.book.open) == 2
    assert not [r for r in caplog.records if "still open" in r.getMessage()]


def test_crew_tweaks_cannot_touch_book_e():
    assert not [k for k in TWEAKS if k.startswith("books.")]
    ok, _, _ = validate(load_config(), "books.E_earnings_iv.max_debit", 900)
    assert not ok
