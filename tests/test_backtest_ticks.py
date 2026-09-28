"""backtest --ticks builds the "144t" series the way the live engine does for the same trade feed."""
import asyncio
import copy
import sys
from argparse import Namespace
from datetime import date, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import backtest
from agentdesk.clock import at_ct
from agentdesk.config import load_config, set_tick_bar_for_feed
from agentdesk.engine import Engine

CFG = load_config()


def cfg_for(feed):
    c = copy.deepcopy(CFG)
    c["data"]["alpaca"]["feed"] = feed
    return c


@pytest.mark.parametrize("feed,prints", [("iex", CFG["strategy"]["tick_bar_size_iex"]),
                                         ("sip", CFG["strategy"]["tick_bar_size"])])
def test_helper_matches_feed(feed, prints):
    c = cfg_for(feed)
    assert set_tick_bar_for_feed(c, feed) == prints
    assert c["strategy"]["tick_bar_effective"] == prints


def write_csv(path: Path, days: int) -> None:
    rows, d = ["t,o,h,l,c,v"], date(2026, 9, 1)
    while days:
        if d.weekday() < 5:
            t0 = at_ct(d, time(8, 30))
            rows += [f"{t0 + 60 * i},660,660.1,659.9,660,1000" for i in range(390)]
            days -= 1
        d += timedelta(days=1)
    path.write_text("\n".join(rows))


@pytest.mark.parametrize("feed", ["iex", "sip"])
def test_backtest_ticks_uses_the_live_tick_bar_size(tmp_path, monkeypatch, feed):
    seen = []

    async def fake_run_day(cfg, day, history, bars, args, trades_iter=None, rh=None):
        seen.append(Engine(cfg, backtest.ReplayFeed(day, history, bars), None, None, None, None).tick.size)
        return []

    async def no_trades(day, feed):
        if False:
            yield

    monkeypatch.setattr(backtest, "run_day", fake_run_day)
    monkeypatch.setattr(backtest, "_trades_cached", no_trades)
    csv = tmp_path / "bars.csv"
    write_csv(csv, CFG["data"]["history_days"] + 2)
    args = Namespace(sim_days=0, ticks=True, csv=str(csv), source="alpaca", days=2, end=None, options="model",
                     iv=0.16, out=str(tmp_path / "out"))
    asyncio.run(backtest.main(cfg_for(feed), args))
    want = CFG["strategy"]["tick_bar_size_iex"] if feed == "iex" else CFG["strategy"]["tick_bar_size"]
    assert seen and set(seen) == {want}
