import math
import random
import sys
from datetime import date, time
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.bars import TickBarBuilder, TimeBarBuilder, Trade
from agentdesk.brokers.robinhood import SchemaError, fit_args, order_args
from agentdesk.clock import at_ct
from agentdesk.config import load_config
from agentdesk.exits import Contract, ExitPlan, Position
from agentdesk.indicators import MACD, RSI
from agentdesk.levels import Levels
from agentdesk.risk import RiskManager
from agentdesk.strategy import SignalEngine
from agentdesk.strikes import choose_strike, strike_for

CFG = load_config()
D = date(2026, 9, 28)


def series(n=400, seed=1):
    rng = random.Random(seed)
    px, out = 660.0, []
    for _ in range(n):
        px += rng.gauss(0, 0.3)
        out.append(px)
    return out


def test_macd_matches_pandas_after_seed():
    xs = series()
    m = MACD()
    vals = [m.update(x) for x in xs]
    s = pd.Series(xs)
    # pandas ewm(adjust=False) seeded at x0 differs early; after ~150 bars both converge
    fast = s.ewm(span=12, adjust=False).mean()
    slow = s.ewm(span=26, adjust=False).mean()
    line = fast - slow
    sig = line.ewm(span=9, adjust=False).mean()
    assert abs(vals[-1].macd - line.iloc[-1]) < 1e-3
    assert abs(vals[-1].signal - sig.iloc[-1]) < 1e-3


def test_rsi_wilder_bounds_and_preview_is_pure():
    xs = series(200, 3)
    r = RSI(14)
    for x in xs[:-1]:
        r.update(x)
    before = (r.avg_gain, r.avg_loss, r.value)
    p = r.preview(xs[-1])
    assert (r.avg_gain, r.avg_loss, r.value) == before
    assert p == r.update(xs[-1])
    assert 0 <= p <= 100


def test_rsi_all_up_is_100():
    r = RSI(14)
    for i in range(30):
        r.update(100 + i)
    assert r.value == 100.0


def test_time_and_tick_bars():
    tb, kb = TimeBarBuilder("1m"), TickBarBuilder(144)
    t0 = at_ct(D, time(9, 0))
    closed_t, closed_k = [], []
    for i in range(1000):
        tr = Trade(t0 + i * 0.5, 660 + (i % 7) * 0.01, 100)
        c = tb.on_trade(tr)
        k = kb.on_trade(tr)
        closed_t += [c] if c else []
        closed_k += [k] if k else []
    assert len(closed_k) == 1000 // 144
    assert all(b.n == 144 for b in closed_k)
    assert len(closed_t) == 8 and all(b.n == 120 for b in closed_t)
    assert closed_t[0].t == t0


def test_1m_to_5m_aggregation():
    one, five = TimeBarBuilder("1m"), TimeBarBuilder("5m")
    t0 = at_ct(D, time(9, 0))
    got = []
    for i in range(12 * 60):
        c = one.on_trade(Trade(t0 + i, 600 + i * 0.001))
        if c:
            f = five.on_bar(c)
            if f:
                got.append(f)
    assert len(got) == 2 and got[0].t == t0 and got[1].t == t0 + 300


@pytest.mark.parametrize("spot,off,side,k", [
    (572.40, 1, "call", 573), (572.40, 2, "call", 574), (572.60, 0, "call", 573), (572.40, -1, "call", 572),
    (572.00, 1, "call", 573), (572.40, 1, "put", 572), (572.40, 2, "put", 571), (572.40, -1, "put", 573),
])
def test_strike_offsets(spot, off, side, k):
    assert strike_for(spot, off, side) == k


def test_strike_schedule_and_levels():
    lv = Levels()
    lv.set_prior_day(580.0, 570.0, 575.0)
    early = at_ct(D, time(9, 0))
    c = choose_strike(572.40, early, "call", lv, None, CFG["strikes"])
    assert c.offset == 2          # nearest wall PDC/$575 clears the +2 strike (574) + buffer
    lv.pivots.append((574.05, "5m pivot high"))
    c = choose_strike(572.40, early, "call", lv, None, CFG["strikes"])
    assert c.offset == 1          # wall at 574.05 is inside 574 + buffer: stay at the base strike
    lv2 = Levels()
    c2 = choose_strike(571.10, early, "call", lv2, None, CFG["strikes"])
    assert c2.offset == 2          # next wall is the $575 round, clears 573 + buffer
    lv3 = Levels()
    lv3.pivots.append((571.5, "5m pivot high"))
    c3 = choose_strike(571.10, early, "call", lv3, None, CFG["strikes"])
    assert c3.offset == 0          # pivot 571.50 caps the 572 strike -> step in
    late = at_ct(D, time(14, 0))
    assert choose_strike(571.10, late, "call", Levels(), None, CFG["strikes"]).offset == -1


