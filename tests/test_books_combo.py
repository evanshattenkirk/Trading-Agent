import sys
from datetime import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.books.base import ExitIntent, Leg, credit_exit
from agentdesk.books.combo import ComboPosition, ComboQuote, leg_problem, paper_fair
from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.exits import Contract
from agentdesk.feeds.base import Quote

FLY = [Leg("call", 765, "sell"), Leg("put", 765, "sell"), Leg("call", 770, "buy"), Leg("put", 760, "buy")]


def q(b, a, ts=100.0):
    return Quote(b, a, ts)


QS = [q(2.00, 2.02), q(1.90, 1.92), q(0.40, 0.41), q(0.35, 0.36)]              # mid 3.16, half-spreads 0.03
WIDE = [q(1.95, 2.07), q(1.85, 1.97), q(0.35, 0.45), q(0.30, 0.40)]            # mid 3.17, half-spreads 0.22


def fly_pos(qty=2, entry=3.20):
    cs = [Contract("SPY", "2026-09-28", float(l.strike), l.right) for l in FLY]
    return ComboPosition("B", "IRON FLY", FLY, cs, qty, entry, True, 5.0, 100.0)


def test_credit_mid_and_natural():
    cq = ComboQuote(FLY, QS)
    assert cq.mid(True) == pytest.approx(3.16)
    assert cq.natural(True, opening=True) == pytest.approx(3.13)     # sell at bids, buy wings at asks
    assert cq.natural(True, opening=False) == pytest.approx(3.19)    # buy back at asks, sell wings at bids


def test_paper_fair_mid_offset_never_worse_than_natural():
    assert paper_fair(ComboQuote(FLY, QS), True, True, "mid_offset", 0.01) == pytest.approx(3.13)
    wide = ComboQuote(FLY, WIDE)
    assert paper_fair(wide, True, True, "mid_offset", 0.01) == pytest.approx(3.13)    # 3.17 - 4 legs x 1c
    assert paper_fair(wide, True, True, "natural", 0.01) == pytest.approx(2.95)
    assert paper_fair(wide, True, False, "mid_offset", 0.01) == pytest.approx(3.21)   # paying to close: mid + 4c


def test_leg_problems_fail_closed():
    assert leg_problem(None, 100.0, 5, 0.25, 0.02, True) == "no quote"
    assert "stale" in leg_problem(q(1.00, 1.02, ts=90.0), 100.0, 5, 0.25, 0.02, True)
    assert "crossed" in leg_problem(q(1.10, 1.00), 100.0, 5, 0.25, 0.02, True)
    assert "zero" in leg_problem(q(0.0, 0.0), 100.0, 5, 0.25, 0.02, True)
    assert "spread" in leg_problem(q(0.80, 1.20), 100.0, 5, 0.25, 0.02, True)
    assert leg_problem(q(1.00, 1.02), 100.0, 5, 0.25, 0.02, True) is None


def test_cheap_wing_is_not_rejected_as_wide():                        # review focus 1
    assert leg_problem(q(0.02, 0.03), 100.0, 5, 0.25, 0.02, True) is None      # 1 tick = 40% of mid
    assert leg_problem(q(0.00, 0.01), 100.0, 5, 0.25, 0.02, False) is None     # an open position still marks
    assert leg_problem(q(0.00, 0.01), 100.0, 5, 0.25, 0.02, True) == "zero bid"
    assert leg_problem(q(0.80, 1.20), 100.0, 5, 0.25, 0.02, False) is None     # wide never blocks a stop check


def test_combo_position_risk_and_pnl():
    p = fly_pos()
    assert p.max_loss == pytest.approx(360.0)            # (5 - 3.20) x 100 x 2
    assert p.risk_basis == pytest.approx(360.0)
    p.mark = 1.60
    assert p.unrealized == pytest.approx(320.0)
    assert p.pnl_per_share(1.60) == pytest.approx(1.60)
    assert p.id.startswith("B")
    d = p.to_dict()
    assert d["book"] == "B" and len(d["legs"]) == 4 and d["max_loss"] == pytest.approx(360.0)
    assert d["contract"] == p.label and "765" in p.label


def test_credit_exit_take_profit_stop_and_close():
    c = {"take_profit_pct": 0.5, "stop_debit_x_credit": 2.0, "close_ct": "14:30"}
    p = fly_pos(entry=3.00)
    early = at_ct(__import__("datetime").date(2026, 9, 28), time(10, 0))
    assert credit_exit(p, 2.00, early, c) is None
    assert credit_exit(p, 1.50, early, c).reason.startswith("take profit")
    st = credit_exit(p, 6.00, early, c)
    assert st.urgent and "stop" in st.reason
    late = at_ct(__import__("datetime").date(2026, 9, 28), time(14, 30))
    assert "close 14:30" in credit_exit(p, 2.00, late, c).reason


def test_config_has_one_explicit_stop_field():
    books = load_config()["books"]
    for k in ("B_iron_fly", "D_iron_condor"):
        assert books[k]["stop_debit_x_credit"] == 2.0 and "stop_x_credit" not in books[k]
    assert books["account"] == {"paper_balance": 10000, "open_risk_cap": 2500, "per_position_max_loss": 300,
                                "fee_per_leg": 0.04}
    assert books["fills"]["model"] == "mid_offset" and books["fills"]["cents_per_leg"] == 1
