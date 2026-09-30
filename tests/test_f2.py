"""Book F2 (research/strategy_f2_prereg.md): the pure rules, the paper fill model, and F2Host end to end with a fake
chain source, a fake F1 scan and fake equity data."""
from __future__ import annotations

import asyncio
import copy
from datetime import date, time

import pytest

from books_fakes import FakeEngine, FakeQuotes
from e_fakes import FakeChains
from f_fakes import FakeData
from agentdesk.books import f2_spreads as S
from agentdesk.books import f_stocks_in_play as F
from agentdesk.books.base import Leg
from agentdesk.books.combo import ComboQuote, paper_fair
from agentdesk.books.f2_host import F2Host, build_f2, realized_vol
from agentdesk.books.group import HostGroup
from agentdesk.config import load_config
from agentdesk.iv import IVQuote

BASE = load_config()
C2 = BASE["books"]["F2_debit_spreads"]
THU, FRI, MON, TUE = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6)
EXP = date(2026, 10, 9)                      # 8 days after Thursday: inside 5-12
NEAR = date(2026, 10, 2)                     # 1 day: too close
FAR = date(2026, 10, 16)                     # 15 days: too far


def et(d, h, m, s=0):
    return F.at_et(d, time(h, m, s))


# ------------------------------------------------------------------ pure rules
def test_expiry_is_the_nearest_5_to_12_days_out():
    assert S.pick_expiry([NEAR, EXP, FAR], THU, C2) == EXP
    assert S.pick_expiry([NEAR, FAR], THU, C2) is None


def test_short_strike_is_one_straddle_out_and_at_least_two_steps():
    ks = {95, 97.5, 100, 102.5, 105, 107.5, 110}
    assert S.spread_strikes(ks, 100, "call", 5.7, 2) == 105
    assert S.spread_strikes(ks, 100, "call", 1.0, 2) == 105          # too narrow: pushed out to 2 steps
    assert S.spread_strikes(ks, 100, "put", 4.8, 2) == 95
    assert S.spread_strikes({100, 102.5}, 100, "call", 5, 2) is None
    assert S.legs_for(S.CALL, 100, 105, 8)[0] == Leg("call", 100, "buy", 1, 8)       # bought leg first: a debit


def test_structure_check_sizing_and_exits():
    assert S.structure_problem(2.0, 5.0, C2) is None
    assert "60%" in S.structure_problem(3.2, 5.0, C2)
    assert S.lots_for(1.5, {**C2, "max_debit": 200})[0] == 1 and S.lots_for(2.5, {**C2, "max_debit": 200})[0] == 0
    assert S.tp_stop(2.0, 4.0, 10.0, C2).reason.startswith("take profit")
    assert S.tp_stop(2.0, 4.1, 5.0, C2).reason.startswith("take profit")       # 82% of the width
    stop = S.tp_stop(2.0, 1.0, 10.0, C2)
    assert stop.reason.startswith("stop") and stop.urgent
    assert S.tp_stop(2.0, 2.5, 10.0, C2) is None


def test_configured_limits_evan_set_2026_09_29():
    assert C2["max_debit"] == 750 and BASE["books"]["account"]["open_risk_cap"] == 2500
    n, why = S.lots_for(2.40, C2)
    assert n == 3 and "max $750" in why
    assert S.lots_for(7.60, C2)[0] == 0                                    # one lot above $750: skipped


def test_exit_days_count_trading_days_and_skip_holidays():
    assert S.exit_day(THU, S.CALL, set(), C2) == MON          # Thu, Fri, Mon: day 3
    assert S.exit_day(THU, S.PUT, set(), C2) == FRI
    assert S.exit_day(THU, S.PUT, {FRI}, C2) == MON
    assert S.exit_time_et(S.CALL, C2, False) == time(15, 30) and S.exit_time_et(S.PUT, C2, True) == time(12, 45)


def _row(sym, rvol, green=True):
    o, c = (100.0, 100.4) if green else (100.4, 100.0)
    return F.ScanRow(sym, rvol, o, c, 100.5, 99.9, 1000, 2.0, 5e8, prev_close=99.0)


