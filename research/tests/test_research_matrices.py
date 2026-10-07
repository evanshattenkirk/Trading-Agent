"""L20: research.matrices never fills a session's opening bars from later bars, and prevC is the prior NYSE session's
close (NaN when the data lacks that session), not just the previous row."""
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import research as R  # noqa: E402


def frame(sessions):
    """sessions: {day: {minute_offset: close}} -> the 1m frame matrices() reads (hm = minutes after midnight ET)."""
    rows = []
    for d, bars in sessions.items():
        for off, c in bars.items():
            rows.append({"day": d, "hm": 570 + off, "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 10})
    return pd.DataFrame(rows)


def full(c0, n=390):
    return {k: c0 + 0.01 * k for k in range(n)}


def test_a_session_missing_its_0930_bar_is_dropped():
    mon, tue, wed = date(2014, 3, 3), date(2014, 3, 4), date(2014, 3, 5)
    late = {k: 100 + 0.01 * k for k in range(5, 390)}                 # 09:30-09:34 missing
    days, M, nb = R.matrices(frame({mon: full(100), tue: late, wed: full(101)}), 1)
    assert days == [mon, wed]
    assert M["O"].shape == (2, 390) and not np.isnan(M["O"]).any()


def test_gaps_inside_a_session_are_forward_filled_only():
    mon = date(2014, 3, 3)
    bars = full(100)
    for k in range(100, 110):
        del bars[k]
    days, M, _ = R.matrices(frame({mon: bars}), 1)
    assert np.allclose(M["C"][0, 100:110], bars[99])                  # the last close before the gap, not a later one


def test_prev_close_comes_from_the_prior_nyse_session():
    fri, tue = date(2014, 2, 14), date(2014, 2, 18)                   # Monday 2014-02-17 was Presidents' Day
    wed, fri2 = date(2014, 2, 19), date(2014, 2, 21)                  # Thursday 2014-02-20 is missing from the data
    days = [fri, tue, wed, fri2]
    C = np.array([[1.0, 10.0], [2.0, 20.0], [3.0, 30.0], [4.0, 40.0]])
    prev = R.prior_session_close(days, C)
    assert np.isnan(prev[0])
    assert prev[1] == 10.0 and prev[2] == 20.0                        # a holiday between is still the prior session
    assert np.isnan(prev[3])                                          # Thursday's close is unknown, not Wednesday's


def test_a_row_on_an_nyse_holiday_is_not_the_next_sessions_prev_close():
    thu, gf, mon = date(2014, 4, 17), date(2014, 4, 18), date(2014, 4, 21)    # the CFD can trade on Good Friday
    prev = R.prior_session_close([thu, gf, mon], np.array([[1.0, 5.0], [2.0, 6.0], [3.0, 7.0]]))
    assert prev[2] == 5.0                                             # Thursday's close, the prior NYSE session


def test_prev_close_knows_the_special_closures():
    fri, wed = date(2012, 10, 26), date(2012, 10, 31)                 # closed 29-30 Oct 2012 (Hurricane Sandy)
    prev = R.prior_session_close([fri, wed], np.array([[1.0, 5.0], [2.0, 6.0]]))
    assert prev[1] == 5.0
