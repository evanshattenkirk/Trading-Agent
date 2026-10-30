"""Tests for research/round5_e.py and research/fetch_thetadata_e.py (rules in research/round5_prereg.md section 3).
Synthetic data only."""
import sys
from datetime import date
from pathlib import Path

import pytest

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import fetch_thetadata_e as fe  # noqa: E402
import round5_e as re5  # noqa: E402


def row(timing, report, entry, symbol="AAPL", status="kept"):
    return {"symbol": symbol, "timing": timing, "report_session": report, "entry_session": entry, "status": status,
            "accepted_et": "x"}


# ---------------------------------------------------------------- report day and timing

def test_report_day_and_timing_follow_the_prereg_and_amendment_1():
    # bmo: D = report session, am.  amc: D = entry session (the filing day), pm.
    assert re5.event(row("bmo", "2024-05-02", "2024-05-01")) == {"symbol": "AAPL", "D": date(2024, 5, 2),
                                                                 "timing": "am"}
    assert re5.event(row("amc", "2024-05-03", "2024-05-02"))["D"] == date(2024, 5, 2)
    assert re5.event(row("amc", "2024-05-03", "2024-05-02"))["timing"] == "pm"
    # dmh: D = the filing day, which X1's file stores as report_session (amendment 1), timing unknown
    e = re5.event(row("dmh", "2024-05-02", "2024-05-01"))
    assert e["D"] == date(2024, 5, 2) and e["timing"] == "unknown"
    assert re5.event(row("bmo", "2024-05-02", "2024-05-01", status="dropped: x")) is None


def test_sessions_before_d_skip_weekends_and_holidays():
    d = date(2024, 7, 8)                                  # Monday; July 4 (Thu) is a holiday, July 3 a half day
    assert re5.t_minus(d, 0) == d
    assert re5.t_minus(d, 1) == date(2024, 7, 5)
    assert re5.t_minus(d, 2) == date(2024, 7, 3)
    assert re5.t_minus(d, 3) == date(2024, 7, 2)


def test_exit_day_is_t_minus_1_unless_the_report_is_after_the_close():
    d = date(2024, 5, 2)
    assert re5.exit_day(d, "am") == date(2024, 5, 1)
    assert re5.exit_day(d, "unknown") == date(2024, 5, 1)
    assert re5.exit_day(d, "pm") == d


def test_clock_is_1545_et_or_1220_on_half_days():
    assert re5.clock_minute(date(2024, 5, 1)) == 15 * 60 + 45
    assert re5.clock_minute(date(2024, 7, 3)) == 12 * 60 + 20
    assert re5.clock_minute(date(2024, 11, 29)) == 12 * 60 + 20


def test_post_and_pre_expiries_depend_on_timing():
    listed = {date(2024, 4, 26), date(2024, 5, 2), date(2024, 5, 3), date(2024, 5, 10)}
    d = date(2024, 5, 2)
    assert re5.post_expiry(listed, d, "am") == date(2024, 5, 2)          # first >= D
    assert re5.post_expiry(listed, d, "pm") == date(2024, 5, 3)          # first > D
    assert re5.post_expiry(listed, d, "unknown") == date(2024, 5, 3)
    assert re5.pre_expiry(listed, d, "am") == date(2024, 4, 26)          # last < D
    assert re5.pre_expiry(listed, d, "unknown") == date(2024, 4, 26)
    assert re5.pre_expiry(listed, d, "pm") == date(2024, 5, 2)           # last <= D
    assert re5.post_expiry(set(), d, "am") is None and re5.pre_expiry(set(), d, "pm") is None


def test_meta_traded_as_fb_before_june_2022():
    assert re5.root("META", date(2022, 6, 8)) == "FB"
    assert re5.root("META", date(2022, 6, 9)) == "META"
    assert re5.root("AAPL", date(2019, 1, 2)) == "AAPL"


# ---------------------------------------------------------------- fetch plan