def mkpos(setup="SWING", qty=4, entry=1.00):
    p = Position(Contract("SPY", "2026-09-28", 573, "call"), setup, qty, entry, at_ct(D, time(9, 0)))
    return p, ExitPlan(CFG["exits"], p)


def test_hard_stop():
    p, plan = mkpos()
    t = p.opened_ts + 30
    assert plan.on_quote(0.85, 0.87, t) is None
    x = plan.on_quote(0.78, 0.80, t)
    assert x and x.qty == 4 and x.urgent and "stop" in x.reason


def test_scale_breakeven_trail_and_runner():
    p, plan = mkpos()
    t = p.opened_ts + 60
    x = plan.on_quote(1.24, 1.27, t)          # mark 1.255 >= +25%
    assert x.qty == 2 and "scale +25" in x.reason
    p.qty -= 2
    assert p.stop >= 1.00                      # breakeven
    x = plan.on_quote(1.49, 1.52, t)           # +50%
    assert x.qty == 1
    p.qty -= 1
    plan.on_quote(1.90, 1.92, t)               # new peak 1.91 -> trail 1.43
    assert abs(p.stop - round(1.91 * 0.75, 2)) < 0.011
    x = plan.on_quote(1.40, 1.42, t)
    assert x and x.qty == 1 and "trailing" in x.reason


def test_cross_back_before_and_after_scale():
    p, plan = mkpos()
    assert plan.on_cross_down("144t", False) is None      # SWING ignores the 144t
    x = plan.on_cross_down("1m", False)
    assert x.qty == 4
    p2, plan2 = mkpos()
    p2.scales_done, p2.qty = 1, 2
    assert plan2.on_cross_down("1m", ripping=True) is None      # ripping: runner holds
    assert plan2.on_cross_down("5m", ripping=True).qty == 2
    assert plan2.on_cross_down("1m", ripping=False).qty == 2


def test_scalp_uses_144t_and_time_stop():
    p, plan = mkpos("SCALP")
    assert plan.on_cross_down("1m", False) is None
    assert plan.on_cross_down("144t", False).qty == 4
    p2, plan2 = mkpos("SCALP")
    x = plan2.on_quote(1.00, 1.02, p2.opened_ts + 7 * 60)
    assert x and "time stop" in x.reason


def test_risk_limits_and_sizing():
    r = RiskManager(CFG)
    now = at_ct(D, time(9, 30))
    assert r.size(1.25) == (4, r.size(1.25)[1])
    assert r.size(0.60)[0] == 5                 # capped at max_contracts
    assert r.size(6.00)[0] == 0                 # too expensive for $500
    assert r.can_enter(now, 0)[0]
    assert not r.can_enter(at_ct(D, time(8, 31)), 0)[0]     # before entry window
    r.on_realized(-120); r.on_trade_closed(-120, now)
    r.on_realized(-90); r.on_trade_closed(-90, now)
    ok, why = r.can_enter(now + 60, 0)
    assert not ok and "cooldown" in why
    r.on_realized(-250)
    ok, why = r.can_enter(now + 3600, 0)
    assert not ok and "daily loss" in why and r.st.halted


def test_crew_can_only_reduce_size():
    r = RiskManager(CFG)
    r.set_size_mult(1.8)
    assert r.st.size_mult == 1.0
    r.set_size_mult(0.1)
    assert r.st.size_mult == CFG["crew"]["min_size_multiplier"]


