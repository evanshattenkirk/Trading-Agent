import asyncio

import pytest

from books_fakes import FakeEngine, FakeQuotes, ct_ts
from agentdesk.books.base import ExitIntent, Leg, OrderIntent, Strategy
from agentdesk.books.book import Book
from agentdesk.books.host import BookHost

FLY = [Leg("call", 765, "sell"), Leg("put", 765, "sell"), Leg("call", 770, "buy"), Leg("put", 760, "buy")]
TIGHT = [(2.00, 2.02), (1.90, 1.92), (0.40, 0.41), (0.35, 0.36)]


def run(c):
    return asyncio.run(c)


class Scripted(Strategy):
    name = "TEST"

    def __init__(self, cfg=None, intent=None):
        super().__init__(cfg or {})
        self.intent, self.exit, self.boom, self.failed = intent, None, False, 0

    def on_clock(self, now, ctx):
        if self.boom:
            raise RuntimeError("boom")
        it, self.intent = self.intent, None
        return it

    def on_quote(self, pos, cq, now, ctx):
        return self.exit

    def entry_failed(self, now):
        self.failed += 1


def setup(intents=("B",), rows=TIGHT):
    fq = FakeQuotes(now=ct_ts(8, 45))
    for l, (b, a) in zip(FLY, rows):
        fq.set(l.right, l.strike, b, a)
    eng = FakeEngine(fq)
    books = [Book(f"{k}_test", {"max_trades_day": 1}, Scripted(intent=OrderIntent(list(FLY), True, 5.0, "test fly", lots=1)))
             for k in intents]
    return eng, fq, BookHost(eng, eng.cfg, books=books)


def tick(host, fq, now):
    fq.now = now
    run(host.on_second(now))


def test_entry_opens_combo_at_paper_fill_with_fees():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    (pos,) = host.positions()
    assert pos.entry == pytest.approx(3.13) and pos.qty == 1 and pos.fees == pytest.approx(0.16)
    assert pos.fills[0]["mid"] == pytest.approx(3.16) and pos.fills[0]["natural"] == pytest.approx(3.13)
    assert eng.bus.of("book_position")[0]["event"] == "open"
    assert host.open_risk() == pytest.approx((5 - 3.13) * 100)


def test_exit_closes_journals_and_books_pnl():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    host.books[0].strategy.exit = ExitIntent("take profit 50%")
    for l, (b, a) in zip(FLY, [(0.90, 0.92), (0.80, 0.82), (0.10, 0.11), (0.08, 0.09)]):
        fq.set(l.right, l.strike, b, a)
    tick(host, fq, ct_ts(9, 30))
    assert not host.positions()
    row = eng.journal.trades()[0]
    assert row["book"] == "B" and row["exit_reason"] == "take profit 50%"
    b = host.books[0]
    assert b.wins == 1 and b.day_pnl == pytest.approx(host.account.realized)
    assert eng.bus.of("book_closed")[0]["pos"]["book"] == "B"


def test_open_risk_cap_skips_entry():
    eng, fq, host = setup()
    host.account.c["open_risk_cap"] = 100
    tick(host, fq, ct_ts(8, 45))
    assert not host.positions() and "open-risk cap" in eng.bus.of("book_skip")[0]["why"]


def test_book_a_open_debit_counts_toward_open_risk():
    eng, fq, host = setup()

    class APos:
        entry, qty = 2.50, 4
    eng.open = [(APos(), None)]
    assert host.open_risk() == pytest.approx(1000.0)


def test_kill_flattens_every_book_and_blocks_entries():
    eng, fq, host = setup(intents=("B", "D"))
    tick(host, fq, ct_ts(8, 45))
    assert len(host.positions()) == 2
    run(host.kill(ct_ts(9, 0)))
    assert not host.positions() and host.account.halted
    assert all(r["exit_reason"] == "KILL switch" for r in eng.journal.trades())


def test_engine_safety_trip_flattens_books_but_a_daily_loss_halt_does_not():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    eng.risk.halt("daily loss limit -$400")                 # book A's own halt
    tick(host, fq, ct_ts(8, 46))
    assert host.positions()
    eng.risk.halt("SAFETY: stale quote", flatten=True)      # what Engine._trip / kill set
    tick(host, fq, ct_ts(8, 47))
    assert not host.positions()


def test_forced_flatten_times():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    tick(host, fq, ct_ts(14, 40))
    assert eng.journal.trades()[0]["exit_reason"] == "flatten 14:40 CT"


def test_sellout_forces_exit_five_minutes_early():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    for c in host.positions()[0].contracts:
        c.sellout_ts = ct_ts(10, 0)
    tick(host, fq, ct_ts(9, 55))
    assert "sellout" in eng.journal.trades()[0]["exit_reason"]


