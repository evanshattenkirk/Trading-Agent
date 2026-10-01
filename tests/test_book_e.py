"""Book E rules (HANDOFF 7E; spec section 3): windows, expiries, legs, sizing, filters, limits and exits."""
import sqlite3
import sys
from datetime import date, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.books import earnings_iv as R
from agentdesk.books.base import Leg
from agentdesk.books.combo import ComboPosition
from agentdesk.books.e_journal import EJournal, pos_from_dict, pos_to_dict
from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.exits import Contract

C = load_config()["books"]["E_earnings_iv"]
MON, THU = date(2026, 10, 5), date(2026, 10, 8)
FRI1, FRI2 = date(2026, 10, 9), date(2026, 10, 16)
EXPS = [date(2026, 10, 7), FRI1, FRI2, date(2026, 10, 23)]


def at(d, h, m):
    return at_ct(d, time(h, m))


def test_config_carries_the_approved_values():
    assert (C["max_debit"], C["max_debit_e2"], C["max_open"], C["paper_only"]) == (500, 250, 3, True)
    assert (C["entry_ct"], C["exit_ct"], C["expiry_day_exit_ct"], C["half_day_ct"]) == ("14:45", "14:45", "14:15", "11:20")
    assert C["take_profit"] == {"straddle_t3": 0.20, "calendar_t10": 0.15} and C["stop_pct"] == 0.30
    assert (C["vix_max"], C["iv_pct_max"], C["iv_min_cycles"], C["max_leg_spread_pct"]) == (30, 0.80, 4, 0.05)
    assert R.sector_of("NVDA", C["sectors"]) == "tech" and R.sector_of("XOM", C["sectors"]) == "energy"
    universe = load_config()["crew"]["earnings"]["universe"]
    assert all(R.sector_of(s, C["sectors"]) for s in universe)


def test_flags_map_to_structures():
    assert (R.structure_for("E1"), R.structure_for("E2"), R.structure_for("exit")) == (R.E1, R.E2, None)


def test_times_shift_on_half_days():
    assert R.entry_time(C, False) == time(14, 45) and R.close_time(C, False) == time(14, 45)
    assert R.entry_time(C, True) == time(11, 20) and R.close_time(C, True) == time(11, 20)


def test_exit_day_is_t0_for_pm_and_t1_otherwise():
    assert R.exit_day(THU, "pm", set()) == THU
    assert R.exit_day(THU, "am", set()) == date(2026, 10, 7)
    assert R.exit_day(date(2026, 10, 12), "", set()) == FRI1          # Monday report: Friday before
    assert R.exit_day(date(2026, 11, 27), "am", {date(2026, 11, 26)}) == date(2026, 11, 25)


def test_announced_boundaries():
    assert not R.announced(THU, "pm", at(THU, 14, 59)) and R.announced(THU, "pm", at(THU, 15, 0))
    assert R.announced(THU, "am", at(THU, 0, 1)) and not R.announced(THU, "am", at(date(2026, 10, 7), 23, 0))
    assert R.announced(THU, "", at(THU, 8, 0))


def test_e1_needs_the_first_post_report_expiry_4_to_10_days_out():
    got, why = R.pick_expiries(R.E1, EXPS, MON, THU, "pm", C)
    assert got == {"long": FRI1, "short": None} and why == "ok"                 # 4 DTE
    got, why = R.pick_expiries(R.E1, EXPS, MON, date(2026, 10, 6), "pm", C)     # first after is 10-07: 2 DTE
    assert got is None and "2 DTE" in why


def test_e2_needs_a_weekly_between_today_and_the_report():
    got, _ = R.pick_expiries(R.E2, EXPS, MON, FRI2, "am", C)
    assert got == {"long": FRI2, "short": FRI1}
    got, why = R.pick_expiries(R.E2, [FRI1, FRI2], FRI1, FRI2, "am", C)         # the only pre expiry is today
    assert got is None and "weekly" in why


def test_legs():
    e1 = R.legs_for(R.E1, 100.0, MON, {"long": FRI1, "short": None})
    assert [(l.right, l.strike, l.side, l.dte) for l in e1] == [("call", 100.0, "buy", 4), ("put", 100.0, "buy", 4)]
    e2 = R.legs_for(R.E2, 115.0, MON, {"long": FRI2, "short": FRI1})
    assert [(l.right, l.side, l.dte) for l in e2] == [("call", "sell", 4), ("call", "buy", 11)]


def test_sizing_by_max_debit():
    assert R.max_debit(R.E1, C) == 500 and R.max_debit(R.E2, C) == 250
    assert R.lots_for(4.72, 500)[0] == 1 and R.lots_for(1.20, 250)[0] == 2
    n, why = R.lots_for(6.02, 500)
    assert n == 0 and "$602 > $500" in why
    assert R.lots_for(0.60, 250, mult=0.5)[0] == 2          # 4 lots, crew cut to half
    assert R.lots_for(1.20, 250, mult=0.5)[0] == 1          # never below 1


def test_limits():
    s = C["sectors"]
    assert R.limit_problem("AMD", [], s, 3) is None
    assert "already open" in R.limit_problem("AMD", ["AMD"], s, 3)
    assert "tech sector" in R.limit_problem("NVDA", ["AMD"], s, 3)
    assert "max 3" in R.limit_problem("XOM", ["AMD", "JPM", "KO"], s, 3)
    assert "no sector" in R.limit_problem("ZZZ", [], s, 3)


def test_vix_filter():
    assert R.vix_problem(30.0, C) is None
    assert "no prior VIX close" in R.vix_problem(None, C)          # like book D: no VIX, no entry (2026-10-01 sweep)
    assert "VIX 31.2 > 30" in R.vix_problem(31.2, C)