def test_fetch_plan_covers_t_minus_10_to_the_exit_and_the_pre_expiry_until_it_expires():
    ev = {"symbol": "AAPL", "D": date(2024, 5, 2), "timing": "pm"}
    listed = {date(2024, 4, 19), date(2024, 4, 26), date(2024, 5, 2), date(2024, 5, 3), date(2024, 5, 10)}
    job = fe.plan_event(ev, lambda r: listed)
    t10 = re5.t_minus(ev["D"], 10)
    assert job["root"] == "AAPL" and job["post"] == date(2024, 5, 3) and job["pre"] == date(2024, 5, 2)
    post = [s for e, s in job["pulls"] if e == job["post"]]
    pre = [s for e, s in job["pulls"] if e == job["pre"]]
    assert post[0] == t10 and post[-1] == date(2024, 5, 2) and len(post) == 11
    assert pre == post                                   # pm: the pre expiry is D itself, alive to the exit
    am = fe.plan_event({**ev, "timing": "am"}, lambda r: listed)
    assert am["pre"] == date(2024, 4, 26) and am["post"] == date(2024, 5, 2)
    pre_am = [s for e, s in am["pulls"] if e == am["pre"]]
    assert pre_am[0] == t10 and pre_am[-1] == date(2024, 4, 26)        # stops on the short leg's expiry day


def test_fetch_plan_skips_events_without_a_post_expiry():
    ev = {"symbol": "AAPL", "D": date(2024, 5, 2), "timing": "pm"}
    job = fe.plan_event(ev, lambda r: {date(2024, 4, 26)})
    assert job["skip"] == "no post-announcement expiry"


# ---------------------------------------------------------------- replay (part 2)

import math as _m  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

HS = 0.01


def norm_rows(px_by_minute, minutes=range(570, 961), hs=HS, skip=()):
    """px_by_minute(m) -> {(right, strike): mid}. skip: {(right, strike, minute)} with no row that minute."""
    rows = []
    for m in minutes:
        for (r, k), mid in px_by_minute(m).items():
            if (r, k, m) in skip:
                continue
            rows.append({"minute": m, "right": r, "strike": float(k), "bid": round(mid - hs, 2),
                         "ask": round(mid + hs, 2)})
    return pd.DataFrame(rows)


def fp(px_by_minute, **kw):
    return re5.FreshPanel(norm_rows(px_by_minute, **kw))


def flat(px):
    return lambda m: px


def straddle_px(spot, c, p, strikes=(95.0, 100.0, 105.0)):
    """Parity-consistent book around `spot`: ATM 100 call c and put p (c - p = spot - 100)."""
    out = {}
    for k in strikes:
        out[("C", k)] = max(spot - k, 0.0) + (c - max(spot - 100, 0.0)) * math.exp(-abs(k - 100) / 5)
        out[("P", k)] = out[("C", k)] - (spot - k)
    return out


import math  # noqa: E402


def test_fresh_panel_keeps_gaps_and_last_fills_within_the_session():
    p = fp(flat({("C", 100.0): 2.0}), skip={("C", 100.0, 600)})
    assert p.fresh("C", 100.0, 600) is None
    assert p.last("C", 100.0, 600) == (1.99, 2.01)
    assert p.fresh("C", 100.0, 601) == (1.99, 2.01)
    assert p.last("C", 100.0, 569) is None
    crossed = re5.FreshPanel(pd.DataFrame([{"minute": 600, "right": "C", "strike": 100.0, "bid": 2.1, "ask": 2.0}]))
    assert crossed.fresh("C", 100.0, 600) is None


def test_implied_vol_inverts_black_scholes():
    for right in ("C", "P"):
        px = re5.bs_price(100.0, 105.0, 7 / 365, 0.04, 0.35, right)
        assert re5.implied_vol(px, 100.0, 105.0, 7 / 365, 0.04, right) == pytest.approx(0.35, abs=1e-6)
    assert re5.implied_vol(0.0001, 100.0, 80.0, 7 / 365, 0.04, "C") is None      # below intrinsic


