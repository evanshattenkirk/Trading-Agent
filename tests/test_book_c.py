import pytest

from books_fakes import DAY, ct_ts
from agentdesk.bars import Bar
from agentdesk.books.base import MarketContext
from agentdesk.books.orb_bull_put import OrbBullPut
from agentdesk.config import load_config

C = dict(load_config()["books"]["C_orb_bull_put"])


def at(h, m):
    """ct_ts that lets minutes run past 59 (09:65 = 10:05)."""
    return ct_ts(h, 0) + m * 60


def bar5(h, m, o, c, v=1000, hi=None, lo=None):
    t = at(h, m)
    return Bar("5m", t, o, hi if hi is not None else max(o, c), lo if lo is not None else min(o, c), c, v, 10, t + 300)


def ctx(now, vwap=760.0, open_=()):
    return MarketContext(now=now, day=DAY, spot=None, vwap=vwap, open=list(open_))


def primed(rsi_path=True):
    """Strategy warmed so EMA20/RSI/volume median are ready: 40 gently rising, choppy prior bars, then today's
    opening range 08:30-09:00 CT with a high of 764.00. The 09:00 breakout bar then has RSI ~67 and a rising EMA20."""
    s = OrbBullPut(C)
    px = 760.0
    for i in range(40):                                   # yesterday's afternoon, 5m bars
        t = ct_ts(10, 0) - 86400 + i * 300
        step = 0.30 if i % 3 else -0.45
        s._update(Bar("5m", t, px, px + 0.4, px - 0.4, px + step, 1000, 10, t + 300))
        px += step
    for i, c in enumerate([763.0, 762.4, 763.4, 762.8, 763.6, 763.2]):
        s.on_bar("5m", bar5(8, 30 + 5 * i, c - 0.3, c, hi=764.0 if i == 4 else c + 0.1), ctx(at(8, 35 + 5 * i)))
    return s


def test_fresh_breakout_enters_short_put_below_spot():
    s = primed()
    assert s.orh[DAY] == pytest.approx(764.0)
    it = s.on_bar("5m", bar5(9, 0, 763.7, 764.4, v=1200), ctx(ct_ts(9, 5)))
    assert [(l.right, l.strike, l.side) for l in it.legs] == [("put", 764, "sell"), ("put", 762, "buy")]
    assert it.credit and it.width == 2 and it.budget == 400 and it.meta["und"] == pytest.approx(764.4)


def test_close_above_orh_without_fresh_cross_does_not_fire():
    s = primed()
    s.on_bar("5m", bar5(9, 0, 763.9, 764.3, v=1200), ctx(ct_ts(9, 5), vwap=770.0))    # below VWAP: no entry
    assert s.on_bar("5m", bar5(9, 5, 764.3, 764.6, v=1200), ctx(ct_ts(9, 10))) is None  # prior close already above


def test_each_condition_blocks():
    for kw, why in ((dict(vwap=765.0), "vwap"),):
        s = primed()
        assert s.on_bar("5m", bar5(9, 0, 763.7, 764.4, v=1200), ctx(ct_ts(9, 5), **kw)) is None, why
    s = primed()
    assert s.on_bar("5m", bar5(9, 0, 764.6, 764.4, v=1200), ctx(ct_ts(9, 5))) is None      # red candle
    s = primed()
    assert s.on_bar("5m", bar5(9, 0, 763.7, 764.4, v=500), ctx(ct_ts(9, 5))) is None       # volume < 0.8 x median


def test_outside_window_and_duplicate_suppression():
    s = primed()
    assert s.on_bar("5m", bar5(8, 55, 763.7, 764.4, v=1200), ctx(ct_ts(9, 0))) is None     # inside the opening range
    s = primed()
    open_pos = type("P", (), {"opened_ts": ct_ts(9, 5), "meta": {"und": 764.4}})()
    assert s.on_bar("5m", bar5(9, 0, 763.7, 764.4, v=1200), ctx(ct_ts(9, 5), open_=[open_pos])) is None


def test_no_signal_from_the_bar_the_last_trade_exited_in():
    s = primed()
    s.on_closed(None, ct_ts(9, 2))
    assert s.on_bar("5m", bar5(9, 0, 763.7, 764.4, v=1200), ctx(ct_ts(9, 5))) is None


def test_underlying_exits_first_wins():
    s = OrbBullPut(C)
    pos = type("P", (), {"opened_ts": ct_ts(9, 5), "meta": {"und": 764.4}})()
    c = lambda now, spot: MarketContext(now=now, day=DAY, spot=spot, vwap=None)
    assert s.on_quote(pos, None, ct_ts(9, 10), c(ct_ts(9, 10), 764.4 * (1 - 0.0018))).urgent
    assert "target" in s.on_quote(pos, None, ct_ts(9, 10), c(ct_ts(9, 10), 764.4 * 1.0045)).reason
    assert "45 min" in s.on_quote(pos, None, ct_ts(9, 50), c(ct_ts(9, 50), 764.5)).reason
    assert s.on_quote(pos, None, ct_ts(9, 49), c(ct_ts(9, 49), 764.5)) is None
    late = type("P", (), {"opened_ts": ct_ts(13, 50), "meta": {"und": 764.4}})()
    assert "14:15" in s.on_quote(late, None, ct_ts(14, 15), c(ct_ts(14, 15), 764.5)).reason


def test_macd_cross_down_exits_on_a_later_bar():
    s = primed()
    pos = type("P", (), {"opened_ts": ct_ts(9, 5), "meta": {"und": 764.4}})()
    out = None
    px = 764.4
    for i in range(12):
        px -= 0.6
        out = s.on_bar("5m", bar5(9, 5 + 5 * i, px + 0.6, px, v=1200), ctx(at(9, 10 + 5 * i), open_=[pos]))
        if out:
            break
    assert out is not None and "MACD" in out.reason


def test_iex_volume_baseline_rebuilds_from_live_bars():             # review focus 5
    s = OrbBullPut({**C, "reset_volume_on_live": True})
    t = ct_ts(10, 0) - 86400
    for i in range(25):
        s._update(Bar("5m", t + i * 300, 750, 750.2, 749.8, 750.1, 20000, 10, t + i * 300 + 300))
    assert len(s.vols) == 20
    s.on_bar("5m", bar5(8, 30, 760.0, 760.2, v=900), ctx(ct_ts(8, 35)))
    assert list(s.vols) == [900]