def test_stale_leg_quote_makes_no_exit_decision():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    pos = host.positions()[0]
    host.books[0].strategy.exit = ExitIntent("would exit")
    fq.set("put", 760, 0.35, 0.36, ts=ct_ts(8, 45))         # 15 s old at 08:45:15
    tick(host, fq, ct_ts(8, 45, 15))
    assert host.positions() and pos.last_quote_ts == ct_ts(8, 45)


def test_a_book_that_keeps_raising_halts_alone():               # review focus 4
    eng, fq, host = setup(intents=("B", "D"))
    tick(host, fq, ct_ts(8, 45))
    host.books[0].strategy.boom = True
    for s in range(1, 5):
        tick(host, fq, ct_ts(8, 46, s))
    b, d = host.books
    assert b.halted and "boom" in b.halt_reason and not b.open
    assert not d.halted and d.open and not host.account.halted and not eng.risk.st.halted


def test_partial_fill_blocks_new_orders():
    eng, fq, host = setup()

    async def partial(*a, **k):
        from agentdesk.brokers.base import OrderResult
        return OrderResult("partial", 1, 3.13, "x", raw={"mid": 3.16, "natural": 3.13, "ref_id": "r", "tries": 1})
    host.books[0].strategy.intent = OrderIntent(list(FLY), True, 5.0, "two lots", lots=2)
    host.account.c["per_position_max_loss"] = 1000
    host.exec.work = partial
    tick(host, fq, ct_ts(8, 45))
    b = host.books[0]
    assert b.open[0].qty == 1 and "partial" in b.blocked and not b.can_enter()[0]


def test_unfillable_entry_tells_strategy_to_retry():            # review focus 3 (host side)
    eng, fq, host = setup()
    fq.set("put", 760, 0.35, 0.36, ts=ct_ts(8, 40))           # stale leg at 08:45
    tick(host, fq, ct_ts(8, 45))
    assert not host.positions() and host.books[0].strategy.failed == 1


def test_early_close_day_blocks_late_entries_and_flattens_1140():
    eng, fq, host = setup()
    eng.cfg["calendar"]["early_close"] = ["2026-09-28"]
    tick(host, fq, ct_ts(8, 45))
    tick(host, fq, ct_ts(11, 40))
    assert "11:40" in eng.journal.trades()[0]["exit_reason"]


def test_snapshot_lists_book_a_first():
    eng, fq, host = setup()
    snap = host.snapshot()
    assert [b["book"] for b in snap["books"]] == ["A", "B"] and "open_risk" in snap["account"]


def test_start_announces_the_books_so_a_replay_shows_the_strip():
    eng, fq, host = setup()
    run(host.start())
    (ev,) = eng.bus.of("books")
    assert [b["book"] for b in ev["books"]] == ["A", "B"] and "open_risk" in ev["account"]


def test_position_updates_carry_only_the_live_fields():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    tick(host, fq, ct_ts(8, 45, 3))
    up = [d for d in eng.bus.of("book_position") if d["event"] == "update"][0]["pos"]
    assert "legs" not in up and "fills" not in up and {"id", "book", "mark", "total_pnl"} <= set(up)


def test_safety_flatten_does_not_wait_for_a_clean_quote():            # review #3
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    host.halt_all("SAFETY: stale quote", flatten=True)
    fq.set("put", 760, 0.40, 0.35)                                   # crossed wing
    tick(host, fq, ct_ts(8, 46))
    assert not host.positions() and "SAFETY" in eng.journal.trades()[0]["exit_reason"]


def test_flatten_with_a_missing_leg_closes_at_the_last_mark():
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    mark = host.positions()[0].mark
    del fq.book[("put", 760.0)]
    tick(host, fq, ct_ts(14, 40))
    row = eng.journal.trades()[0]
    assert not host.positions() and "last mark" in row["exit_reason"]
    assert row["pnl"] == pytest.approx((3.13 - mark) * 100 - 0.32)


def test_a_hanging_vix_fetch_never_stalls_position_management():     # review #6
    eng, fq, host = setup()
    tick(host, fq, ct_ts(8, 45))
    pos = host.positions()[0]

    class Hang:
        async def prior_close(self, day):
            await asyncio.Event().wait()
    host.vix = Hang()

    async def go():
        for s in (1, 2):
            fq.now = ct_ts(8, 46, s)
            await host.run(host.on_second(ct_ts(8, 46, s)), inline=False)
            await asyncio.sleep(0.01)
    run(go())
    assert pos.last_quote_ts == ct_ts(8, 46, 2)


@pytest.mark.parametrize("raw,want", [(True, True), (False, False), ("false", False), ("True", True), ("n/a", None)])
def test_vix1d_flag_is_parsed_strictly(raw, want):                    # review #8
    eng, fq, host = setup()

    class Crew:
        briefs = {"vol": {"ts": ct_ts(8, 25), "vix1d_flag": raw}}
    eng.crew = Crew()
    assert host._ctx(ct_ts(8, 45), host.books[0]).vix1d_flag is want
