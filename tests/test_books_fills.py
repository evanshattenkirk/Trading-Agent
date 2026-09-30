import asyncio

import pytest

from books_fakes import FILLS, FakeQuotes, contracts
from agentdesk.books.base import Leg
from agentdesk.books.fills import ComboExecutor, rh_legs
from agentdesk.books.legs import fetch_quotes
from agentdesk.brokers.paper import PaperBroker
from agentdesk.brokers.robinhood import order_args
from agentdesk.exits import Contract

FLY = [Leg("call", 765, "sell"), Leg("put", 765, "sell"), Leg("call", 770, "buy"), Leg("put", 760, "buy")]
TIGHT = [(2.00, 2.02), (1.90, 1.92), (0.40, 0.41), (0.35, 0.36)]      # mid 3.16, natural 3.13
WIDE = [(1.95, 2.07), (1.85, 1.97), (0.35, 0.45), (0.30, 0.40)]       # mid 3.17, natural 2.95, fair 3.13


def run(c):
    return asyncio.run(c)


def quotes(rows):
    fq = FakeQuotes()
    for l, (b, a) in zip(FLY, rows):
        fq.set(l.right, l.strike, b, a)
    return fq


def broker(fq):
    pb = PaperBroker(fq)
    pb.combo_model, pb.combo_cents = "mid_offset", 0.01
    return pb


def test_submit_combo_fills_at_fair_or_rests():
    pb = broker(quotes(TIGHT))
    cs = contracts(FLY)
    assert run(pb.submit_combo(FLY, cs, 1, 3.16, True, True, 100.0)).status == "unfilled"
    r = run(pb.submit_combo(FLY, cs, 1, 3.13, True, True, 100.0))
    assert r.status == "filled" and r.filled_qty == 1 and r.avg_price == pytest.approx(3.13)
    assert r.raw["mid"] == pytest.approx(3.16) and r.raw["natural"] == pytest.approx(3.13)
    assert run(pb.submit_combo(FLY, cs, 1, 3.00, True, True, 100.0)).avg_price == pytest.approx(3.13)


def test_submit_combo_rejects_missing_leg_quote():
    fq = quotes(TIGHT)
    del fq.book[("put", 760.0)]
    assert run(broker(fq).submit_combo(FLY, contracts(FLY), 1, 3.0, True, True, 100.0)).status == "rejected"


def test_executor_walks_from_mid_toward_natural():
    ex = ComboExecutor(broker(quotes(WIDE)), FILLS)
    r = run(ex.work(FLY, contracts(FLY), 1, True, True, False, 100.0))
    assert r.status == "filled" and r.avg_price == pytest.approx(3.13)
    assert r.raw["tries"] == 2 and r.raw["ref_id"] and r.raw["limit"] == pytest.approx(3.13)


def test_executor_urgent_starts_at_natural():
    ex = ComboExecutor(broker(quotes(WIDE)), FILLS)
    r = run(ex.work(FLY, contracts(FLY), 1, True, False, True, 100.0))     # buy the fly back now
    assert r.raw["tries"] == 1 and r.raw["limit"] == pytest.approx(3.39)
    assert r.avg_price == pytest.approx(3.21)                             # paper fair: mid + 4c


def test_executor_natural_model_always_ends_filled():
    pb = broker(quotes(WIDE))
    pb.combo_model = "natural"
    r = run(ComboExecutor(pb, {**FILLS, "model": "natural"}).work(FLY, contracts(FLY), 1, True, True, False, 100.0))
    assert r.status == "filled" and r.avg_price == pytest.approx(2.95) and r.raw["tries"] == 5


def test_multi_leg_direction_open_credit_close_debit():
    ids = ["c765", "p765", "c770", "p760"]
    opening = rh_legs(FLY, ids, opening=True)
    assert opening[0] == {"option_id": "c765", "side": "sell", "position_effect": "open", "ratio_quantity": 1}
    assert order_args("acct", opening, 1, 3.13, True)["direction"] == "credit"
    closing = rh_legs(FLY, ids, opening=False)
    assert closing[0]["side"] == "buy" and closing[2] == {"option_id": "c770", "side": "sell",
                                                           "position_effect": "close", "ratio_quantity": 1}
    assert order_args("acct", closing, 1, 1.50, True)["direction"] == "debit"


class FakeRH:
    account = "123456"

    def __init__(self):
        self.calls = []

    async def instrument_id(self, c):
        return f"{c.right[0]}{c.strike:g}"

    async def call(self, tool, args):
        self.calls.append((tool, args))
        return {"ok": True}


def test_shadow_reviews_once_per_order_and_never_places():
    rh = FakeRH()
    ex = ComboExecutor(broker(quotes(WIDE)), FILLS, reviewer=rh)
    r = run(ex.work(FLY, contracts(FLY), 1, True, True, False, 100.0))
    assert [t for t, _ in rh.calls] == ["review_option_order"]
    assert rh.calls[0][1]["direction"] == "credit" and r.review == {"ok": True}
    assert r.status == "filled"                        # the fill itself is still paper


