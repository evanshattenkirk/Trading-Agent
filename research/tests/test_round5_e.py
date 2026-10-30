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