def test_signal_engine_entry_needs_all_conditions():
    from agentdesk.bars import Bar
    se = SignalEngine(CFG["strategy"])
    assert se.evaluate(0, 600) is None
    t0 = at_ct(D, time(8, 30))
    # warm every TF on a gentle uptrend with pullbacks, then force a fresh 1m cross-up
    px = 600.0
    for i in range(700):
        px += 0.02 + (0.15 if i % 9 < 4 else -0.12)
        for tf, step in (("1m", 60), ("144t", 20)):
            se.on_bar_close(Bar(tf, t0 + i * step, px, px, px, px, 1, 144, t0 + (i + 1) * step))
        if i % 5 == 4:
            se.on_bar_close(Bar("5m", t0 + i * 60, px, px, px, px, 1, 1, t0 + (i + 1) * 60))
        if i % 15 == 14:
            se.on_bar_close(Bar("15m", t0 + i * 60, px, px, px, px, 1, 1, t0 + (i + 1) * 60))
    assert se.ready()
    ok, passed, failed = se.check(t0 + 700 * 60)
    assert isinstance(ok, bool) and len(passed) + len(failed) >= 7
    st = se.tf["1m"]
    if st.cross_up_ts is not None:
        now = st.cross_up_ts + 10
        sig = se.evaluate(now, px)
        if sig:
            assert sig.setup in ("SWING", "SCALP")
            se.consume(sig)
            assert se.evaluate(now, px) is None      # one cross -> one entry


def test_fit_args_drops_unknown_and_checks_required():
    sch = {"properties": {"account_number": {}, "legs": {}, "quantity": {}, "price": {}, "type": {}, "time_in_force": {}, "ref_id": {}},
           "required": ["account_number", "legs", "quantity"]}
    a = order_args("A1", [{"option_id": "X", "side": "buy", "position_effect": "open"}], 3, 1.2, review=True)
    out = fit_args("place_option_order", sch, a)
    assert "chain_symbol" not in out and "direction" not in out
    assert out["quantity"] == "3" and out["price"] == "1.20" and out["type"] == "limit"
    with pytest.raises(SchemaError):
        fit_args("place_option_order", sch, {"legs": []})


def test_order_args_spread_sets_direction():
    legs = [{"option_id": "L", "side": "buy", "position_effect": "open"}, {"option_id": "S", "side": "sell", "position_effect": "open"}]
    assert order_args("A1", legs, 2, 0.85, review=False)["direction"] == "debit"


def test_proposal_bounds_and_scopes(tmp_path):
    import copy
    from agentdesk.proposals import ProposalBook, apply_overrides, write_override
    cfg = copy.deepcopy(dict(CFG))
    book = ProposalBook(cfg, tmp_path / "p.json")
    ok = book.submit("quant", {"scope": "day", "title": "t1", "params": {"exits.stop_loss_pct": 0.15}}, 0)
    assert ok["status"] == "applied"
    bad = book.submit("quant", {"scope": "day", "title": "t2", "params": {"exits.stop_loss_pct": 0.5}}, 0)
    assert bad["status"].startswith("rejected")
    loosen = book.submit("quant", {"scope": "day", "title": "t3", "params": {"risk.max_trades_per_day": 30}}, 0)
    assert loosen["status"].startswith("rejected")          # can only lower
    unknown = book.submit("quant", {"scope": "standing", "title": "t4", "params": {"sizing.max_contracts": 20}}, 0)
    assert unknown["status"].startswith("rejected")         # sizing is not tweakable by the crew
    trade = book.submit("vol", {"scope": "trade", "title": "t5", "params": {"strategy.rsi.upper": 65}}, 0)
    assert trade["status"].startswith("rejected")           # per-trade only exits / strike cap
    st = book.submit("quant", {"scope": "standing", "title": "t6", "params": {"exits.swing.scale_outs.0.at": 0.30}}, 0)
    assert st["status"] == "pending"
    path = write_override(st["params"], tmp_path / "ov.yaml")
    fresh = copy.deepcopy(dict(CFG))
    apply_overrides(fresh, path)
    assert fresh["exits"]["swing"]["scale_outs"][0]["at"] == 0.30
    assert fresh["exits"]["swing"]["scale_outs"][1]["at"] == CFG["exits"]["swing"]["scale_outs"][1]["at"]


def test_size_up_scales_budget_and_cap_but_cuts_win():
    r = RiskManager(CFG)
    assert r.size(0.60, 1.25)[0] == 6          # cap 5 -> 6
    assert r.size(1.25, 1.25)[0] == 5          # $625 / $125
    r.set_size_mult(0.75)
    assert r.size(0.60, 1.25)[0] == 5          # a cut is active: no size-up, $375 budget
    assert r.size(1.25, 1.25)[0] == 3
