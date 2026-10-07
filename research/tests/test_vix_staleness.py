"""L20: a prior VIX close more than 5 calendar days old is no prior close (the day is skipped), so a vix.csv that ends
early can't price months of sessions from one stale close."""
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH.parent))

import strategies_bcd as sb  # noqa: E402


def vix(*pairs):
    return pd.Series({d: v for d, v in pairs})


def test_prev_vix_uses_the_last_close_before_the_day():
    v = vix((date(2014, 2, 13), 14.0), (date(2014, 2, 14), 15.0), (date(2014, 2, 18), 16.0))
    out = sb.prev_vix(v, [date(2014, 2, 14), date(2014, 2, 18), date(2014, 2, 19)])
    assert list(out) == [14.0, 15.0, 16.0]                     # Fri -> Tue across Presidents' Day is 4 days: fine


def test_prev_vix_is_nan_when_the_last_close_is_over_five_days_old():
    v = vix((date(2026, 3, 2), 20.0))
    out = sb.prev_vix(v, [date(2026, 3, 3), date(2026, 3, 7), date(2026, 3, 8), date(2026, 6, 1)])
    assert out[0] == 20.0 and out[1] == 20.0                   # 1 and 5 days old
    assert np.isnan(out[2]) and np.isnan(out[3])               # 6 days and three months old


def test_prev_vix_is_nan_before_the_first_close():
    assert np.isnan(sb.prev_vix(vix((date(2026, 3, 2), 20.0)), [date(2026, 3, 2)])[0])


def test_vrp_spreads_skips_stale_vix_in_both_lookups():
    src = (RESEARCH / "vrp_spreads.py").read_text()
    assert src.count("MAX_VIX_AGE_DAYS") == 3                   # defined once, checked in vrp() and structures()


def test_book_a_variants_only_warns_about_stale_vix():
    src = (RESEARCH / "book_a_variants.py").read_text()
    body = src[src.index("def prior_vix"):src.index("def bars_of")]
    assert "print(" in body and "return vix_close[vdays[i]] if i >= 0 else None" in body   # value unchanged
