"""Book E / IV recorder chain helpers: expiry pickers, ATM strike, pacing."""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.iv import IVQuote, Pacer, RobinhoodChains, atm_strike, d30_expiry, front_expiry, post_expiry, pre_expiry

TODAY = date(2026, 10, 5)                       # Monday
EXPS = [date(2026, 10, 5), date(2026, 10, 7), date(2026, 10, 9), date(2026, 10, 16), date(2026, 11, 6)]


def test_front_is_the_next_expiry_after_today():
    assert front_expiry(EXPS, TODAY) == date(2026, 10, 7)
    assert front_expiry([TODAY], TODAY) is None


def test_d30_is_the_expiry_closest_to_30_days():
    assert d30_expiry(EXPS, TODAY) == date(2026, 11, 6)          # 32 days out beats 11


def test_pre_and_post_depend_on_report_timing():
    wed = date(2026, 10, 7)
    assert pre_expiry(EXPS, wed, "am") == date(2026, 10, 5)      # am: strictly before D
    assert pre_expiry(EXPS, wed, "") == date(2026, 10, 5)        # unknown: treated like am
    assert pre_expiry(EXPS, wed, "pm") == wed                    # pm: D's own expiry closes before the report
    assert post_expiry(EXPS, wed, "am") == wed                   # am: D's expiry trades through the reaction
    assert post_expiry(EXPS, wed, "pm") == date(2026, 10, 9)
    assert post_expiry(EXPS, wed, "") == date(2026, 10, 9)       # unknown: must carry either timing
    assert post_expiry(EXPS, date(2026, 12, 1), "pm") is None


def test_atm_strike_must_be_listed_in_every_expiry():
    assert atm_strike([{95.0, 100.0, 105.0}, {100.0, 105.0}], 101.0) == 100.0
    assert atm_strike([{95.0, 105.0}, {100.0, 105.0}], 101.0) == 105.0
    assert atm_strike([{100.0, 105.0}], 102.5) == 100.0          # tie goes to the lower strike
    assert atm_strike([], 100.0) is None
    assert atm_strike([{100.0}], None) is None


def test_ivquote_is_a_quote_with_iv():
    q = IVQuote(1.0, 1.2, 5.0, 0.31)
    assert q.mark == 1.1 and q.iv == 0.31
    assert IVQuote(1.0, 1.2, 5.0).iv is None


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(round(s, 6))
        self.t += s


def test_pacer_spaces_calls_at_the_rate():
    clk = Clock()
    p = Pacer(0.5, clock=clk, sleep=clk.sleep)

    async def go():
        for _ in range(4):
            await p.wait()
    asyncio.run(go())
    assert clk.slept == [2.0, 2.0, 2.0]


def test_pacer_does_not_sleep_when_calls_are_already_slow():
    clk = Clock()
    p = Pacer(1.0, clock=clk, sleep=clk.sleep)

    async def go():
        await p.wait()
        clk.t += 5
        await p.wait()
    asyncio.run(go())
    assert clk.slept == []



NOW = 1_790_000_000.0          # any fixed epoch; the cache compares against it


class FakeRH:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    async def call(self, tool, args):
        self.calls.append((tool, dict(args)))
        r = self.responses[tool]
        return r(args) if callable(r) else r


def instruments(exp, strikes, page=None, nxt=None):
    rows = [{"id": f"{r[0]}{k:g}-{exp}", "strike_price": f"{k:.4f}", "type": r, "expiration_date": exp}
            for k in strikes for r in ("call", "put")]
    return {"results": rows, "next": nxt}


def test_expirations_are_read_once_per_day():
    rh = FakeRH({"get_option_chains": {"results": [{"symbol": "AMD", "expiration_dates": ["2026-10-09", "2026-10-16"]}]}})
    ch = RobinhoodChains(rh, clock=lambda: NOW)

    async def go():
        a = await ch.expirations("AMD")
        b = await ch.expirations("AMD")
        return a, b
    a, b = asyncio.run(go())
    assert a == b == [date(2026, 10, 9), date(2026, 10, 16)]
    assert len(rh.calls) == 1


def test_expirations_without_the_field_raise_with_the_keys_seen():
    ch = RobinhoodChains(FakeRH({"get_option_chains": {"results": [{"symbol": "AMD"}]}}), clock=lambda: NOW)
    try:
        asyncio.run(ch.expirations("AMD"))
    except RuntimeError as ex:
        assert "expiration_dates" in str(ex)
    else:
        raise AssertionError("expected RuntimeError")


def test_strikes_page_through_and_need_both_rights(tmp_path):
    pages = {None: instruments("2026-10-09", [95, 100], nxt="c2"), "c2": instruments("2026-10-09", [105])}
    pages["c2"]["results"] = [r for r in pages["c2"]["results"] if r["type"] == "call"]     # 105 has no put
    rh = FakeRH({"get_option_instruments": lambda a: pages[a.get("cursor")]})
    ch = RobinhoodChains(rh, cache_dir=tmp_path, clock=lambda: NOW)
    ks = asyncio.run(ch.strikes("AMD", date(2026, 10, 9)))
    assert ks == {95.0, 100.0}
    assert [a.get("cursor") for _, a in rh.calls] == [None, "c2"]
    c = ch.contract("AMD", date(2026, 10, 9), 100, "put")
    assert c.broker_id == "p100-2026-10-09" and c.expiry == "2026-10-09" and c.symbol == "AMD"


