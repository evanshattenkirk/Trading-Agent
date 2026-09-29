"""Robinhood call budget (2026-09-29): the paper engine made ~200 calls/min against a ~240/min account ceiling, got
RATE_LIMITED 773 times and crowded out the quote recorder. The engine now paces itself to a shared budget, backs off
when throttled, batches every B/C/D/G position into one quote call, and never halts a book over a throttle."""
import asyncio
from types import SimpleNamespace

import pytest

from books_fakes import FakeEngine, FakeQuotes, ct_ts
from agentdesk.books.base import Leg, OrderIntent, Strategy
from agentdesk.books.book import Book
from agentdesk.books.host import BookHost
from agentdesk.books.legs import fetch_quotes
from agentdesk.brokers.robinhood import CallBudget, RateLimited, RobinhoodMCP, is_rate_limited
from agentdesk.config import load_config
from agentdesk.feeds.base import Quote


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t=1000.0):
        self.t, self.slept = t, []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


def budget(per_min=120, per_s=3, clock=None):
    clock = clock or Clock()
    return CallBudget(per_min=per_min, per_s=per_s, clock=clock, sleep=clock.sleep), clock


# ------------------------------------------------------------------ CallBudget
def test_budget_spaces_bursts_to_the_per_second_cap():
    b, clk = budget(per_s=3)

    async def go():
        for _ in range(4):
            await b.acquire()
    run(go())
    assert clk.slept and clk.t == pytest.approx(1001.0, abs=0.01)      # the 4th call waits for the 1 s window


def test_budget_holds_to_the_per_minute_cap():
    b, clk = budget(per_min=5, per_s=100)

    async def go():
        for _ in range(6):
            await b.acquire()
    run(go())
    assert clk.t == pytest.approx(1060.0, abs=0.01)                    # the 6th waits for the oldest to age out


def test_throttle_pauses_every_call_and_backs_off_until_a_success():
    b, clk = budget(per_s=100)
    b.throttled()
    run(b.acquire())
    assert clk.t == pytest.approx(1000 + CallBudget.FIRST_PAUSE_S)
    assert b.pause_s == CallBudget.FIRST_PAUSE_S * 2                  # the next one doubles
    for _ in range(10):
        b.throttled()
    assert b.pause_s == CallBudget.MAX_PAUSE_S
    assert CallBudget.MAX_PAUSE_S < load_config()["risk"]["watchdog"]["quote_stale_sec"]   # never trips the watchdog
    b.ok()
    assert b.pause_s == CallBudget.FIRST_PAUSE_S


def test_urgent_calls_skip_the_wait_but_still_count():
    b, clk = budget(per_s=1)
    b.throttled()

    async def go():
        await b.acquire(urgent=True)
        await b.acquire(urgent=True)
    run(go())
    assert clk.t == 1000.0 and b.last_minute() == 2


def test_rate_limited_text_is_recognised():
    assert is_rate_limited("get_option_quotes error: error: RATE_LIMITED: too many requests, please try again shortly")
    assert is_rate_limited("HTTP 429")
    assert not is_rate_limited("get_option_quotes error: 500 internal")


# ------------------------------------------------------------------ RobinhoodMCP.call
class FakeSession:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    async def call_tool(self, tool, args):
        self.calls.append(tool)
        a = self.answers.pop(0) if self.answers else "ok"
        err = a != "ok"
        return SimpleNamespace(isError=err, content=[SimpleNamespace(text=a if err else "{}")],
                               structuredContent=None if err else {"data": {"ok": True}})


def mcp(answers, **bud):
    cfg = load_config()
    rh = RobinhoodMCP(cfg)
    rh.tools = {"get_option_quotes": {}, "place_option_order": {}}
    rh.session = FakeSession(answers)
    rh.budget, clk = budget(**bud)
    return rh, clk


def test_a_throttled_call_raises_rate_limited_and_pauses_the_next_one():
    rh, clk = mcp(["error: RATE_LIMITED: too many requests, please try again shortly"], per_s=100)
    with pytest.raises(RateLimited):
        run(rh.call("get_option_quotes", {"instrument_ids": ["x"]}))
    assert rh.session.calls == ["get_option_quotes"]                   # no instant retry into the limit
    assert run(rh.call("get_option_quotes", {"instrument_ids": ["x"]})) == {"ok": True}
    assert clk.t == pytest.approx(1000 + CallBudget.FIRST_PAUSE_S)    # waited out the pause before asking again
    assert rh.budget.pause_s == CallBudget.FIRST_PAUSE_S


def test_engine_config_budgets_below_half_the_ceiling():
    cfg = load_config()
    b = CallBudget.from_cfg(cfg["robinhood"])
    assert b is not None and b.per_min <= 120                          # ~240/min ceiling, the recorder keeps the rest
    assert cfg["l2"]["poll_ms"] >= 3000                                 # observe-only Level 2: 20 calls/min, not 60


