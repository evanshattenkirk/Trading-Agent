"""research/iex_vs_sip.py: matching helpers plus an offline end-to-end replay on synthetic prints."""
import asyncio
import importlib.util
import random
from datetime import date, time
from pathlib import Path

from agentdesk.bars import Bar
from agentdesk.clock import at_ct
from agentdesk.config import load_config

_spec = importlib.util.spec_from_file_location("iex_vs_sip", Path(__file__).resolve().parent.parent / "research" / "iex_vs_sip.py")
ivs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ivs)


def test_match_events_one_to_one_within_tolerance():
    assert ivs.match_events([10, 20, 30], [11, 12, 45], 5) == [(10, 11)]
    # each alt event is used once
    assert ivs.match_events([10, 11], [10], 5) == [(10, 10)]
    assert ivs.match_events([], [1, 2], 5) == []


def test_match_events_respects_tolerance():
    assert ivs.match_events([100], [161], 60) == []
    assert ivs.match_events([100], [160], 60) == [(100, 160)]
    assert ivs.match_events([100], [40], 60) == [(100, 40)]


def test_agreement_recall_precision_jaccard():
    a = ivs.agreement([0, 100, 200, 300], [5, 205, 900], 10)
    assert a["matched"] == 2
    assert a["recall"] == 0.5
    assert a["precision"] == round(2 / 3, 3)
    assert a["jaccard"] == round(2 / 5, 3)
    assert a["median_lag_s"] == 5.0


def test_state_agreement_step_functions():
    ref = [(0, True), (50, False)]
    alt = [(0, True), (60, False)]
    assert ivs.state_agreement(ref, alt, 0, 100) == 0.9
    assert ivs.state_agreement(ref, ref, 0, 100) == 1.0
    assert ivs.state_agreement([], alt, 0, 100) is None


def test_print_share_buckets():
    d = date(2026, 9, 25)
    t0 = at_ct(d, time(8, 30))
    sip = [t0 + i for i in range(0, 3600, 1)]          # first hour, 1 print/s
    iex = [t0 + i for i in range(0, 3600, 20)]         # 5% of them
    s = ivs.print_share(iex, sip, d)
    assert s["share"] == 0.05
    assert s["implied_tick"] == 7.2
    assert s["by_bucket"]["08:30"] == 0.05
    assert s["by_bucket"]["10:00"] is None


def test_compare_entries_same_setup():
    ref = [{"ts": 100, "setup": "SWING"}, {"ts": 500, "setup": "SCALP"}]
    alt = [{"ts": 130, "setup": "SWING"}, {"ts": 560, "setup": "SWING"}, {"ts": 2000, "setup": "SCALP"}]
    c = ivs.compare_entries(ref, alt, 120)
    assert c["matched"] == 2 and c["same_setup"] == 1
    assert c["by_setup"]["SCALP"]["matched"] == 0


def _synthetic_day(day: date, seed: int):
    rnd = random.Random(seed)
    t, px = at_ct(day, time(8, 30)), 650.0
    end = at_ct(day, time(15, 0))
    ts, ps, ss = [], [], []
    drift = 0.0
    while t < end:
        t += rnd.expovariate(3.0)                # ~3 prints/s
        if rnd.random() < 0.002:
            drift = rnd.choice([-1, 1]) * 0.004
        px = max(1.0, px + drift + rnd.gauss(0, 0.01))
        ts.append(t); ps.append(round(px, 2)); ss.append(100.0)
    return ts, ps, ss


def _synthetic_history(days: list[date]) -> list[Bar]:
    rnd = random.Random(7)
    px, out = 650.0, []
    for d in days:
        t0 = at_ct(d, time(8, 30))
        for m in range(390):
            o = px
            px += rnd.gauss(0, 0.08)
            out.append(Bar("1m", t0 + 60 * m, o, max(o, px) + 0.02, min(o, px) - 0.02, px, 1000, 10, t0 + 60 * m + 60))
    return out


def test_replay_and_compare_end_to_end_offline():
    cfg = load_config()
    day = date(2026, 9, 25)
    hist = _synthetic_history([date(2026, 9, d) for d in (18, 21, 22, 23, 24)])
    ts, ps, ss = _synthetic_day(day, 3)
    sip = (ts, ps, ss)
    iex = (ts[::20], ps[::20], ss[::20])                # 5% sample, like IEX
    runs = {"sip144": asyncio.run(ivs.replay(cfg, day, hist, sip, 144, 0.16)),
            "iex8": asyncio.run(ivs.replay(cfg, day, hist, iex, 8, 0.16))}
    for r in runs.values():
        assert set(r["crosses"]) == set(ivs.TFS)
        assert r["crosses"]["144t"]["up"], "tick series should produce crosses"
        assert r["crosses"]["1m"]["up"]
    share = ivs.print_share(list(iex[0]), list(sip[0]), day)
    cd = ivs.compare_day(day, runs, share)
    v = cd["variants"]["iex8"]
    assert 0 <= v["cross_up"]["144t"]["recall"] <= 1
    assert v["state_agree"]["1m"] is not None
    tot = ivs.roll_up([cd], {k: r["trades"] for k, r in runs.items()})
    assert tot["iex_share_median"] == share["share"]
    assert "daily_net_delta_vs_sip" in tot["variants"]["iex8"]


def test_replay_does_not_touch_the_callers_config():
    cfg = load_config()
    assert "tick_bar_effective" not in cfg["strategy"]
    day = date(2026, 9, 25)
    hist = _synthetic_history([date(2026, 9, d) for d in (18, 21, 22, 23, 24)])
    ts, ps, ss = _synthetic_day(day, 5)
    asyncio.run(ivs.replay(cfg, day, hist, (ts[:2000], ps[:2000], ss[:2000]), 8, 0.16))
    assert "tick_bar_effective" not in cfg["strategy"]
    assert cfg["crew"]["enabled"] == load_config()["crew"]["enabled"]


def test_main_runs_one_variant_per_iex_tick_size(tmp_path, monkeypatch):
    days = [date(2026, 9, d) for d in (17, 18, 21, 22, 23, 24, 25)]
    hist = _synthetic_history(days)

    async def fake_bars(feed, start, end, cache):
        return hist

    async def fake_trades(day, feed, cache):
        ts, ps, ss = _synthetic_day(day, day.day)
        return (ts, ps, ss) if feed == "sip" else (ts[::25], ps[::25], ss[::25])

    monkeypatch.setattr(ivs, "load_bars", fake_bars)
    monkeypatch.setattr(ivs, "load_trades", fake_trades)
    ivs.cli(["--days", "2", "--end", "2026-09-25", "--iex-ticks", "8,5", "--no-diag", "--env", "",
             "--out", str(tmp_path), "--cache", str(tmp_path / "cache")])
    import json
    s = json.loads((tmp_path / "summary.json").read_text())
    assert set(s["variants"]) == {"sip144", "iex8", "iex5"}
    assert s["run"]["iex_ticks"] == [8, 5]
    assert "cross_up" in s["variants"]["iex5"]
