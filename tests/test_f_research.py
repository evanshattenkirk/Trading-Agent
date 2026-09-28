"""research/strategy_f_intraday.py: the book F replication backtest (docs/BOOK_F_HANDOFF.md section 5)."""
from __future__ import annotations

import importlib.util
import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from f_fakes import CFG

_spec = importlib.util.spec_from_file_location("strategy_f_intraday",
                                               Path(__file__).resolve().parent.parent / "research" / "strategy_f_intraday.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

D0 = date(2024, 1, 2)


def days(n, start=D0):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def daily(n, px=100.0, rng=2.0, vol=2_000_000, start=D0):
    return [{"d": d, "o": px, "h": px + rng / 2, "l": px - rng / 2, "c": px, "v": vol} for d in days(n, start)]


def bar(t, o, h, l, c, v=1000):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def flat_day(px=100.0, until=960, start=575):
    return [bar(t, px, px + 0.05, px - 0.05, px) for t in range(start, until)]


# ------------------------------------------------------------------ Alpaca pull (resumable)
class FakeHTTP:
    """Serves /v2/stocks/bars pages; records every request."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params):
        self.calls.append(dict(params))
        tok = params.get("page_token")
        return self.pages[tok or "first"]


def test_bars_follows_page_tokens_and_groups_by_symbol():
    pages = {"first": {"bars": {"AAA": [{"t": "2024-01-02T14:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]},
                       "next_page_token": "p2"},
             "p2": {"bars": {"AAA": [{"t": "2024-01-02T14:31:00Z", "o": 1.5, "h": 2, "l": 1, "c": 1.8, "v": 5}],
                             "BBB": [{"t": "2024-01-02T14:30:00Z", "o": 9, "h": 9, "l": 9, "c": 9, "v": 1}]},
                    "next_page_token": None}}
    h = R.AlpacaHistory(FakeHTTP(pages), per_min=10_000)
    out = h.bars(["AAA", "BBB"], "1Min", "2024-01-02T14:30:00Z", "2024-01-02T14:35:00Z")
    assert [b["t"] for b in out["AAA"]] == [570, 571]            # minutes after midnight ET
    assert out["BBB"][0]["c"] == 9
    p = h.http.calls[0]
    assert p["feed"] == "sip" and p["adjustment"] == "split" and p["symbols"] == "AAA,BBB"


def test_or_window_pull_resumes_from_files_on_disk(tmp_path):
    store = R.Store(tmp_path)
    pages = {"first": {"bars": {"AAA": [{"t": "2024-01-02T14:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]},
                       "next_page_token": None}}
    h = R.AlpacaHistory(FakeHTTP(pages), per_min=10_000)
    R.pull_or_windows(store, h, ["AAA"], [D0])
    n = len(h.http.calls)
    assert store.or_path(D0).exists() and n == 1
    R.pull_or_windows(store, h, ["AAA"], [D0])                   # second run: nothing re-downloaded
    assert len(h.http.calls) == n
    assert store.read_bars(store.or_path(D0))["AAA"][0]["v"] == 10


def test_daily_csv_has_the_strategy_f_columns(tmp_path):
    store = R.Store(tmp_path)
    store.write_daily("AAA", daily(3))
    head = store.daily_path("AAA").read_text().splitlines()[0]
    assert head == "date,open,high,low,close,volume"
    back = store.read_daily("AAA")
    assert back[0]["d"] == D0 and back[-1]["c"] == 100.0


# ------------------------------------------------------------------ point-in-time universe
def test_universe_uses_only_prior_data_and_the_section_3_filters():
    ds = days(25)
    today = ds[-1]
    data = {
        "BIG": daily(25, px=100, vol=5_000_000),        # $500M/day
        "SMALL": daily(25, px=100, vol=500_000),        # $50M/day -> below $100M
        "CHEAP": daily(25, px=8, vol=50_000_000),       # price < $10
        "CALM": daily(25, px=100, rng=0.2, vol=5_000_000),   # ATR 0.2 < 0.50
        "NEW": daily(10, px=100, vol=5_000_000, start=ds[15]),   # < 20 days of history
        "NVDA": daily(25, px=100, vol=5_000_000),       # an extra
    }
    data["SMALL"][-1]["v"] = 10**9                      # today's volume must not count (not known pre-market)
    uni = R.universe_for_day(today, data, sp500={"BIG", "SMALL", "CHEAP", "CALM", "NEW"}, extras=["NVDA"],
                             cfg=CFG)
    assert set(uni) == {"BIG", "NVDA"}
    assert uni["BIG"]["atr"] == pytest.approx(2.0) and uni["BIG"]["dv20"] == pytest.approx(5e8)


def test_universe_keeps_only_the_top_n_sp500_names_by_dollar_volume():
    ds = days(25)
    data = {f"S{i}": daily(25, px=100, vol=2_000_000 + i * 100_000) for i in range(5)}
    cfg = {**CFG, "universe": {**CFG["universe"], "top_sp500_by_dollar_vol": 2}}
    uni = R.universe_for_day(ds[-1], data, sp500=set(data), extras=[], cfg=cfg)
    assert set(uni) == {"S4", "S3"}


def test_scan_ranks_with_rvol5_from_the_prior_14_sessions():
    uni = {"AAA": {"atr": 2.0, "dv20": 5e8, "close": 100}, "BBB": {"atr": 2.0, "dv20": 5e8, "close": 100}}
    today = {"AAA": [bar(570 + i, 100, 100.5, 99.9, 100.2, 600) for i in range(5)],     # vol 3000, green
             "BBB": [bar(570 + i, 100, 100.1, 99.5, 99.8, 600) for i in range(5)]}      # red
    hist = [{"AAA": 1000, "BBB": 1000}] * 14
    res = R.scan_day(uni, today, hist, CFG)
    assert [r.symbol for r in res.picks] == ["AAA"]
    a = res.picks[0]
    assert a.rvol5 == pytest.approx(3.0) and a.or_high == pytest.approx(100.5) and a.open == 100 and a.close == 100.2
    assert [r.symbol for r in res.shorts] == ["BBB"]


# ------------------------------------------------------------------ one day
def pick(sym="AAA", or_high=100.0, atr=2.0, rank=1):
    from agentdesk.books.f_stocks_in_play import ScanRow
    return ScanRow(sym, rvol5=3.0, open=99.5, close=99.9, or_high=or_high, or_low=99.0, vol5=1, atr=atr,
                   dollar_vol20=5e8, rank=rank, picked=True)


def test_day_long_is_held_to_the_1555_open_with_slippage():
    bars = flat_day(99.9, start=575, until=580) + [bar(580, 99.9, 100.3, 99.9, 100.2)] + flat_day(101.0, start=581)
    tr = R.simulate_day([pick()], {"AAA": bars}, CFG, slip_bp=2)
    assert len(tr) == 1
    t = tr[0]
    assert t["entry"] == pytest.approx(100.0 * 1.0002)
    assert t["qty"] == 9                                       # $1,000 / $100.05 limit -> 9 (risk cap 125 shares)
    assert t["exit"] == pytest.approx(101.0 * 0.9998) and t["why"] == "exit 15:55"
    assert t["r"] == pytest.approx((t["exit"] - t["entry"]) / (t["entry"] - t["stop"]))


def test_day_trigger_after_the_1030_cutoff_is_ignored():
    bars = flat_day(99.9, start=575, until=630) + [bar(630, 99.9, 100.5, 99.9, 100.4)] + flat_day(100.4, start=631)
    assert R.simulate_day([pick()], {"AAA": bars}, CFG) == []


def test_day_stop_and_one_entry_per_name():
    bars = ([bar(575, 99.9, 100.3, 99.9, 100.2), bar(576, 100.1, 100.1, 99.7, 99.7)]      # stop 99.8 hit
            + [bar(577, 99.9, 100.6, 99.9, 100.5)] + flat_day(100.5, start=578))            # breaks again: no re-entry
    tr = R.simulate_day([pick()], {"AAA": bars}, CFG, slip_bp=0)
    assert len(tr) == 1 and tr[0]["why"] == "stop" and tr[0]["exit"] == pytest.approx(99.8)
    assert tr[0]["r"] == pytest.approx(-1.0)


def test_day_half_day_exits_at_1255():
    bars = [bar(575, 99.9, 100.3, 99.9, 100.2)] + flat_day(100.5, start=576, until=780)
    tr = R.simulate_day([pick()], {"AAA": bars}, CFG, slip_bp=0, half_day=True)
    assert tr[0]["why"] == "exit 12:55" and tr[0]["exit"] == pytest.approx(100.5)


def test_day_daily_loss_halts_the_book_and_flattens_the_rest():
    cfg = {**CFG, "risk_per_trade": 50, "max_notional": 100_000, "daily_loss": 75}
    stop_bars = [bar(575, 99.9, 100.3, 99.9, 100.2), bar(576, 100.1, 100.1, 99.0, 99.0)] + flat_day(99.0, start=577)
    late = [bar(575, 99.9, 99.95, 99.8, 99.9), bar(576, 99.9, 99.95, 99.8, 99.9),
            bar(577, 99.9, 100.3, 99.9, 100.2)] + flat_day(100.2, start=578)
    picks = [pick("L1", rank=1), pick("L2", rank=2), pick("LATE", rank=3)]
    tr = R.simulate_day(picks, {"L1": stop_bars, "L2": stop_bars, "LATE": late}, cfg, slip_bp=0)
    assert [t["symbol"] for t in tr] == ["L1", "L2"]           # -$50 + -$50 = -$100 <= -$75: LATE never enters
    assert sum(t["pnl"] for t in tr) == pytest.approx(-100.0, abs=1.0)


# ------------------------------------------------------------------ stats
def test_clustered_t_with_one_trade_per_cluster_is_the_plain_t():
    xs = [1.0, -0.5, 2.0, 0.3, -1.1, 0.8]
    n, m = len(xs), sum(xs) / len(xs)
    s = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    assert R.clustered_t(xs, list(range(n))) == pytest.approx(m / (s / math.sqrt(n)))


def test_summary_pf_win_rate_and_pass_bar():
    trades = [{"day": D0, "symbol": "A", "pnl": 30.0, "r": 1.2, "bp": 30.0},
              {"day": D0, "symbol": "B", "pnl": -10.0, "r": -0.4, "bp": -10.0},
              {"day": D0 + timedelta(days=1), "symbol": "C", "pnl": -10.0, "r": -0.4, "bp": -10.0}]
    s = R.summarize(trades, [D0, D0 + timedelta(days=1)], balance=10_000)
    assert s["trades"] == 3 and s["win_rate"] == pytest.approx(1 / 3)
    assert s["pf"] == pytest.approx(1.5) and s["mean_r"] == pytest.approx(0.4 / 3)
    assert s["worst_day"]["pnl"] == pytest.approx(-10.0)
    assert R.passes({"pf": 1.2, "t": 2.5}, {"mean_r": 0.1}, {"mean_r": 0.05}) is True
    assert R.passes({"pf": 1.2, "t": 2.5}, {"mean_r": 0.1}, {"mean_r": -0.01}) is False
    assert R.passes({"pf": 1.05, "t": 2.5}, {"mean_r": 0.1}, {"mean_r": 0.1}) is False


# ------------------------------------------------------------------ whole pipeline on a fake Alpaca
class FakeAlpaca:
    """Deterministic bars for any request: 'HOT' has a big green opening range on its last day, 'COLD' never
    does. Enough to run pull -> scan -> simulate -> report."""

    def __init__(self, dates):
        self.dates, self.calls = dates, 0

    def get(self, url, params):
        from datetime import datetime as dt
        self.calls += 1
        syms = params["symbols"].split(",")
        start = dt.fromisoformat(params["start"].replace("Z", "+00:00"))
        end = dt.fromisoformat(params["end"].replace("Z", "+00:00"))
        out = {}
        for s in syms:
            if params["timeframe"] == "1Day":
                out[s] = [{"t": f"{d}T05:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100, "v": 3_000_000}
                          for d in self.dates if start.date() <= d <= end.date()]
                continue
            d = start.astimezone(R.F.ET).date()
            hot = s == "HOT" and d == self.dates[-1]
            bars, t = [], start
            while t <= end:
                m = t.astimezone(R.F.ET)
                mm = m.hour * 60 + m.minute
                if mm < 575:
                    o, c, v = (100, 100.3, 5000) if hot else (100, 100.05, 1000)
                    h, l = max(o, c) + 0.02, min(o, c) - 0.02
                else:
                    o = c = 101.0 if hot and mm > 580 else 100.3 if hot else 100.0
                    h, l, v = o + 0.05, o - 0.05, 1000
                bars.append({"t": t.isoformat().replace("+00:00", "Z"), "o": o, "h": h, "l": l, "c": c, "v": v})
                t += timedelta(minutes=1)
            out[s] = bars
        return {"bars": out, "next_page_token": None}


def test_pipeline_pulls_backtests_and_writes_the_report(tmp_path):
    ds = days(40)
    store = R.Store(tmp_path / "cache")
    h = R.AlpacaHistory(FakeAlpaca(ds), per_min=10**6)
    syms = ["HOT", "COLD", "SPY"]
    R.pull_daily(store, h, syms, ds[0], ds[-1])
    data = {s: store.read_daily(s) for s in syms}
    R.pull_or_windows(store, h, ["HOT", "COLD"], ds)
    R.pull_candidate_days(store, h, data, {"HOT", "COLD"}, [], ds)
    calls = h.calls
    R.pull_candidate_days(store, h, data, {"HOT", "COLD"}, [], ds)            # resume: nothing new
    assert h.calls == calls
    assert set(store.read_bars(store.day_path(ds[-1]))) == {"HOT"}
    res = R.report(store, data, {"HOT", "COLD"}, [], ds, tmp_path)
    assert res["primary"]["trades"] == 1
    t = json.loads((tmp_path / "strategy_f_intraday_results.json").read_text())
    assert t["primary"]["trades"] == 1 and "sensitivity" in t
    md = (tmp_path / "strategy_f_intraday.md").read_text()
    assert md.startswith("**PASS**") or md.startswith("**FAIL**")