def test_strike_lists_are_cached_on_disk_until_stale(tmp_path):
    rh = FakeRH({"get_option_instruments": instruments("2026-10-09", [100])})
    asyncio.run(RobinhoodChains(rh, cache_dir=tmp_path, clock=lambda: NOW).strikes("AMD", date(2026, 10, 9)))
    again = RobinhoodChains(rh, cache_dir=tmp_path, clock=lambda: NOW + 86400)
    assert asyncio.run(again.strikes("AMD", date(2026, 10, 9))) == {100.0}
    assert len(rh.calls) == 1                                    # second instance read the file
    stale = RobinhoodChains(rh, cache_dir=tmp_path, clock=lambda: NOW + 4 * 86400)
    asyncio.run(stale.strikes("AMD", date(2026, 10, 9)))
    assert len(rh.calls) == 2                                    # older than LIST_MAX_AGE_DAYS: listed again


def test_quotes_are_one_batched_call_with_iv():
    rh = FakeRH({"get_option_instruments": instruments("2026-10-09", [100]),
                 "get_option_quotes": {"results": [
                     {"instrument_id": "c100-2026-10-09", "bid_price": "2.40", "ask_price": "2.50", "implied_volatility": "0.62"},
                     {"instrument_id": "p100-2026-10-09", "bid_price": "2.20", "ask_price": "2.30"}]}})
    ch = RobinhoodChains(rh, clock=lambda: NOW)

    async def go():
        await ch.strikes("AMD", date(2026, 10, 9))
        cs = [ch.contract("AMD", date(2026, 10, 9), 100, r) for r in ("call", "put")]
        return cs, await ch.quotes(cs), await ch.quote(cs[0])
    cs, qs, one = asyncio.run(go())
    assert [q.bid for q in qs] == [2.40, 2.20] and qs[0].iv == 0.62 and qs[1].iv is None
    assert qs[0].ts == NOW and one is qs[0]
    assert [t for t, _ in rh.calls].count("get_option_quotes") == 1


def test_calls_wait_for_the_pacer():
    class CountPacer:
        n = 0

        async def wait(self):
            CountPacer.n += 1
    rh = FakeRH({"get_equity_quotes": {"results": [{"symbol": "AMD", "last_trade_price": "100.2"},
                                                   {"symbol": "XOM", "bid_price": "115", "ask_price": "115.2"}]}})
    ch = RobinhoodChains(rh, pacer=CountPacer(), clock=lambda: NOW)
    assert asyncio.run(ch.spots(["AMD", "XOM"])) == {"AMD": 100.2, "XOM": 115.1}
    assert CountPacer.n == 1


# Robinhood's live schemas and response shapes, read 2026-09-29 (after the first IV pass failed every name with
# "one of ids or underlying_symbol is required"). The real client runs fit_args against these schemas, so any
# argument name the server doesn't declare is dropped before the call goes out.
LIVE_SCHEMAS = {
    "get_option_chains": {"properties": {"ids": {}, "underlying_symbol": {}}},
    "get_option_instruments": {"properties": {k: {} for k in ("chain_id", "chain_symbol", "cursor", "expiration_dates",
                                                              "ids", "state", "strike_price", "tradability", "type")}},
    "get_option_quotes": {"properties": {"instrument_ids": {}}, "required": ["instrument_ids"]},
}


class SchemaRH(FakeRH):
    """FakeRH that filters arguments the way the real client does and rejects calls the server would reject."""

    async def call(self, tool, args):
        from agentdesk.brokers.robinhood import fit_args
        args = fit_args(tool, LIVE_SCHEMAS[tool], args)
        if tool == "get_option_chains" and not (args.get("ids") or args.get("underlying_symbol")):
            raise RuntimeError("one of ids or underlying_symbol is required")
        return await super().call(tool, args)


def test_expirations_ask_for_underlying_symbol_and_take_the_matching_chain():
    live = {"chains": [                                  # an adjusted chain (after a corporate action) can come first
        {"id": "c-adj", "symbol": "AMD1", "expiration_dates": ["2026-10-02"]},
        {"id": "c-std", "symbol": "AMD", "expiration_dates": ["2026-10-09", "2026-10-16"], "settle_on_open": False},
    ]}
    rh = SchemaRH({"get_option_chains": live})
    exps = asyncio.run(RobinhoodChains(rh, clock=lambda: NOW).expirations("AMD"))
    assert rh.calls == [("get_option_chains", {"underlying_symbol": "AMD"})]
    assert exps == [date(2026, 10, 9), date(2026, 10, 16)]


def test_expirations_fall_back_to_the_only_chain_when_symbols_differ():
    rh = SchemaRH({"get_option_chains": {"chains": [{"symbol": "BRK.B", "expiration_dates": ["2026-10-09"]}]}})
    assert asyncio.run(RobinhoodChains(rh, clock=lambda: NOW).expirations("BRK-B")) == [date(2026, 10, 9)]


def test_strike_listing_and_quotes_pass_the_live_schemas(tmp_path):
    inst = {"instruments": instruments("2026-10-09", [100])["results"]}
    quote = {"results": [{"quote": {"instrument_id": "c100-2026-10-09", "bid_price": "1.00", "ask_price": "1.20",
                                    "implied_volatility": "0.41"}}]}
    rh = SchemaRH({"get_option_instruments": inst, "get_option_quotes": quote})
    ch = RobinhoodChains(rh, cache_dir=tmp_path, clock=lambda: NOW)

    async def go():
        ks = await ch.strikes("AMD", date(2026, 10, 9))
        return ks, await ch.quotes([ch.contract("AMD", date(2026, 10, 9), 100.0, "call")])
    ks, (q,) = asyncio.run(go())
    assert ks == {100.0}
    assert rh.calls[0] == ("get_option_instruments", {"chain_symbol": "AMD", "expiration_dates": "2026-10-09",
                                                      "state": "active"})
    assert q.iv == 0.41 and q.mark == 1.1
