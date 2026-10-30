"""Tests for research/round5_w.py (rules in research/round5_prereg.md section 2). Synthetic data only."""
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import bd_real_quotes as bd  # noqa: E402
import round5_w as w5  # noqa: E402

E945, X1540 = 9 * 60 + 45, 15 * 60 + 40
STRIKES = [float(k) for k in range(90, 111)]
HS = 0.02                                             # half spread


def prices(spot, tv, width):
    """Parity-consistent mids: intrinsic + a time value that decays away from the money."""
    out = {}
    for k in STRIKES:
        t = tv * math.exp(-abs(k - spot) / width)
        out[("C", k)] = max(spot - k, 0.0) + t
        out[("P", k)] = max(k - spot, 0.0) + t
    return out


def chain(before, after, switch=X1540 - 30, minutes=range(570, 961), hs=HS, drop=(), late=None):
    """late: {(right, strike): first minute quoted}, so the contract is listed but has no quote before then."""
    rows = []
    for m in minutes:
        px = before if m < switch else after
        for (r, k), mid in px.items():
            if (r, k) in drop or m < (late or {}).get((r, k), 0):
                continue
            rows.append({"ms_of_day": m * 60_000, "strike": k, "right": r,
                         "bid": max(round(mid - hs, 2), 0.0), "ask": round(mid + hs, 2)})
    return pd.DataFrame(rows)


def panel(before, after=None, **kw):
    return bd.QuotePanel(bd.normalize(chain(before, after or before, **kw)))


ENTRY = prices(100.0, 1.0, 3.0)                       # straddle at 100: 2.00
EXIT = prices(101.0, 0.10, 1.0)                       # SPY up a dollar into the close, little time value left


# ---------------------------------------------------------------- strikes

def test_atm_strike_is_nearest_spot_listed_for_both_rights_ties_lower():
    p = panel(prices(100.5, 1.0, 3.0))
    s = w5.pick(p, E945, 1.0)
    assert s["K"] == 100.0                            # 100.5 sits between 100 and 101: the lower wins


def test_wings_sit_w_straddles_out_ties_farther_and_at_least_one_strike_away():
    p = panel(ENTRY)
    for w, (kc, kp) in {1.0: (102.0, 98.0), 1.5: (103.0, 97.0), 2.0: (104.0, 96.0)}.items():
        s = w5.pick(p, E945, w)
        assert (s["K"], s["kc"], s["kp"]) == (100.0, kc, kp)
        assert s["M"] == pytest.approx(2.0)
    tiny = panel(prices(100.0, 0.10, 0.5))             # straddle 0.20: 1 x M is inside one strike
    s = w5.pick(tiny, E945, 1.0)
    assert (s["kc"], s["kp"]) == (101.0, 99.0)
    tie = {**ENTRY}                                    # M = 3.0 -> 1.5 x M = 4.5 is a tie between 104 and 105
    tie[("C", 100.0)], tie[("P", 100.0)] = 1.5, 1.5
    s = w5.pick(panel(tie), E945, 1.5)
    assert (s["kc"], s["kp"]) == (105.0, 95.0)


# ---------------------------------------------------------------- skips

def test_skips_missing_leg_zero_bid_short_and_wide_leg():
    late = panel(ENTRY, late={("C", 102.0): E945 + 15})          # listed, first quoted at 10:00
    assert w5.trade(late, E945, 1.0, "patient")["skip"] == "missing_leg"
    wide = panel(ENTRY, hs=0.30)                      # 0.60 wide on a 0.51 wing
    assert w5.trade(wide, E945, 1.0, "patient")["skip"] == "wide_leg"


def mid_q(px, r, k):
    return (max(round(px[(r, k)] - HS, 2), 0.0) + round(px[(r, k)] + HS, 2)) / 2


def test_zero_bid_short_is_its_own_reason():
    hs_short = chain(ENTRY, ENTRY)
    m = (hs_short["strike"] == 100.0) & (hs_short["right"] == "C")
    hs_short.loc[m, "bid"] = 0.0
    hs_short.loc[m, "ask"] = 0.01
    p = bd.QuotePanel(bd.normalize(hs_short))
    assert w5.trade(p, E945, 1.0, "patient")["skip"] == "zero_bid_short"


def test_half_day_sessions_are_dropped():
    assert w5.session_reason(date(2024, 11, 29)) == "half_day"
    assert w5.session_reason(date(2024, 11, 27)) is None


# ---------------------------------------------------------------- trade arithmetic