def test_iv_filter_matches_book_e():
    assert re5.iv_ok(0.5, [0.4, 0.3, 0.2]) == (True, None)                     # fewer than 4 cycles: off
    assert re5.iv_ok(0.5, [0.4, 0.3, 0.2, 0.6])[0]                             # 3/4 = 75% <= 80%
    ok, pct = re5.iv_ok(0.7, [0.4, 0.3, 0.2, 0.6])
    assert not ok and pct == 1.0
    assert re5.iv_ok(None, [0.1] * 6) == (True, None)


# A pm reporter on Thursday 2024-05-02: T-3 = Mon 04-29, exit T-0 = 05-02 at 15:45, post expiry Fri 05-03.
EV = {"symbol": "AAPL", "D": date(2024, 5, 2), "timing": "pm", "post": date(2024, 5, 3), "pre": date(2024, 5, 2),
      "sector": "tech"}
CTX = re5.Context(vix=pd.Series({date(2024, 4, 1): 15.0}), rate=pd.Series({date(2024, 4, 1): 5.0}))
ENTRY_M = 15 * 60 + 45


def e1_quotes(path_mid=None):
    """Entry at Mon 15:45 with the straddle at 4.00; afterwards the ATM legs follow path_mid(day, minute)."""
    q = {}
    for d in re5.sessions_between(date(2024, 4, 29), date(2024, 5, 2)):
        def px(m, d=d):
            if path_mid is None or (d == date(2024, 4, 29) and m <= ENTRY_M):
                return straddle_px(100.0, 2.0, 2.0)
            c = path_mid(d, m) / 2
            return straddle_px(100.0, c, c)
        q[(d, EV["post"])] = fp(px)
    return q


def test_e1_time_exit_arithmetic_under_patient_fills():
    t = re5.e1(EV, e1_quotes(), "patient", CTX, re5.IVHistory())
    # buy call + put at min(ask, mid + 0.01) = 2.01 each; sell at max(bid, mid - 0.01) = 1.99 each on exit day 15:45
    assert t["entry_day"] == date(2024, 4, 29) and t["exit_day"] == date(2024, 5, 2) and t["why"] == "time"
    assert t["debit"] == pytest.approx(4.02)
    assert t["pnl"] == pytest.approx(100 * (3.98 - 4.02) - 4 * 0.04)
    assert t["ret"] == pytest.approx(t["pnl"] / (100 * 4.02))


def test_e1_take_profit_and_stop_fire_on_the_combo_mid():
    up = e1_quotes(lambda d, m: 5.0 if (d, m) >= (date(2024, 4, 30), 600) else 4.0)
    t = re5.e1(EV, up, "mid", CTX, re5.IVHistory())
    assert t["why"] == "take profit" and t["exit_day"] == date(2024, 4, 30) and t["exit_minute"] == 600
    assert t["pnl"] == pytest.approx(100 * (5.0 - 4.0) - 4 * 0.04)
    down = e1_quotes(lambda d, m: 2.7 if (d, m) >= (date(2024, 5, 1), 700) else 4.0)
    t = re5.e1(EV, down, "mid", CTX, re5.IVHistory())
    assert t["why"] == "stop" and t["exit_day"] == date(2024, 5, 1)


def test_e1_no_tp_stop_variant_holds_to_the_exit():
    up = e1_quotes(lambda d, m: 5.0 if (d, m) >= (date(2024, 4, 30), 600) else 4.0)
    t = re5.e1(EV, up, "mid", CTX, re5.IVHistory(), tp_stop=False)
    assert t["why"] == "time" and t["exit_day"] == date(2024, 5, 2)


def test_e1_skips():
    assert re5.e1({**EV, "post": date(2024, 5, 10)}, e1_quotes(), "mid", CTX, re5.IVHistory())["skip"] == "dte"
    hot = re5.Context(vix=pd.Series({date(2024, 4, 26): 31.0}), rate=CTX.rate)
    assert re5.e1(EV, e1_quotes(), "mid", hot, re5.IVHistory())["skip"] == "vix"
    wide = {k: re5.FreshPanel(v.df.assign(bid=v.df.bid - 0.20)) for k, v in e1_quotes().items()}
    assert re5.e1(EV, wide, "mid", CTX, re5.IVHistory())["skip"] == "wide_leg"
    assert re5.e1(EV, {}, "mid", CTX, re5.IVHistory())["skip"] == "no_entry_quotes"


