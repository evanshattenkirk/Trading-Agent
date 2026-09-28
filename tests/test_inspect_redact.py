"""rh-inspect prints Robinhood responses; the account number must show only its last 4 digits (CLAUDE.md)."""
import asyncio
import json

from agentdesk.brokers.robinhood import redact_account


def test_redact_account_keeps_only_the_last_4_digits():
    rev = {"account_number": "123456789", "collateral": {"account_number": "123456789"}}
    out = redact_account(json.dumps(rev), "123456789")
    assert "123456789" not in out and out.count("••••6789") == 2


def test_redact_account_is_a_no_op_without_an_account():
    assert redact_account("abc", "") == "abc"


class FakeRH:
    account = "123456789"

    def __init__(self):
        self.calls = []

    async def call(self, tool, args):
        self.calls.append(tool)
        if tool == "get_equity_tradability":
            return {"results": [{"symbol": "SPY", "account_number": "123456789", "tradable": True}]}
        if tool == "review_equity_order":
            return {"account_number": "123456789", "symbol": "SPY"}
        return {}

    tools = {"review_equity_order": {"properties": {"account_number": {}}}}


def test_inspect_equity_never_prints_the_full_account_number(capsys):
    from agentdesk.brokers.robinhood_equity import inspect_equity
    asyncio.run(inspect_equity(FakeRH(), "123456789"))
    out = capsys.readouterr().out
    assert "123456789" not in out and "••••6789" in out