def by_hand(model, w=1.0, exit_px=EXIT):
    K, kc, kp = 100.0, {1.0: 102.0, 1.5: 103.0, 2.0: 104.0}[w], {1.0: 98.0, 1.5: 97.0, 2.0: 96.0}[w]

    def q(px, r, k):
        mid = px[(r, k)]
        return max(round(mid - HS, 2), 0.0), round(mid + HS, 2)

    def f(px, r, k, side):
        return bd.fill(*q(px, r, k), side, model)

    credit = f(ENTRY, "C", K, "sell") + f(ENTRY, "P", K, "sell") - f(ENTRY, "C", kc, "buy") - f(ENTRY, "P", kp, "buy")
    cost, sold = f(exit_px, "C", K, "buy") + f(exit_px, "P", K, "buy"), 0
    for r, k in (("C", kc), ("P", kp)):
        if q(exit_px, r, k)[0] >= 0.01:
            cost -= f(exit_px, r, k, "sell")
            sold += 1
    fees = (4 + 2 + sold) * bd.FEE
    pnl = 100 * (credit - cost - fees)
    max_loss = 100 * (max(kc - K, K - kp) - credit) + 100 * 8 * bd.FEE
    return pnl, max_loss, sold


@pytest.mark.parametrize("model", ["mid", "patient", "taker"])
def test_trade_pnl_and_return_per_lot(model):
    p = panel(ENTRY, EXIT)
    t = w5.trade(p, E945, 1.0, model)
    pnl, max_loss, sold = by_hand(model)
    assert t["pnl"] == pytest.approx(pnl)
    assert t["max_loss"] == pytest.approx(max_loss)
    assert t["ret"] == pytest.approx(pnl / max_loss)
    assert t["wings_sold"] == sold and t["exit_minute"] == X1540


def test_a_zero_bid_wing_expires_without_a_fee():
    p = panel(ENTRY, EXIT)
    t = w5.trade(p, E945, 2.0, "patient")              # the 96 put and 104 call are bid 0 at 15:40
    pnl, max_loss, sold = by_hand("patient", w=2.0)
    assert sold < 2 and t["wings_sold"] == sold
    assert t["pnl"] == pytest.approx(pnl)


def test_exit_1530_and_settlement_rows():
    p = panel(ENTRY, EXIT)
    t = w5.trade(p, E945, 1.0, "mid", exit_min=15 * 60 + 30)
    assert t["exit_minute"] == 15 * 60 + 30
    s = w5.settlement(p, E945, 1.0, "mid")
    # parity spot at the close is 101: the short 100 call owes 1, everything else expires
    credit = (mid_q(ENTRY, "C", 100.0) + mid_q(ENTRY, "P", 100.0) - mid_q(ENTRY, "C", 102.0)
              - mid_q(ENTRY, "P", 98.0))
    assert s["pnl"] == pytest.approx(100 * (credit - 1.0 - 4 * bd.FEE))


def test_b_wings_use_round_spot_and_plus_minus_5():
    p = panel(ENTRY, EXIT)
    t = w5.trade(p, E945, None, "mid", legs_fn=w5.legs_b)
    assert (t["K"], t["kc"], t["kp"]) == (100.0, 105.0, 95.0)


# ---------------------------------------------------------------- selection and pooling

def test_discovery_picks_highest_t_ties_smaller_w_then_earlier_entry():
    rows = []
    for e in w5.ENTRIES:
        for w in w5.WINGS:
            base = 0.02 if (e, w) in {(w5.ENTRIES[1], 1.5), (w5.ENTRIES[0], 2.0)} else 0.0
            for i in range(20):
                rows.append({"entry": e, "w": w, "ret": base + (0.01 if i % 2 else -0.01)})
    assert w5.choose_cell(pd.DataFrame(rows)) == (w5.ENTRIES[1], 1.5)   # equal t: 1.5 < 2.0


def test_pooled_daily_mean_averages_the_underlyings_that_traded():
    df = pd.DataFrame({"day": [date(2022, 1, 3)] * 3 + [date(2022, 1, 4)] * 2 + [date(2022, 1, 5)],
                       "und": ["SPY", "QQQ", "IWM", "SPY", "QQQ", "IWM"],
                       "ret": [0.03, 0.00, -0.03, 0.02, 0.04, -0.01]})
    d = w5.daily(df)
    assert list(d) == pytest.approx([0.0, 0.03, -0.01])
    s = w5.tstats(d)
    assert s["n"] == 3 and s["mean"] == pytest.approx(np.mean([0.0, 0.03, -0.01]))


def test_verdict_needs_all_four_and_is_provisional_without_fresh_data():
    ok = {"disc_mean": 0.01, "pool_t": 2.7, "pool_taker_mean": 0.001, "fresh_mean": 0.01, "fresh_t": 1.7}
    assert w5.verdict(ok)["pass"] and not w5.verdict(ok)["provisional"]
    for k, v in [("disc_mean", -0.01), ("pool_t", 2.5), ("pool_taker_mean", -0.001), ("fresh_t", 1.6)]:
        assert not w5.verdict({**ok, k: v})["pass"]
    spy_only = {**ok, "fresh_mean": None, "fresh_t": None}
    v = w5.verdict(spy_only)
    assert v["provisional"] and not v["pass"] and v["bars_1_3"]
