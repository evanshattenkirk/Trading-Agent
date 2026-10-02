"""Tests for research/book_a_holdout.py: real quotes must follow the SPY price the engine is reacting to."""
import asyncio
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))
import book_a_holdout as h  # noqa: E402
from agentdesk.exits import Contract  # noqa: E402
from agentdesk.pricing import quote_from_model  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 1)


def panel_at(spot_by_minute: dict[int, float]):
    """Model quotes for strikes 755..775, one row per minute, each priced at that minute's spot."""
    from bd_real_quotes import QuotePanel, normalize
    rows = []
    for m, s in spot_by_minute.items():
        for k in range(755, 776):
            for r in ("call", "put"):
                b, a = quote_from_model(s, k, (16 * 60 + 15 - m) * 60, 0.15, r)
                rows.append((m * 60000, k * 1000, r[0].upper(), b, a))
    return QuotePanel(normalize(pd.DataFrame(rows, columns=["ms_of_day", "strike", "right", "bid", "ask"])))


class Feed:
    def __init__(self, minute: int, sec: int, px: float):
        self.t = datetime.combine(DAY, time(minute // 60, minute % 60, sec), ET).timestamp()
        self.px = px


def quote(panel, feed, strike=765, right="call", recenter=True):
    q = h.ThetaQuotes(feed, panel, recenter=recenter)
    return asyncio.run(q.quote(Contract("SPY", str(DAY), strike, right)))


@pytest.mark.parametrize("right", ["call", "put"])
def test_quote_follows_spot_since_the_row(right):
    m = 11 * 60
    panel = panel_at({mm: 765.0 for mm in range(570, 961)})     # the rows still show SPY at 765
    fresh = quote_from_model(766.0, 765, (16 * 60 + 15 - m) * 60 - 30, 0.15, right)
    q = quote(panel, Feed(m, 30, 766.0), right=right)            # SPY has since moved to 766
    stale = quote(panel, Feed(m, 30, 766.0), right=right, recenter=False)
    mid, fresh_mid, stale_mid = (q.bid + q.ask) / 2, sum(fresh) / 2, (stale.bid + stale.ask) / 2
    assert abs(mid - fresh_mid) < 0.03                            # re-centred on the new price
    assert abs(stale_mid - fresh_mid) > 0.3                       # the raw row is far off (the old bug)


def test_uses_the_previous_minute_row_never_the_current_one():
    m = 11 * 60
    panel = panel_at({mm: (765.0 if mm < m else 770.0) for mm in range(570, 961)})
    q = quote(panel, Feed(m, 5, 765.0), recenter=False)           # the 11:00 row (770) is not visible at 11:00:05
    assert (q.bid + q.ask) / 2 < quote_from_model(767.0, 765, 18000, 0.15, "call")[0]


def test_missing_strike_has_no_quote():
    panel = panel_at({mm: 765.0 for mm in range(570, 961)})
    assert quote(panel, Feed(11 * 60, 0, 765.0), strike=900) is None