def test_call_signals_are_f2_names_green_and_rvol5_ranked():
    rows = [_row("NVDA", 3.0), _row("AMD", 5.0), _row("MU", 2.5, green=False), _row("XYZ", 9.0), _row("AAPL", 1.5)]
    got = S.call_signals(rows, {"NVDA", "AMD", "MU", "AAPL"}, {**C2, "max_armed_c": 2})
    assert [r.symbol for r in got] == ["AMD", "NVDA"]


def _daily(px=100.0, vol=2_000_000, n=25):
    return [{"d": date(2026, 8, 1), "o": px, "h": px + 1, "l": px - 1, "c": px, "v": vol} for _ in range(n)]


def test_put_signals_need_the_move_and_the_volume():
    up_big = S.day_stats(_daily(), [{"t": 570, "c": 101, "v": 2e6}, {"t": 939, "c": 103.5, "v": 1.8e6}])
    up_quiet = S.day_stats(_daily(), [{"t": 939, "c": 104.0, "v": 1e6}])
    small = S.day_stats(_daily(), [{"t": 939, "c": 101.0, "v": 5e6}])
    assert up_big["chg_pct"] == pytest.approx(3.5) and up_big["vol_ratio"] == pytest.approx(1.9)
    got = S.put_signals({"A": up_big, "B": up_quiet, "C": small, "D": None}, C2)
    assert [s for s, _ in got] == ["A"]


def test_earnings_inside_the_hold_blocks_the_entry():
    cal = [{"symbol": "NVDA", "date": FRI, "timing": "pm"}, {"symbol": "AMD", "date": TUE, "timing": "am"}]
    assert "inside the hold" in S.earnings_conflict("NVDA", cal, THU, MON)
    assert S.earnings_conflict("AMD", cal, THU, MON) is None


def test_realized_vol_annualizes_log_returns():
    flat = _daily()
    assert realized_vol(flat) == pytest.approx(0.0)
    alt = [{"c": 100.0 * (1.01 if i % 2 else 1.0)} for i in range(21)]
    assert realized_vol(alt) == pytest.approx(0.01 * 252 ** 0.5 * 1.0, rel=0.05)


def test_mid_frac_fill_sits_between_mid_and_natural():
    legs = [Leg("call", 100, "buy"), Leg("call", 105, "sell")]
    cq = ComboQuote(legs, [IVQuote(3.00, 3.10, 0), IVQuote(1.00, 1.05, 0)])
    assert cq.mid(False) == pytest.approx(2.025) and cq.natural(False, True) == pytest.approx(2.10)
    assert paper_fair(cq, False, True, "mid_frac", 0.0, 0.35) == pytest.approx(2.05)
    assert paper_fair(cq, False, False, "mid_frac", 0.0, 0.35) == pytest.approx(2.00)     # selling it back


# ------------------------------------------------------------------ host
class FakeF1:
    """What F2 reads from F1: the day's scan and F1's fresh stock quotes."""

    def __init__(self, rows=()):
        self.scan = F.ScanResult(list(rows), [], []) if rows else None
        self.q: dict = {}
        self.data = None

    def _fresh(self, sym, now):
        q = self.q.get(sym)
        return q if q is not None and now - q.ts <= 5 else None


def chain(ch, sym="NVDA", exp=EXP, call=(3.00, 3.10), put=(2.60, 2.70), short_call=(1.00, 1.05), short_put=(0.90, 0.95)):
    ch.chain(sym, exp, [95, 97.5, 100, 102.5, 105, 107.5, 110])
    ch.set(sym, exp, 100, "call", *call, 0.50)
    ch.set(sym, exp, 100, "put", *put, 0.52)
    ch.set(sym, exp, 105, "call", *short_call, 0.55)
    ch.set(sym, exp, 95, "put", *short_put, 0.58)


