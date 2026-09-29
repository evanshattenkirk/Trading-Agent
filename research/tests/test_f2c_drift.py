"""Tests for research/f2c_drift.py (F2-C's premise: the stock's same-day drift after the breakout)."""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import f2c_drift as D  # noqa: E402
from agentdesk.books import f_stocks_in_play as F  # noqa: E402

CFG = D.PRIMARY
D0 = date(2024, 1, 2)


def bar(t, o, h, l, c, v=1000):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def flat(px, start, until=960):
    return [bar(t, px, px + 0.05, px - 0.05, px) for t in range(start, until)]


def pick(sym="NVDA", rvol=3.0, green=True, rank=1):
    o, c = (99.5, 99.9) if green else (99.9, 99.5)
    return F.ScanRow(sym, rvol, o, c, 100.0, 99.0, 1, 2.0, 5e8, rank=rank, picked=True)


def test_hold_has_no_stop_and_exits_at_1555():
    bars = [bar(575, 99.9, 100.3, 99.9, 100.2)] + [bar(576, 100.2, 100.2, 98.5, 98.6)] + flat(101.0, 577)
    [t] = D.simulate_hold_day([pick()], {"NVDA": bars}, CFG, slip_bp=0)
    assert t["why"] == "exit 15:55" and t["exit"] == pytest.approx(101.0)
    assert t["qty"] == 9 and t["mae_bp"] < -100 and t["mfe_bp"] > 90


def test_or_low_stop_exits_at_the_opening_range_low():
    bars = [bar(575, 99.9, 100.3, 99.9, 100.2)] + [bar(576, 100.2, 100.2, 98.5, 98.6)] + flat(101.0, 577)
    [t] = D.simulate_hold_day([pick()], {"NVDA": bars}, CFG, slip_bp=0, or_low_stop=True)
    assert t["why"] == "OR-low stop" and t["exit"] == pytest.approx(99.0)


def test_f2_picks_use_f2_names_and_the_top_3():
    rows = [pick("NVDA", 3.0), pick("AMD", 5.0), pick("MU", 4.0), pick("AAPL", 2.5), pick("XYZ", 9.0),
            pick("TSLA", 6.0, green=False)]
    got = D.f2_picks(rows, {"NVDA", "AMD", "MU", "AAPL", "TSLA"}, CFG)
    assert [r.symbol for r in got] == ["AMD", "MU", "NVDA"]


def test_summary_verdict_and_pass_bar():
    days = [D0, D0 + timedelta(days=1)]
    tr = [{"day": D0, "pnl": 10.0, "bp": 100.0, "why": "exit 15:55", "mfe_bp": 150, "mae_bp": -20, "mfe_atr": 0.8, "mae_atr": -0.1},
          {"day": days[1], "pnl": -4.0, "bp": -40.0, "why": "exit 15:55", "mfe_bp": 10, "mae_bp": -60, "mfe_atr": 0.1, "mae_atr": -0.3}]
    s = D.summarize(tr, days)
    assert s["trades"] == 2 and s["mean_bp"] == pytest.approx(30.0) and s["pf"] == pytest.approx(2.5)
    assert s["mfe"]["reach_100bp"] == pytest.approx(0.5) and s["mfe"]["reach_0.5atr"] == pytest.approx(0.5)
    assert D.verdict({"trades": 2, "mean_bp": -1.0}, False) == "NO DRIFT"
    assert D.verdict({"trades": 2, "mean_bp": 5.0}, False) == "POSITIVE, NOT SIGNIFICANT"
    assert D.verdict({"trades": 2, "mean_bp": 5.0}, True) == "DRIFT"
    assert D.passes({"pf": 1.2, "t": 2.5}, {"mean_bp": 1}, {"mean_bp": 2}) is True


def test_f2_universe_comes_from_config():
    names = D.f2_universe()
    assert {"NVDA", "AMD", "AAPL"} <= names and len(names) >= 30
