"""M19: the backtest's model quotes price 0DTE options on the trading-day clock of HANDOFF section 7 (the B/D research
model), not the simulator's calendar clock, which priced them at about half of real."""
import asyncio
import math
import sys
from datetime import date, time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.backtest import ModelQuotes
from agentdesk.clock import at_ct
from agentdesk.pricing import YEAR, bs_price, quote_from_model, trading_clock_t_sec

DAY = date(2026, 10, 6)


class Feed:
    def __init__(self, hh, mm, px=765.0):
        self.day, self.t, self.px = DAY, at_ct(DAY, time(hh, mm)), px


def mid(q):
    return (q.bid + q.ask) / 2


def bd_call(S, K, sd):                       # research/strategies_bcd.py's call, the B/D model
    N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))      # noqa: E731
    d1 = (math.log(S / K) + 0.5 * sd * sd) / sd
    return S * N(d1) - K * N(d1 - sd)


@pytest.mark.parametrize("hh,mm,rth_left_min", [(8, 30, 390), (11, 0, 240), (14, 0, 60), (15, 0, 0)])
def test_trading_clock_gives_the_handoff_sd(hh, mm, rth_left_min):
    iv = 0.16
    t = trading_clock_t_sec((15 * 60 + 15 - (hh * 60 + mm)) * 60)
    want = 0.80 * iv / math.sqrt(252) * math.sqrt(rth_left_min / 390 + 15 / 390)
    assert iv * math.sqrt(t / YEAR) == pytest.approx(want)


def test_trading_clock_caps_at_one_session_plus_the_tail():
    assert trading_clock_t_sec(24 * 3600) == trading_clock_t_sec(405 * 60)
    assert trading_clock_t_sec(-5) == 0.0


def test_model_quotes_match_the_bd_model_at_the_open():
    q = asyncio.run(ModelQuotes(Feed(8, 30), 0.16).quote(SimpleNamespace(strike=765, right="call")))
    sd = 0.80 * 0.16 / math.sqrt(252) * math.sqrt(1 + 15 / 390)
    assert mid(q) == pytest.approx(bd_call(765.0, 765, sd), abs=0.06)      # $2.51 in the B/D model (rates, rounding)
    assert 2.4 < mid(q) < 2.7


def test_calendar_clock_is_kept_for_committed_research():
    feed = Feed(8, 30)
    c = SimpleNamespace(strike=766, right="call")
    q = asyncio.run(ModelQuotes(feed, 0.128, clock="calendar").quote(c))
    assert (q.bid, q.ask) == quote_from_model(765.0, 766, 6.75 * 3600, 0.128, "call")
    assert mid(q) < 0.7                                                       # the "cheap-option model": ~$0.65
    trading = asyncio.run(ModelQuotes(feed, 0.16).quote(c))
    assert mid(trading) > 2.5 * mid(q)


def test_the_simulator_keeps_its_calendar_pricing():
    assert bs_price(765.0, 765, 6.75 * 3600, 0.128, "call") == pytest.approx(1.096, abs=0.001)


def test_unknown_clock_is_refused():
    with pytest.raises(ValueError):
        ModelQuotes(Feed(8, 30), 0.16, clock="wall")
