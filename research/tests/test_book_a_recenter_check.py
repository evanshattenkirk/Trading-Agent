"""Tests for research/book_a_recenter_check.py (the synthetic re-centring check quoted in book_a_variants.md 4a)."""
import math
import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))
import book_a_recenter_check as rc  # noqa: E402
import book_a_variants as bav  # noqa: E402
from agentdesk.bars import Bar  # noqa: E402
from agentdesk.config import load_config  # noqa: E402
from agentdesk.pricing import quote_from_model  # noqa: E402

ET = ZoneInfo("America/New_York")


def session(d, base, drift, amp, period, seed):
    rnd, out, px = random.Random(seed), [], base
    t0 = datetime.combine(d, time(9, 30), ET).timestamp()
    for m in range(390):
        o = px
        px = base + drift * m + amp * math.sin(2 * math.pi * m / period) + rnd.gauss(0, 0.03)
        out.append(Bar("1m", t0 + 60 * m, o, max(o, px) + 0.03, min(o, px) - 0.03, px, 1000.0, 0, t0 + 60 * m + 60))
    return out


def trading_days(n, d=date(2014, 3, 3)):
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.mark.parametrize("stamp", ["start", "end"])
def test_rows_are_stamped_at_the_minutes_open_or_close(stamp):
    bars = session(date(2014, 3, 3), 765, 0.0, 1.0, 30, 1)
    panel = rc.make_panel(bars, 0.128, stamp)
    m = 11 * 60
    b = bars[m - 570]
    s, left = (b.o, (16 * 60 + 15 - m) * 60) if stamp == "start" else (b.c, (16 * 60 + 15 - m - 1) * 60)
    assert panel.ba("C", 765.0, m) == quote_from_model(s, 765, left, 0.128, "call")
    assert panel.ba("P", 760.0, m) == quote_from_model(s, 760, left, 0.128, "put")


def test_run_day_reports_every_run_on_a_session_that_trades():
    days = trading_days(6)
    hist = [b for i, d in enumerate(days[:5]) for b in session(d, 760 + i, 0.002, 0.4, 40, i)]
    bars = session(days[5], 765, 0.004, 0.6, 30, 99)
    out = rc.run_day(bav.variant_cfg(load_config(ROOT / "config.yaml"), "baseline"), days[5], hist, bars, 0.128)
    assert set(out) == set(rc.RUNS)
    assert all(isinstance(v, (int, float)) for v in out.values())
    assert out["reference"] != 0.0                                  # the synthetic day does trade