def make(rows=(), cal=(), data=None, **over):
    cfg = copy.deepcopy(BASE)
    cfg["books"]["F2_debit_spreads"].update({"max_debit": 1000, **over})
    eng = FakeEngine(FakeQuotes(), cfg)
    ch = FakeChains()
    chain(ch)
    f1 = FakeF1(rows)
    rows_cal = list(cal)

    async def calendar(today):
        return list(rows_cal)
    h = F2Host(eng, cfg, ch, data=data, fhost=f1, calendar_fn=calendar)
    asyncio.run(h.start())
    return h, eng, ch, f1


def tick(h, ch, t):
    ch.now = t
    h.e.feed.t = t
    asyncio.run(h.on_second(t))


def stock(f1, sym, px, t):
    f1.q[sym] = F.Q(px - 0.01, px + 0.01, px, t)


def opened_call(**kw):
    h, eng, ch, f1 = make(rows=[_row("NVDA", 3.0)], **kw)
    tick(h, ch, et(THU, 9, 35, 10))
    assert set(h.armed) == {"NVDA"}
    stock(f1, "NVDA", 100.4, et(THU, 9, 36))
    tick(h, ch, et(THU, 9, 36))
    assert not h.book.open                               # not above the OR high yet
    stock(f1, "NVDA", 100.6, et(THU, 9, 36, 3))
    tick(h, ch, et(THU, 9, 36, 3))
    return h, eng, ch, f1


def test_call_spread_opens_on_the_or_high_break():
    h, eng, ch, f1 = opened_call()
    [p] = h.book.open
    assert p.book == "F2" and p.setup == "NVDA CALL SPREAD" and not p.credit
    assert [(l.right, l.strike, l.side) for l in p.legs] == [("call", 100, "buy"), ("call", 105, "sell")]
    assert p.entry == pytest.approx(2.05) and p.width == 5 and p.qty == 4          # floor($1,000 / $205)
    assert p.target == pytest.approx(4.10) and p.stop == pytest.approx(1.02, abs=0.01)
    assert p.meta["exit_day"] == str(MON) and p.meta["or_low"] == pytest.approx(99.9)
    assert p.meta["skew"] == pytest.approx(0.55 / 0.50) and p.watchdog_exempt and p.overnight
    assert h.j.decisions(str(THU))[-1]["outcome"] == "opened"
    assert eng.bus.of("book_position")[-1]["event"] == "open"
    assert h.j.open_positions()[0][0].id == p.id
    assert not h.armed


def test_call_setup_waits_for_the_scan_and_disarms_at_1030():
    h, eng, ch, f1 = make()
    tick(h, ch, et(THU, 9, 36))
    assert not h.c_armed
    f1.scan = F.ScanResult([_row("NVDA", 3.0)], [], [])
    tick(h, ch, et(THU, 9, 37))
    assert set(h.armed) == {"NVDA"}
    tick(h, ch, et(THU, 10, 30))
    assert not h.armed and not h.book.open


def test_first_day_thesis_stop_below_the_or_low():
    h, eng, ch, f1 = opened_call()
    ch.px["NVDA"] = 99.85
    tick(h, ch, et(THU, 9, 40))
    assert not h.book.open
    c = h.book.closed[0]
    assert c.exit_reason.startswith("thesis stop") and c.status == "closed"
    assert eng.journal.trades()[0]["book"] == "F2"


def test_take_profit_on_the_spread_mid():
    h, eng, ch, f1 = opened_call()
    ch.px["NVDA"] = 104.0
    ch.set("NVDA", EXP, 100, "call", 5.40, 5.50, 0.5)
    ch.set("NVDA", EXP, 105, "call", 1.20, 1.25, 0.5)
    tick(h, ch, et(THU, 11, 0))
    assert h.book.closed and h.book.closed[0].exit_reason.startswith("take profit")
    assert h.book.day_pnl > 0


def test_call_spread_time_exit_on_day_three():
    h, eng, ch, f1 = opened_call()
    ch.px["NVDA"] = 100.8
    tick(h, ch, et(FRI, 10, 0))
    assert h.book.open
    tick(h, ch, et(MON, 15, 29, 55))
    assert h.book.open
    tick(h, ch, et(MON, 15, 30))
    assert h.book.closed[0].exit_reason == f"time exit {MON} 15:30 ET"


