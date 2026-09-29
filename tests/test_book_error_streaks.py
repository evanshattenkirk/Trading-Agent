"""A book halts after max_consecutive_errors failures in a row. Each tick BookHost runs a book's clock check and then
manages its open position; a clean clock check used to reset the count, so a book whose position management failed
every second never halted (found 2026-09-29). Now each kind of call keeps its own streak and the worst one counts."""
import asyncio

from books_fakes import FakeEngine, ct_ts
from test_rate_budget import FLY, Opener, RHQuotes
from agentdesk.books.book import Book
from agentdesk.books.host import BookHost


def run(c):
    return asyncio.run(c)


class BrokenManage(Opener):
    def on_quote(self, pos, cq, now, ctx):
        raise RuntimeError("exit check broke")


def test_a_book_whose_position_checks_keep_failing_halts_and_flattens():
    q = RHQuotes(ct_ts(9, 0))
    for l in FLY:
        q.set(l.right, l.strike, 1.0, 1.02)
    eng = FakeEngine(q)
    book = Book("B_test", {"max_trades_day": 1}, BrokenManage(FLY))
    host = BookHost(eng, eng.cfg, books=[book])
    q.now = ct_ts(9, 0)
    run(host.on_second(ct_ts(9, 0)))
    assert book.open
    for i in range(1, 6):
        q.now = ct_ts(9, 0, i)
        run(host.on_second(ct_ts(9, 0, i)))
    assert book.halted and not book.open


def test_a_success_of_one_kind_does_not_clear_another_kinds_streak():
    book = Book("B_test", {}, None)
    for _ in range(2):
        book.failed("manage")
        book.succeeded("clock")
    assert book.errors == 2
    book.succeeded("manage")
    assert book.errors == 0


def _ok():
    async def fine():
        return None
    return fine()


def _broken():
    async def broken():
        raise RuntimeError("boom")
    return broken()


def _hosts():
    from test_e_host import make as make_e
    from test_f_host import make as make_f
    return [make_e()[0], make_f()[2]]


def test_books_e_and_f_halt_when_one_hook_keeps_failing_between_clean_ones():
    for h in _hosts():
        for i in range(h.max_errors):
            run(h._safe(_ok(), 1000.0 + i))
            run(h._safe(_broken(), 1000.0 + i))
        assert h.book.halted, h.book.letter