def test_iv_filter_waits_for_four_cycles():
    ok, note, pct = R.iv_check(0.61, [0.40, 0.45, 0.50], C)
    assert ok and note == "IV filter: 3/4 cycles" and pct is None
    ok, note, pct = R.iv_check(0.61, [0.40, 0.45, 0.50, 0.55], C)
    assert not ok and pct == 1.0 and "percentile" in note
    ok, _, pct = R.iv_check(0.52, [0.40, 0.45, 0.50, 0.55, 0.60], C)
    assert ok and pct == 0.6
    ok, note, _ = R.iv_check(None, [0.4] * 5, C)
    assert ok and note == "IV filter: no IV today"


def test_take_profit_and_stop_per_structure():
    assert R.tp_stop(R.E1, 4.72, 5.66, C) is None
    assert R.tp_stop(R.E1, 4.72, 5.67, C).reason == "take profit +20%"
    assert R.tp_stop(R.E2, 1.55, 1.79, C).reason == "take profit +15%"
    it = R.tp_stop(R.E2, 1.55, 1.08, C)
    assert it.reason == "stop -30%" and it.urgent
    assert R.tp_stop(R.E2, 1.55, 1.09, C) is None


def meta(ev, timing, short=""):
    return {"earnings_date": str(ev), "timing": timing, "short_expiry": str(short) if short else ""}


def test_exit_on_the_exit_day_at_the_close_time():
    m = meta(THU, "pm")
    assert R.exit_reason(m, at(THU, 14, 44), C, False, set()) == (None, False)
    why, err = R.exit_reason(m, at(THU, 14, 45), C, False, set())
    assert "T-0" in why and not err
    why, _ = R.exit_reason(meta(THU, "am"), at(date(2026, 10, 7), 14, 45), C, False, set())
    assert "T-1" in why
    assert R.exit_reason(m, at(THU, 11, 20), C, True, set())[0]                  # half-day close 11:20


def test_e2_leaves_on_the_short_legs_expiry_day_first():
    m = meta(FRI2, "am", FRI1)
    assert R.exit_reason(m, at(FRI1, 14, 14), C, False, set()) == (None, False)
    why, err = R.exit_reason(m, at(FRI1, 14, 15), C, False, set())
    assert "short leg expires today" in why and not err
    why, err = R.exit_reason(m, at(date(2026, 10, 12), 8, 30), C, False, set())
    assert "expired" in why and err


def test_held_through_the_announcement_is_an_error():
    why, err = R.exit_reason(meta(THU, "pm"), at(FRI1, 8, 30), C, False, set())
    assert "held through" in why and err


def e2_pos():
    legs = [Leg("call", 115.0, "sell", dte=4), Leg("call", 115.0, "buy", dte=11)]
    cs = [Contract("XOM", "2026-10-09", 115.0, "call", "a"), Contract("XOM", "2026-10-16", 115.0, "call", "b")]
    p = ComboPosition("E", "E2 CALENDAR", legs, cs, 2, 1.55, False, 0.0, 100.0, strike_reason="XOM 115 ATM",
                      entry_reasons=["2 x $155"], meta={"symbol": "XOM", "structure": R.E2, "earnings_date": "2026-10-16",
                                                        "timing": "am", "short_expiry": "2026-10-09"}, id="E-abc")
    p.fees, p.mark, p.target, p.stop = 0.16, 1.60, 1.78, 1.09
    p.fills.append({"ts": 100.0, "side": "open", "qty": 2, "px": 1.55})
    return p


def test_positions_round_trip_through_json():
    p = e2_pos()
    q = pos_from_dict(pos_to_dict(p))
    assert (q.id, q.book, q.setup, q.qty, q.qty_initial, q.entry, q.credit) == ("E-abc", "E", "E2 CALENDAR", 2, 2, 1.55, False)
    assert q.legs == p.legs and [c.broker_id for c in q.contracts] == ["a", "b"] and q.contracts[1].expiry == "2026-10-16"
    assert (q.fees, q.mark, q.target, q.stop, q.meta, q.fills) == (0.16, 1.60, 1.78, 1.09, p.meta, p.fills)
    assert q.max_loss == 310.0


def test_ejournal_keeps_open_rows_and_decisions():
    ej = EJournal(sqlite3.connect(":memory:"))
    p = e2_pos()
    ej.save(p, 100.0)
    got, bad = ej.open_positions()
    assert [x.id for x in got] == ["E-abc"] and bad == []
    p.status = "closed"
    ej.save(p, 200.0)
    assert ej.open_positions() == ([], [])
    ej.decision(day="2026-10-05", ts=1.0, symbol="XOM", structure=R.E2, T=9, earnings_date="2026-10-16", timing="am",
                outcome="skipped", reason="quote", debit=None, lots=None, iv=None, iv_pct=None, vix=18.0)
    assert not ej.traded("XOM", "2026-10-16", R.E2)
    ej.decision(day="2026-10-06", ts=2.0, symbol="XOM", structure=R.E2, T=8, earnings_date="2026-10-16", timing="am",
                outcome="opened", reason="filled", debit=1.55, lots=1, iv=0.3, iv_pct=None, vix=18.0)
    assert ej.traded("XOM", "2026-10-16", R.E2) and not ej.traded("XOM", "2026-10-16", R.E1)
    assert [d["outcome"] for d in ej.decisions()] == ["skipped", "opened"]
    assert [d["outcome"] for d in ej.decisions("2026-10-06")] == ["opened"]


def test_unreadable_rows_are_reported_not_restored():
    db = sqlite3.connect(":memory:")
    ej = EJournal(db)
    db.execute("INSERT INTO e_positions VALUES ('E-bad', 'open', 'AMD', 'straddle_t3', 1, 1, '{not json')")
    assert ej.open_positions() == ([], ["E-bad"])