def test_recorder_keeps_its_own_pacing():
    from agentdesk import recorder
    cfg = load_config()
    rc = recorder.recorder_cfg(cfg, recorder.settings(cfg))
    assert CallBudget.from_cfg(rc["robinhood"]) is None


# ------------------------------------------------------------------ fetch_quotes + BookHost batching
class RHQuotes(FakeQuotes):
    """FakeQuotes behind a Robinhood-like batched get_option_quotes, counting calls."""
    max_age = 0.5

    def __init__(self, now):
        super().__init__(now)
        self.cache, self.batches, self.fail = {}, [], None
        host = self

        class RH:
            async def instrument_id(self, c):
                return f"{c.right}{c.strike:g}{c.expiry}"

            async def call(self, tool, args):
                if host.fail:
                    raise host.fail
                host.batches.append(list(args["instrument_ids"]))
                res = []
                for oid in args["instrument_ids"]:
                    for (right, k, *_), (b, a, _) in host.book.items():
                        if oid.startswith(f"{right}{k:g}"):
                            res.append({"instrument_id": oid, "quote": {"bid_price": b, "ask_price": a}})
                            break
                return {"results": res}
        self.rh = RH()

    async def quote(self, c):
        return (await fetch_quotes(self, [c, c]))[0]


FLY = [Leg("call", 765, "sell"), Leg("put", 765, "sell"), Leg("call", 770, "buy"), Leg("put", 760, "buy")]
CAL = [Leg("call", 765, "sell", dte=0), Leg("call", 765, "buy", dte=1)]


def test_fetch_quotes_reuses_fresh_quotes_instead_of_asking_again():
    from books_fakes import contracts
    q = RHQuotes(ct_ts(9, 0))
    for l in FLY:
        q.set(l.right, l.strike, 1.0, 1.02)
    cs = contracts(FLY)
    run(fetch_quotes(q, cs))
    again = run(fetch_quotes(q, cs))
    assert len(q.batches) == 1 and all(x is not None and x.ask == 1.02 for x in again)


class Opener(Strategy):
    name = "TEST"

    def __init__(self, legs):
        super().__init__({})
        self.intent = OrderIntent(list(legs), False, 0.0, "test", lots=1)

    def on_clock(self, now, ctx):
        it, self.intent = self.intent, None
        return it

    def on_quote(self, pos, cq, now, ctx):
        return None


def host_with_two_open_books():
    q = RHQuotes(ct_ts(9, 0))
    for l in FLY:
        q.set(l.right, l.strike, 1.0, 1.02)
    eng = FakeEngine(q)
    books = [Book("B_test", {"max_trades_day": 1}, Opener(FLY)), Book("G_test", {"max_trades_day": 1}, Opener(CAL))]
    host = BookHost(eng, eng.cfg, books=books)
    q.now = ct_ts(9, 0)
    run(host.on_second(ct_ts(9, 0)))
    assert len(host.positions()) == 2
    return q, host


def test_every_open_position_is_quoted_in_one_call_per_tick():
    q, host = host_with_two_open_books()
    q.cache.clear()
    q.batches.clear()
    q.now = ct_ts(9, 0, 5)
    run(host.on_second(ct_ts(9, 0, 5)))
    assert len(q.batches) == 1                                         # 4 fly legs + 2 calendar legs, one call
    assert len(q.batches[0]) == 5                                      # the shared 765 call is asked for once


def test_a_throttle_never_counts_toward_a_book_halt():
    q, host = host_with_two_open_books()
    q.fail = RateLimited("get_option_quotes error: RATE_LIMITED: too many requests")
    for i in range(1, 8):
        q.cache.clear()
        q.now = ct_ts(9, 1, i)
        run(host.on_second(ct_ts(9, 1, i)))
    assert not any(b.halted for b in host.books) and all(b.errors == 0 for b in host.books)
    logs = [d for d in host.e.bus.of("log") if "rate limit" in d["msg"]]
    assert len(logs) == 1 and logs[0]["level"] == "warn"                 # one line per episode, not one per call


@pytest.mark.parametrize("which", ["E", "F"])
def test_books_e_and_f_skip_a_throttled_round_without_counting_it(which):
    if which == "E":
        from test_e_host import make
        h = make()[0]
    else:
        from test_f_host import make
        h = make()[2]

    async def throttled():
        raise RateLimited("get_option_quotes error: RATE_LIMITED: too many requests")
    for i in range(6):
        run(h._safe(throttled(), 1000.0 + i))
    assert h.book.errors == 0 and not h.book.halted
    assert len([d for d in h.e.bus.of("log") if "rate limit" in d["msg"]]) == 1
