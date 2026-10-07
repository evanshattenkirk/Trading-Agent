"""The position card shows the exit plan the engine is actually using (M18): each book A position carries its own
plan in its payload, and a crew tweak that changes the config reaches the page as a `config` event."""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.exits import ExitPlan, Position

from test_safety import C, engine, open_pos


def events(e):
    got = []
    e.bus.taps.append(got.append)
    return got


def test_position_payload_carries_its_plan():
    e = engine()
    pos = open_pos(e)
    plan = pos.to_dict()["plan"]
    sw = e.cfg["exits"]["swing"]
    assert plan["stop_pct"] == e.cfg["exits"]["stop_loss_pct"]
    assert plan["trail_pct"] == sw["runner_trail_pct"] and plan["time_stop_min"] == sw["time_stop_min"]
    assert plan["exit_on_cross_back"] == sw["exit_on_cross_back"]
    assert plan["scale_outs"] == [{"at": s["at"], "fraction": s["fraction"]} for s in sw["scale_outs"]]
    assert e.snapshot()["positions"][0]["pos"]["plan"] == plan


def test_next_trade_tweak_shows_on_that_position_only():
    """A trade-scope tweak builds a per-position copy of the exits config; the page used to show the base config."""
    e = engine()
    ecfg = copy.deepcopy(e.cfg["exits"])
    ecfg["swing"]["runner_trail_pct"] = 0.12
    ecfg["swing"]["scale_outs"][0]["at"] = 0.20
    pos = Position(C, "SWING", 2, 1.00, e.feed.t)
    ExitPlan(ecfg, pos)
    plan = pos.to_dict()["plan"]
    assert plan["trail_pct"] == 0.12 and plan["scale_outs"][0]["at"] == 0.20
    assert e.cfg["exits"]["swing"]["runner_trail_pct"] != 0.12


def test_scalp_plan_and_a_position_without_one():
    e = engine()
    pos = Position(C, "SCALP", 1, 1.00, e.feed.t)
    assert pos.to_dict()["plan"] is None
    ExitPlan(e.cfg["exits"], pos)
    assert pos.to_dict()["plan"]["exit_on_cross_back"] == e.cfg["exits"]["scalp"]["exit_on_cross_back"]


def test_day_tweak_emits_the_new_config_and_updates_the_open_plan():
    e = engine()
    pos = open_pos(e)
    got = events(e)
    e.apply_tweak({"scope": "day", "title": "Tighter trail", "params": {"exits.swing.runner_trail_pct": 0.10,
                                                                         "strategy.rsi.upper": 65}}, e.feed.t)
    cfg = [x for x in got if x["type"] == "config"]
    assert len(cfg) == 1
    assert cfg[0]["config"]["exits"]["swing"]["runner_trail_pct"] == 0.10
    assert cfg[0]["config"]["strategy"]["rsi"]["upper"] == 65
    assert set(cfg[0]["config"]) == set(e.snapshot()["config"])             # the same subset the snapshot carries
    assert pos.to_dict()["plan"]["trail_pct"] == 0.10                        # the open position's plan reads it live


def test_next_trade_tweak_changes_no_config():
    e = engine()
    got = events(e)
    e.apply_tweak({"scope": "trade", "title": "x", "params": {"exits.swing.runner_trail_pct": 0.10}}, e.feed.t)
    assert not [x for x in got if x["type"] == "config"]