def test_e2_uses_the_first_passing_day_and_leaves_on_the_short_expiry():
    # am reporter on Thu 2024-05-02: pre = Fri 04-26 (last < D), post = Thu 05-02 (first >= D); exit T-1 = 05-01.
    ev = {**EV, "timing": "am", "pre": date(2024, 4, 26), "post": date(2024, 5, 2)}
    t10, t9 = re5.t_minus(ev["D"], 10), re5.t_minus(ev["D"], 9)
    q = {}
    for d in re5.sessions_between(t10, date(2024, 5, 1)):
        q[(d, ev["post"])] = fp(flat(straddle_px(100.0, 3.0, 3.0)))
        if d <= ev["pre"]:
            wide = 0.30 if d == t10 else HS                              # T-10's short leg fails the 5% check
            q[(d, ev["pre"])] = fp(flat(straddle_px(100.0, 1.5, 1.5)), hs=wide)
    t = re5.e2(ev, q, "mid", CTX, re5.IVHistory())
    assert t["entry_day"] == t9 and t["exit_day"] == ev["pre"] and t["exit_minute"] == 15 * 60 + 15
    assert t["debit"] == pytest.approx(1.5) and t["why"] == "short expiry"


def test_clustered_by_entry_date_averages_same_day_events_first():
    df = pd.DataFrame({"entry_day": [date(2024, 1, 2)] * 2 + [date(2024, 1, 3), date(2024, 1, 4)],
                       "ret": [0.2, 0.0, -0.1, 0.3]})
    s = re5.clustered(df)
    x = np.array([0.1, -0.1, 0.3])
    assert s["dates"] == 3 and s["mean"] == pytest.approx(x.mean())
    assert s["t"] == pytest.approx(x.mean() / (x.std(ddof=1) / _m.sqrt(3)))


def test_verdict_bars():
    ok = {"is_mean": 0.01, "oos_t": 2.6, "oos_taker_mean": 0.001}
    assert re5.verdict(ok)["pass"]
    for k, v in [("is_mean", -0.01), ("oos_t", 2.5), ("oos_taker_mean", -0.001)]:
        assert not re5.verdict({**ok, k: v})["pass"]


def test_driver_runs_one_symbol_end_to_end(tmp_path):
    ev = {**EV, "symbol": "AAPL"}
    rows = []
    for d in re5.sessions_between(re5.t_minus(ev["D"], 10), ev["D"]):
        for e, (c, p) in ((ev["post"], (2.0, 2.0)), (ev["pre"], (1.0, 1.0))):
            for (r, k), mid in straddle_px(100.0, c, p).items():
                for m in range(570, 961, 5):
                    rows.append({"ms_of_day": m * 60_000, "strike": k, "right": r, "bid": round(mid - HS, 2),
                                 "ask": round(mid + HS, 2), "session": d.isoformat(), "expiry": e.isoformat()})
    pd.DataFrame(rows).to_parquet(tmp_path / "AAPL_2024-05-02.parquet")
    out = re5.run_symbol(("AAPL", [ev, {**ev, "D": date(2024, 8, 1), "post": None}], str(tmp_path), CTX.vix,
                          CTX.rate))
    df = pd.DataFrame(out)
    main = df[(df.variant == "main") & (df.model == "patient")]
    assert set(main.st) == {"E1", "E2"} and (df.skip == "no_quote_file").sum() == 1
    e1row = main[main.st == "E1"].iloc[0]
    assert pd.isna(e1row.get("skip")) and e1row["why"] == "time"
    res = re5.summarize(df, {"tech": ["AAPL"]})
    assert res["E1"]["out_of_sample"]["patient"]["trades"] == 1
    assert res["E1"]["reported_only"]["as_built_portfolio_2018_on (patient)"]["trades"] == 1