def put_data():
    data = FakeData(THU)
    data.add("AMD", o=97.5, c=98.0, or_high=98.1, or_low=97.4, vol5=3_000_000, hist_vol=1000, atr=2.0, px=97.0)
    data.add_bar("AMD", 939, 100.1, 100.3, 100.0, 100.2, 900_000)          # +3.3% on 1.95x volume by 15:40
    data.add("MSFT", o=100.0, c=100.4, or_high=100.5, or_low=99.9, vol5=500_000, hist_vol=1000, atr=2.0)
    data.add_bar("MSFT", 939, 104.0, 104.1, 103.9, 104.0, 100_000)          # +4% but quiet volume
    return data


def test_put_spread_fades_a_high_volume_up_day_and_exits_next_day():
    h, eng, ch, f1 = make(data=put_data())
    chain(ch, "AMD")
    tick(h, ch, et(THU, 15, 40, 5))
    [p] = h.book.open
    assert p.setup == "AMD PUT SPREAD"
    assert [(l.right, l.strike, l.side) for l in p.legs] == [("put", 100, "buy"), ("put", 95, "sell")]
    assert p.meta["exit_day"] == str(FRI) and "or_low" not in p.meta
    assert p.entry == pytest.approx(1.75) and p.qty == 5
    assert "MSFT" not in {d["symbol"] for d in h.j.decisions(str(THU))}     # +4% on quiet volume: no signal
    tick(h, ch, et(FRI, 15, 40))
    assert h.book.closed[0].exit_reason == f"time exit {FRI} 15:40 ET"


def test_report_inside_the_hold_skips_and_a_missing_calendar_blocks():
    h, eng, ch, f1 = opened_call(cal=[{"symbol": "NVDA", "date": FRI, "timing": "pm"}])
    assert not h.book.open and "inside the hold" in h.j.decisions(str(THU))[-1]["reason"]
    h2, eng2, ch2, f12 = make(rows=[_row("NVDA", 3.0)])

    async def down(today):
        return None
    h2.calendar_fn = down
    tick(h2, ch2, et(THU, 9, 35, 10))
    stock(f12, "NVDA", 100.6, et(THU, 9, 36))
    tick(h2, ch2, et(THU, 9, 36))
    assert not h2.book.open and "calendar unavailable" in h2.j.decisions(str(THU))[-1]["reason"]


def test_expensive_or_wide_structures_are_skipped():
    h, eng, ch, f1 = make(rows=[_row("NVDA", 3.0)])
    ch.set("NVDA", EXP, 105, "call", 0.10, 0.15, 0.5)                   # debit 3.25 of a 5 width: 65% > 60%
    ch.set("NVDA", EXP, 100, "call", 3.30, 3.40, 0.5)
    tick(h, ch, et(THU, 9, 35, 10))
    stock(f1, "NVDA", 100.6, et(THU, 9, 36))
    tick(h, ch, et(THU, 9, 36))
    assert not h.book.open and "of the 5 width" in h.j.decisions(str(THU))[-1]["reason"]
    h2, eng2, ch2, f12 = make(rows=[_row("NVDA", 3.0)])
    ch2.set("NVDA", EXP, 105, "call", 0.60, 1.40, 0.5)                 # 80% of mid wide
    tick(h2, ch2, et(THU, 9, 35, 10))
    stock(f12, "NVDA", 100.6, et(THU, 9, 36))
    tick(h2, ch2, et(THU, 9, 36))
    assert not h2.book.open and "short leg" in h2.j.decisions(str(THU))[-1]["reason"]


def test_size_cap_skips_a_spread_above_max_debit():
    h, eng, ch, f1 = make(rows=[_row("NVDA", 3.0)], max_debit=200)
    tick(h, ch, et(THU, 9, 35, 10))
    stock(f1, "NVDA", 100.6, et(THU, 9, 36))
    tick(h, ch, et(THU, 9, 36))
    assert not h.book.open and "0 x 2.05" in h.j.decisions(str(THU))[-1]["reason"]