class RHQ:
    """Stand-in for RobinhoodQuotes whose batched get_option_quotes answers with `shape(ids)`."""
    def __init__(self, shape):
        self.rh, self.cache = FakeRH(), {}

        async def call(tool, args):
            self.rh.calls.append((tool, args))
            return shape(args["instrument_ids"])
        self.rh.call = call

    async def quote(self, c):
        raise AssertionError("should batch")


def test_fetch_quotes_batches_robinhood_calls():
    # Robinhood's real shape (recorder thread, 2026-09-28): prices nested under results[].quote
    src = RHQ(lambda ids: {"results": [{"instrument_id": i, "quote": {"bid_price": "1.00", "ask_price": "1.02"}}
                                       for i in ids]})
    qs = run(fetch_quotes(src, contracts(FLY)))
    assert len(src.rh.calls) == 1 and all(q.bid == 1.0 and q.ask == 1.02 for q in qs)
    assert len(src.cache) == 4


def test_fetch_quotes_accepts_flat_items_and_missing_legs():
    src = RHQ(lambda ids: {"quotes": [{"instrument_id": i, "bid_price": "1.00", "ask_price": "1.02"} for i in ids[:3]]})
    qs = run(fetch_quotes(src, contracts(FLY)))
    assert [q is None for q in qs] == [False, False, False, True]    # a missing leg is None, never a stale guess


def test_fetch_quotes_treats_a_missing_bid_as_zero():                 # review #11
    src = RHQ(lambda ids: {"results": [{"instrument_id": i, "quote": {"bid_price": None, "ask_price": "0.01"}} for i in ids]})
    qs = run(fetch_quotes(src, contracts(FLY)))
    assert all(q is not None and q.bid == 0.0 and q.ask == 0.01 for q in qs)


# The review's direction comes from the position's credit flag, never from leg order. E2 and G list their sold
# (near-expiry) leg first but are paid debits; before this fix their shadow reviews went out as "credit".
def g_legs():
    from datetime import date
    from books_fakes import ct_ts
    from agentdesk.books.base import MarketContext
    from agentdesk.books.call_calendar import CallCalendar
    from agentdesk.config import load_config
    s = CallCalendar(load_config()["books"]["G_call_calendar"])
    now = ct_ts(9, 0)
    return s.on_clock(now, MarketContext(now=now, day=date(2026, 9, 28), spot=765.3, vwap=765.3)).legs


def e_legs(structure):
    from datetime import date
    from agentdesk.books.earnings_iv import legs_for
    return legs_for(structure, 150.0, date(2026, 10, 1), {"short": date(2026, 10, 9), "long": date(2026, 10, 16)})


def f2_legs():
    from agentdesk.books.f2_spreads import legs_for
    return legs_for("C", 150.0, 155.0, 7)


def c_legs():
    return [Leg("put", 760, "sell"), Leg("put", 758, "buy")]            # orb_bull_put.py


def d_legs():
    return [Leg("call", 775, "sell"), Leg("put", 755, "sell"), Leg("call", 777, "buy"), Leg("put", 753, "buy")]


BOOK_LEGS = {
    "B": (lambda: FLY, True), "C": (c_legs, True), "D": (d_legs, True),
    "E1": (lambda: e_legs("straddle_t3"), False), "E2": (lambda: e_legs("calendar_t10"), False),
    "F2": (f2_legs, False), "G": (g_legs, False),
}


@pytest.mark.parametrize("book", sorted(BOOK_LEGS))
@pytest.mark.parametrize("opening", [True, False])
def test_review_direction_follows_the_credit_flag(book, opening):
    make, credit = BOOK_LEGS[book]
    legs = make()
    rh = FakeRH()
    ex = ComboExecutor(broker(quotes(TIGHT)), FILLS, reviewer=rh)
    run(ex._review(legs, contracts(legs), 1, 1.00, opening, credit))
    receiving = credit == opening
    assert rh.calls[0][0] == "review_option_order"
    assert rh.calls[0][1]["direction"] == ("credit" if receiving else "debit")


def test_g_calendar_shadow_review_is_a_debit_and_fill_stays_paper():
    legs = g_legs()
    assert legs[0].side == "sell"                                       # leg order is unchanged
    fq = FakeQuotes()
    fq.set("call", 765, 1.00, 1.02, expiry="2026-09-28")
    fq.set("call", 765, 2.00, 2.02, expiry="2026-09-29")
    cs = [Contract("SPY", "2026-09-28", 765.0, "call"), Contract("SPY", "2026-09-29", 765.0, "call")]
    rh = FakeRH()
    r = run(ComboExecutor(broker(fq), FILLS, reviewer=rh).work(legs, cs, 1, False, True, False, 100.0))
    assert [t for t, _ in rh.calls] == ["review_option_order"]
    assert rh.calls[0][1]["direction"] == "debit"
    assert r.status == "filled"                                         # the fill itself is still paper


def test_order_args_explicit_direction_overrides_leg_order():
    legs = [{"option_id": "S", "side": "sell", "position_effect": "open"},
            {"option_id": "L", "side": "buy", "position_effect": "open"}]
    assert order_args("acct", legs, 1, 1.0, True)["direction"] == "credit"              # inferred
    assert order_args("acct", legs, 1, 1.0, True, direction="debit")["direction"] == "debit"
    single = order_args("acct", legs[:1], 1, 1.0, True, direction="debit")
    assert "direction" not in single                                                     # single leg: none
