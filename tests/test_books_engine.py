import asyncio
import copy

import pytest

from books_fakes import FakeFeed, FakeQuotes, ct_ts
from agentdesk.__main__ import check_live_promotion
from agentdesk.books.base import Leg, OrderIntent
from agentdesk.books.book import Book
from agentdesk.books.host import BookHost, build_books
from agentdesk.brokers.paper import PaperBroker
from agentdesk.bus import Bus
from agentdesk.config import load_config
from agentdesk.engine import Engine
from agentdesk.journal import Journal
from agentdesk.proposals import TWEAKS, ProposalBook, validate

from test_books_host import FLY, TIGHT, Scripted

CFG = load_config()


def engine_with_books():
    fq = FakeQuotes(now=ct_ts(8, 45))
    for l, (b, a) in zip(FLY, TIGHT):
        fq.set(l.right, l.strike, b, a)
    feed = FakeFeed()
    e = Engine(copy.deepcopy(CFG), feed, fq, PaperBroker(fq), Bus(), Journal(None), "paper")
    e.inline = True                 # run hooks in-line (as in sim) so each step finishes before the asserts
    e.price = 765.0
    e.vwap.add(765.0, 1)
    e.books = BookHost(e, e.cfg, books=[Book("B_test", {}, Scripted(intent=OrderIntent(list(FLY), True, 5.0, "t", lots=1)))])
    return e, fq, feed


def step(e, fq, feed, now):
    fq.now = feed.t = now
    asyncio.run(e._on_second(now))


def test_engine_runs_books_each_second_and_snapshot_has_them():
    e, fq, feed = engine_with_books()
    step(e, fq, feed, ct_ts(8, 45))
    assert len(e.books.positions()) == 1
    assert [b["book"] for b in e.snapshot()["books"]["books"]] == ["A", "B"]


def test_kill_flattens_books_too():
    e, fq, feed = engine_with_books()
    step(e, fq, feed, ct_ts(8, 45))

    async def go():
        await e.kill()
    asyncio.run(go())
    assert not e.books.positions() and e.books.account.halted


def test_watchdog_trips_on_a_stale_combo():
    e, fq, feed = engine_with_books()
    step(e, fq, feed, ct_ts(8, 45))
    pos = e.books.positions()[0]

    async def go():
        e._watchdog(pos.last_quote_ts + 11)
    asyncio.run(go())
    assert e.risk.st.halted and "no fresh quote" in e.risk.st.halt_reason and e.books.account.flatten


def test_live_mode_needs_exactly_one_promoted_book():
    check_live_promotion(CFG, "paper")                          # paper: nothing to check
    with pytest.raises(SystemExit, match="paper_only: false"):
        check_live_promotion(CFG, "live")                       # every book is paper_only today
    cfg = copy.deepcopy(CFG)
    cfg["books"]["A_macd_calls"]["paper_only"] = False
    check_live_promotion(cfg, "live")
    cfg["books"]["C_orb_bull_put"]["paper_only"] = False
    with pytest.raises(SystemExit):
        check_live_promotion(cfg, "live")


def test_books_refuse_to_build_without_paper_only():
    cfg = copy.deepcopy(CFG)
    cfg["books"]["D_iron_condor"]["paper_only"] = False
    with pytest.raises(SystemExit, match="D_iron_condor"):
        build_books(cfg)
    assert [b.letter for b in build_books(CFG)] == ["B", "C", "D", "G"]


def test_book_code_has_no_order_placing_path():
    import pathlib
    src = "".join(p.read_text() for p in pathlib.Path("agentdesk/books").glob("*.py"))
    for tool in ("place_option_order", "cancel_option_order", "exercise_option"):
        assert tool not in src


def test_proposals_cannot_touch_books():
    assert not any(k.startswith("books.") for k in TWEAKS)
    assert validate(CFG, "books.C_orb_bull_put.max_loss_budget", 100)[0] is False
    item = ProposalBook(CFG, None).submit("quant", {"scope": "day", "title": "more C", "params": {"books.B_iron_fly.lots": 2}}, 0.0)
    assert item["status"].startswith("rejected")


def test_vol_desk_is_asked_for_vix1d():
    from agentdesk.crew import DESKS
    assert "vix1d_flag" in DESKS["vol"].role


def test_vol_desk_vix1d_flag_reaches_book_b_same_day_only():
    e, fq, feed = engine_with_books()

    class Crew:
        briefs = {"vol": {"ts": ct_ts(8, 25), "vix1d_flag": True}}
    e.crew = Crew()
    book = e.books.books[0]
    assert e.books._ctx(ct_ts(8, 45), book).vix1d_flag is True
    Crew.briefs["vol"]["ts"] = ct_ts(8, 25) - 86400             # yesterday's brief says nothing about today
    assert e.books._ctx(ct_ts(8, 45), book).vix1d_flag is None


def test_books_never_reset_book_a_error_count():                    # review #1
    e, fq, feed = engine_with_books()

    async def broker_503():
        raise RuntimeError("robinhood 503")

    async def go():
        for s in range(3):
            await e._run(broker_503(), "manage")
            fq.now = feed.t = ct_ts(8, 45, s)
            await e._on_second(ct_ts(8, 45, s))            # books run cleanly every second
    asyncio.run(go())
    assert e.risk.st.halted and "3 broker/API errors" in e.risk.st.halt_reason


def test_a_host_level_books_failure_halts_books_not_a():
    e, fq, feed = engine_with_books()

    async def boom(now):
        raise RuntimeError("host bug")
    e.books.on_second = boom
    for s in range(4):
        step(e, fq, feed, ct_ts(8, 45, s))
    assert e.books.account.halted and "host bug" in e.books.account.halt_reason
    assert not e.risk.st.halted and e._errors == 0


def test_iex_history_and_live_share_a_scale_so_c_keeps_its_volume_baseline():   # review #5
    from agentdesk.__main__ import build
    cfg = copy.deepcopy(CFG)
    cfg["data"]["provider"], cfg["data"]["alpaca"]["feed"], cfg["data"]["quotes_source"] = "alpaca", "iex", "alpaca"
    build(cfg, "paper", 1, 1)
    assert not cfg["books"]["C_orb_bull_put"].get("reset_volume_on_live")


def test_flatten_still_flattens_books_when_a_book_a_exit_fails():    # review #13
    e, fq, feed = engine_with_books()
    step(e, fq, feed, ct_ts(8, 45))

    class APos:
        qty = 1

    async def broken_exit(*a, **k):
        raise RuntimeError("robinhood 503")
    e.open, e.exit = [(APos(), None)], broken_exit
    with pytest.raises(RuntimeError):
        asyncio.run(e.flatten())
    assert not e.books.positions()