def test_restart_restores_and_shutdown_keeps_kill_sells():
    h, eng, ch, f1 = opened_call()
    pid = h.book.open[0].id
    h2 = F2Host(eng, h.cfg, ch, fhost=f1, j=h.j, calendar_fn=h.calendar_fn)
    asyncio.run(h2.start())
    assert [p.id for p in h2.book.open] == [pid] and h2.book.open[0].watchdog_exempt
    asyncio.run(h2.flatten("shutdown", et(THU, 15, 0)))
    assert h2.book.open
    ch.now = et(THU, 14, 0)
    asyncio.run(h2.kill(et(THU, 14, 0)))
    assert not h2.book.open and h2.book.closed[0].exit_reason.startswith("KILL")


def test_one_spread_per_name_and_a_daily_loss_blocks_new_entries():
    h, eng, ch, f1 = opened_call(daily_loss=1)
    asyncio.run(h._consider("NVDA", S.CALL, et(THU, 9, 50), 100.6, {"or_low": 99.9, "or_high": 100.5}))
    assert "already holding NVDA" in h.j.decisions(str(THU))[-1]["reason"]
    ch.px["NVDA"] = 99.0
    tick(h, ch, et(THU, 9, 51))                                            # thesis stop: a loss
    assert h.book.day_pnl < 0 and "daily loss" in (h.book.blocked or "")
    asyncio.run(h._consider("AMD", S.CALL, et(THU, 9, 52), 100.6, {"or_low": 99.9, "or_high": 100.5}))
    assert "daily loss" in h.j.decisions(str(THU))[-1]["reason"]


def test_group_shares_risk_and_shows_f2_on_the_strip():
    h, eng, ch, f1 = opened_call()
    g = HostGroup(None, None, None, h)
    snap = g.snapshot()
    assert [b["book"] for b in snap["books"]] == ["F2"] and snap["f2"]["universe"]
    assert g.open_risk() == pytest.approx(h.book.open[0].max_loss)


def test_opens_and_closes_refresh_the_book_strip():
    h, eng, ch, f1 = make(rows=[_row("NVDA", 3.0)])
    HostGroup(None, None, None, h)
    tick(h, ch, et(THU, 9, 35, 10))
    stock(f1, "NVDA", 100.6, et(THU, 9, 36))
    tick(h, ch, et(THU, 9, 36))
    [strip] = eng.bus.of("books")
    assert strip["books"][0]["book"] == "F2" and strip["books"][0]["trades"] == 1 and "open" not in strip["books"][0]
    ch.set("NVDA", EXP, 100, "call", 6.40, 6.50, 0.5)
    ch.set("NVDA", EXP, 105, "call", 2.20, 2.30, 0.55)
    tick(h, ch, et(THU, 11, 0))                                             # take profit
    assert len(eng.bus.of("books")) == 2 and eng.bus.of("books")[-1]["books"][0]["day_pnl"] > 0


def test_build_is_paper_only_and_idle_in_sim():
    cfg = copy.deepcopy(BASE)
    eng = FakeEngine(FakeQuotes(), cfg)
    assert build_f2(eng, cfg, provider="sim") is None
    cfg["books"]["F2_debit_spreads"]["paper_only"] = False
    with pytest.raises(SystemExit):
        build_f2(eng, cfg, provider="alpaca", chains=FakeChains())


def test_f_report_has_an_f2_section_split_by_setup_and_tags():
    from agentdesk.books.f_report import build_f2_report, format_f2_report
    h, eng, ch, f1 = opened_call()
    ch.set("NVDA", EXP, 100, "call", 5.40, 5.50, 0.5)
    ch.set("NVDA", EXP, 105, "call", 1.20, 1.25, 0.5)
    ch.px["NVDA"] = 104.0
    tick(h, ch, et(THU, 11, 0))
    r = build_f2_report(h.j)
    assert r["all"]["trades"] == 1 and r["all"]["pnl"] > 0
    assert set(r["by_setup"]) == {"C calls"} and set(r["by_skew"]) == {"short leg IV >= long"}
    text = format_f2_report(r)
    assert "Book F2 (debit spreads)" in text and "By setup:" in text
