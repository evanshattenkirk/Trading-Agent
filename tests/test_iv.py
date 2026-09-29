"""Book E / IV recorder chain helpers: expiry pickers, ATM strike, pacing."""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.iv import IVQuote, Pacer, atm_strike, d30_expiry, front_expiry, post_expiry, pre_expiry

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
