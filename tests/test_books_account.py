from books_fakes import contracts
from agentdesk.books.account import AccountRisk
from agentdesk.books.base import Leg, Strategy
from agentdesk.books.book import Book
from agentdesk.books.combo import ComboPosition

PUTS = [Leg("put", 764, "sell"), Leg("put", 762, "buy")]


def test_fixed_lots_capped_by_per_position_max_loss():
    a = AccountRisk({"per_position_max_loss": 300})
    assert a.size(170, lots=1)[0] == 1
    assert a.size(170, lots=2)[0] == 1
    n, why = a.size(310, lots=1)
    assert n == 0 and "310" in why


def test_budget_sizing_uses_lower_of_budget_and_cap():
    a = AccountRisk({"per_position_max_loss": 300})
    assert a.size(150, budget=400)[0] == 2
    assert a.size(160, budget=400)[0] == 1
    assert AccountRisk({"per_position_max_loss": 1000}).size(160, budget=400)[0] == 2


def test_crew_cut_never_skips_a_one_lot_book():
    a = AccountRisk({"per_position_max_loss": 1000})
    assert a.size(150, lots=1, mult=0.5)[0] == 1
    assert a.size(150, lots=4, mult=0.5)[0] == 2
    assert a.size(150, lots=4, mult=1.25)[0] == 4          # no size-up for B/C/D


def test_open_risk_cap_and_buying_power():
    a = AccountRisk({"paper_balance": 10000, "open_risk_cap": 1500})
    assert a.can_open(300, open_risk=1000)[0]
    ok, why = a.can_open(600, open_risk=1000)
    assert not ok and "cap" in why
    small = AccountRisk({"paper_balance": 500, "open_risk_cap": 1500})
    ok, why = small.can_open(200, open_risk=400)
    assert not ok and "buying power" in why
    assert small.can_open(200, open_risk=400, a_day_pnl=200)[0]
    small.on_closed(-150)
    assert small.equity() == 350


def test_halt_blocks_new_risk():
    a = AccountRisk()
    a.halt("KILL switch", flatten=True)
    ok, why = a.can_open(1, 0)
    assert not ok and "KILL" in why and a.flatten


def test_book_gates_and_day_state():
    b = Book("C_orb_bull_put", {"max_trades_day": 2}, Strategy({}))
    assert b.letter == "C" and b.can_enter()[0]
    p = ComboPosition("C", "ORB BULL PUT", PUTS, contracts(PUTS), 1, 0.50, True, 2.0, 100.0)
    b.on_open(p)
    assert b.can_enter() == (False, "position already open")
    b.on_close(p, -40.0)
    assert b.trades == 1 and b.losses == 1 and b.day_pnl == -40.0 and b.can_enter()[0]
    b.on_open(ComboPosition("C", "ORB BULL PUT", PUTS, contracts(PUTS), 1, 0.50, True, 2.0, 200.0))
    b.on_close(b.open[0], 25.0)
    assert b.can_enter() == (False, "max 2 trades today")
    b.blocked = "partial fill"
    b.reset_day()
    assert b.can_enter()[0] and b.blocked is None and b.trades == 0
    b.halt("error: boom")
    assert not b.can_enter()[0] and b.to_dict()["halted"]
