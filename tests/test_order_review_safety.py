"""Order review replies and order-placement retries (review 2026-10-06, L9 and L12).

L9: review_option_order / review_equity_order echo the account number; what is stored (events.jsonl, the journal,
the review page) shows only its last 4 digits. L12: a failed place_*_order is retried only when the tool schema takes
the ref_id idempotency key; otherwise the retry could place a second order, so it is refused."""
import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.brokers.base import OrderResult, OrderStateError
from agentdesk.brokers.robinhood import RobinhoodBroker
from agentdesk.exits import Contract

ACCT = "5QR12345"
NUM_ACCT = "123456789"
C = Contract("SPY", "2026-09-28", 660.0, "call", broker_id="opt-1")


def run(c):
    return asyncio.run(c)


class FakeRH:
    """Scripted Robinhood client: `script[tool]` is a list of replies (or exceptions); the last one repeats."""

    def __init__(self, account=ACCT, ref_id_in_schema=True, **script):
        self.account, self.script, self.calls = account, script, []
        props = {"account_number": {}, "legs": {}, "quantity": {}, "price": {}}
        if ref_id_in_schema:
            props["ref_id"] = {}
        self.tools = {"place_option_order": {"properties": props}, "place_equity_order": {"properties": props}}

    async def call(self, tool, args):
        self.calls.append(tool)
        seq = self.script.get(tool, [{}])
        r = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(r, Exception):
            raise r
        return r

    async def instrument_id(self, c):
        return f"id-{c.strike:g}{c.right[0]}"


def stored(res) -> str:
    return json.dumps({"review": res.review, "raw": res.raw, "message": res.message}, ensure_ascii=False)


def shadow(rh):
    b = RobinhoodBroker(rh, quotes=None, live=False)

    async def paper_fill(contract, side, qty, limit, now):
        return OrderResult("filled", qty, limit, "paper")
    b.paper.submit = paper_fill
    return b


# ----------------------------------------------------------------------------- L9
def test_shadow_option_review_is_stored_with_the_last_4_digits_only():
    rh = FakeRH(review_option_order=[{"account_number": ACCT, "buying_power_effect": "-110.00",
                                      "alerts": [f"Account {ACCT} has limited margin"]}])
    res = run(shadow(rh).submit(C, "buy", 1, 1.10, 0.0))
    s = stored(res)
    assert ACCT not in s and "••••2345" in s


def test_a_numeric_account_echo_is_redacted_too():
    rh = FakeRH(account=NUM_ACCT, review_option_order=[{"account_number": int(NUM_ACCT), "ok": True}])
    s = stored(run(shadow(rh).submit(C, "buy", 1, 1.10, 0.0)))
    assert NUM_ACCT not in s and "6789" in s


def test_a_failed_review_message_is_redacted():
    rh = FakeRH(review_option_order=[RuntimeError(f"review_option_order error: account {ACCT} not approved")])
    res = run(shadow(rh).submit(C, "buy", 1, 1.10, 0.0))
    assert res.status == "rejected" and ACCT not in stored(res)


def test_live_option_order_review_and_placement_replies_are_redacted():
    rh = FakeRH(review_option_order=[{"account_number": ACCT}], place_option_order=[{"id": "o1", "account": ACCT}],
                get_option_orders=[{"orders": [{"state": "filled", "processed_quantity": "1", "average_price": "1.10"}]}])
    b = RobinhoodBroker(rh, quotes=None, live=True, fill_timeout=0.5)
    res = run(b.submit(C, "buy", 1, 1.10, 0.0))
    assert res.status == "filled" and ACCT not in stored(res)


def test_equity_review_is_redacted():
    from agentdesk.brokers.robinhood_equity import RobinhoodEquityBroker

    class Paper:
        async def buy(self, sym, qty, limit, trigger, now):
            return OrderResult("filled", qty, limit, "paper")
    rh = FakeRH(review_equity_order=[{"account_number": ACCT, "warnings": [f"{ACCT}: pattern day trader"]}])
    b = RobinhoodEquityBroker(rh, Paper(), live=False, cfg={"live_enabled": False, "robinhood": {}})
    res = run(b.buy("NVDA", 5, 100.05, 100.0, now=0.0))
    assert ACCT not in stored(res) and "••••2345" in stored(res)


def test_combo_review_is_redacted():
    from agentdesk.books.fills import ComboExecutor
    from books_fakes import FILLS, contracts
    from test_books_fills import FLY, TIGHT, broker, quotes
    rh = FakeRH(review_option_order=[{"account_number": ACCT, "note": f"acct {ACCT}"}])
    res = run(ComboExecutor(broker(quotes(TIGHT)), FILLS, reviewer=rh).work(FLY, contracts(FLY), 1, True, True, True, 100.0))
    assert res.review and ACCT not in stored(res)


# ----------------------------------------------------------------------------- L12
def test_no_retry_of_a_failed_placement_when_the_schema_has_no_ref_id(caplog):
    rh = FakeRH(ref_id_in_schema=False, place_option_order=[RuntimeError("502 Bad Gateway"), {"id": "o2"}])
    b = RobinhoodBroker(rh, quotes=None, live=True, fill_timeout=0.02)
    with caplog.at_level(logging.ERROR, logger="agentdesk.robinhood"), pytest.raises(OrderStateError):
        run(b.submit(C, "buy", 1, 1.10, 0.0))
    assert rh.calls.count("place_option_order") == 1                 # a retry without ref_id could duplicate it
    assert "ref_id" in caplog.text


def test_a_failed_placement_is_retried_once_when_ref_id_is_in_the_schema():
    rh = FakeRH(place_option_order=[RuntimeError("502 Bad Gateway"), {"id": "o2"}],
                get_option_orders=[{"orders": [{"state": "filled", "processed_quantity": "1", "average_price": "1.10"}]}])
    b = RobinhoodBroker(rh, quotes=None, live=True, fill_timeout=0.5)
    assert run(b.submit(C, "buy", 1, 1.10, 0.0)).status == "filled"
    assert rh.calls.count("place_option_order") == 2


def test_no_equity_retry_without_ref_id_in_the_schema():
    from agentdesk.brokers.robinhood_equity import RobinhoodEquityBroker
    rh = FakeRH(ref_id_in_schema=False, place_equity_order=[RuntimeError("502"), {"id": "e2"}])
    b = RobinhoodEquityBroker(rh, None, live=True, cfg={"live_enabled": True, "robinhood": {"account_number": ACCT}})
    with pytest.raises(OrderStateError):
        run(b.buy("NVDA", 5, 100.05, 100.0, now=0.0))
    assert rh.calls.count("place_equity_order") == 1
