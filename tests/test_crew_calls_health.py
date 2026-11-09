"""A crew that makes no Claude calls must say so (the 2026-11-06 weekly report found $0 of calls for a whole week and
nothing on the Mac had flagged it). Failed calls are counted with their last error and logged to the engine log, the
day's usage is written after every huddle (not only at 15:05) with whether the crew was offline, the weekly report
counts each run's last row once, and the Manager panel shows a Crew Claude calls row."""
import asyncio
import logging
import sys
from datetime import date, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import health
from agentdesk.clock import at_ct
from agentdesk.journal import Journal
from test_crew_desks import CFG, D, make

OPEN = at_ct(date(2026, 10, 27), time(10, 0))        # a Tuesday, market hours
EVENING = at_ct(date(2026, 10, 27), time(18, 0))


def run(coro):
    return asyncio.run(coro)


class Boom:
    def __init__(self, ex):
        self.ex = ex

    async def create(self, **kw):
        raise self.ex


class Ok:
    async def create(self, **kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"headline": "ok", "lines": []}')],
                               stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))


def online(monkeypatch, messages, now=None):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    e, c = make(now=now or at_ct(D, time(8, 25)), sim=False)
    assert not c.offline
    c._client = SimpleNamespace(messages=messages)
    return e, c


# ---------------------------------------------------------------- crew.py
def test_a_failed_desk_call_is_counted_with_its_error_and_logged(monkeypatch, caplog):
    err = RuntimeError("Error code: 400 - Your credit balance is too low to access the Anthropic API.")
    e, c = online(monkeypatch, Boom(err))
    with caplog.at_level(logging.WARNING, logger="agentdesk.crew"):
        assert run(c._llm("macro", e.feed.now())) is None
    u = c.cache_usage
    assert u["calls"] == 0 and u["failed"] == 1 and u["by_desk"]["macro"]["failed"] == 1
    assert "credit balance is too low" in u["last_error"]["error"] and u["last_error"]["desk"] == "macro"
    assert u["last_error"]["ts"] == e.feed.now()
    assert any("credit balance is too low" in r.getMessage() for r in caplog.records)


def test_roundtable_and_calendar_failures_count_too(monkeypatch):
    e, c = online(monkeypatch, Boom(TimeoutError("timed out")))
    run(c._llm_roundtable("premarket", ["macro", "vol"], e.feed.now()))
    run(c._llm_calendar(e.feed.now(), D))
    assert c.cache_usage["failed"] == 2
    assert set(c.cache_usage["by_desk"]) == {"roundtable", "calendar"}


def test_a_good_call_after_a_failure_stamps_last_ok(monkeypatch):
    e, c = online(monkeypatch, Boom(RuntimeError("overloaded")))
    run(c._llm("macro", e.feed.now()))
    e.feed.t += 60
    c._client = SimpleNamespace(messages=Ok())
    run(c._llm("macro", e.feed.now()))
    assert c.cache_usage["calls"] == 1 and c.cache_usage["failed"] == 1
    assert c.cache_usage["last_ok"] == e.feed.now() > c.cache_usage["last_error"]["ts"]


def test_an_error_never_keeps_an_api_key():
    e, c = make(now=at_ct(D, time(8, 25)))
    c.note_failure("macro", RuntimeError("bad header x-api-key: sk-ant-api03-AbC_dEf-123 rejected"))
    assert "sk-ant" not in c.cache_usage["last_error"]["error"] and "AbC_dEf" not in c.cache_usage["last_error"]["error"]


def test_a_missing_anthropic_package_is_a_failure_not_silence(monkeypatch):
    import builtins
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    e, c = make(now=at_ct(D, time(8, 25)), sim=False)
    real = builtins.__import__

    def no_anthropic(name, *a, **kw):
        if name == "anthropic":
            raise ImportError("No module named 'anthropic'")
        return real(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", no_anthropic)
    assert run(c._llm("macro", e.feed.now())) is None
    assert c.cache_usage["failed"] == 1 and "anthropic" in c.cache_usage["last_error"]["error"]


def test_every_huddle_writes_the_days_usage_with_the_run_and_offline_flag(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    e, c = make(now=at_ct(D, time(11, 30)), sim=False)
    e.journal = Journal(None)
    assert c.offline
    run(c._consult("midday", ["macro"], at_ct(D, time(11, 30))))
    rows = [r for r in e.journal.crew_log(str(D)) if r["kind"] == "usage"]
    assert len(rows) == 1
    assert rows[0]["detail"]["offline"] is True and rows[0]["detail"]["run"] == c.run_id


def test_an_offline_crew_off_the_simulator_warns_at_startup(monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="agentdesk.crew"):
        make(sim=False)
    assert any("ANTHROPIC_API_KEY" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agentdesk.crew"):
        make(sim=True)                                  # the simulator is offline on purpose
    assert not caplog.records


def test_health_inputs_carry_the_crews_call_summary(monkeypatch):
    e, c = online(monkeypatch, Boom(RuntimeError("401 invalid x-api-key")))
    run(c._llm("macro", e.feed.now()))
    s = c.health()
    assert s["offline"] is False and s["calls"] == 0 and s["failed"] == 1
    assert "invalid x-api-key" in s["last_error"]["error"]


# ---------------------------------------------------------------- weekly report
def test_weekly_report_counts_each_runs_last_usage_row_once(tmp_path):
    from reporting import weekly_quant as wq
    path = tmp_path / "journal.db"
    j = Journal(path)
    day = "2026-09-28"
    j.record_crew(day, at_ct(D, time(8, 25)), "usage", None, {"run": 1, "est_cost_usd": 0.10, "web_searches": 2})
    j.record_crew(day, at_ct(D, time(11, 30)), "usage", None, {"run": 1, "est_cost_usd": 0.30, "web_searches": 5,
                                                                "calls": 6, "failed": 1})
    j.record_crew(day, at_ct(D, time(13, 15)), "usage", None, {"run": 2, "est_cost_usd": 0.05, "web_searches": 1,
                                                                "calls": 1, "failed": 3})   # a restart starts at 0
    j.record_crew("2026-09-29", at_ct(D, time(15, 5)) + 86400, "usage", None,
                  {"run": 3, "offline": True, "calls": 0, "failed": 0})
    j.record_crew("2026-09-30", at_ct(D, time(15, 5)) + 2 * 86400, "usage", None,
                  {"est_cost_usd": 0.20, "web_searches": 4})                                # old rows: no run id
    c = wq.build_report(path, date(2026, 10, 2))["crew"]["week"]
    assert abs(c["est_cost_usd"] - 0.55) < 1e-9 and c["web_searches"] == 10
    assert c["claude_calls"] == 7 and c["failed_calls"] == 4 and c["offline_sessions"] == 1
    md = wq.render_markdown(wq.build_report(path, date(2026, 10, 2)))
    assert "| Claude calls (failed) | 7 (4) |" in md and "| Sessions the crew ran offline | 1 |" in md


# ---------------------------------------------------------------- Manager panel row
def crew(**kw):
    return {"offline": False, "calls": 0, "failed": 0, "web_searches": 0, "est_cost_usd": 0.0, "last_ok": None,
            "last_error": None, **kw}


def test_healthy_crew_is_green_with_todays_calls():
    r = health.crew_calls(crew(calls=6, web_searches=9, est_cost_usd=0.41, last_ok=OPEN - 60), OPEN, CFG, True)
    assert r["state"] == "ok" and "6 calls" in r["detail"] and "$0.41" in r["detail"]


def test_offline_crew_is_red_with_the_env_fix():
    r = health.crew_calls(crew(offline=True), OPEN, CFG, True)
    assert r["state"] == "bad" and "ANTHROPIC_API_KEY" in r["detail"]
    assert "never" in r["fix"] and "install_paper.sh" in r["cmd"]


def test_failing_calls_are_red_with_the_last_error():
    err = {"ts": OPEN - 30, "desk": "macro", "error": "Error code: 400 - Your credit balance is too low"}
    r = health.crew_calls(crew(failed=3, last_error=err), OPEN, CFG, True)
    assert r["state"] == "bad" and "3 failed" in r["detail"] and "credit balance" in r["detail"]
    assert "console.anthropic.com" in r["fix"]


def test_a_failure_followed_by_good_calls_is_amber():
    err = {"ts": OPEN - 3600, "desk": "macro", "error": "overloaded"}
    r = health.crew_calls(crew(calls=4, failed=1, last_error=err, last_ok=OPEN - 60), OPEN, CFG, True)
    assert r["state"] == "warn"


def test_no_calls_by_mid_morning_is_amber():
    assert health.crew_calls(crew(), OPEN, CFG, True)["state"] == "warn"
    assert health.crew_calls(crew(), at_ct(date(2026, 10, 27), time(8, 0)), CFG, True)["state"] == "ok"


def test_crew_row_is_idle_when_the_engine_is_stopped_or_the_snapshot_predates_it():
    r = health.crew_calls(crew(calls=6), EVENING, CFG, False)
    assert r["state"] == "idle" and "6 calls" in r["detail"]
    assert health.crew_calls(None, OPEN, CFG, True)["state"] == "idle"


def test_the_report_includes_the_crew_row(tmp_path):
    paths = health.Paths(*(tmp_path / n for n in ("e.json", "r.json", "hb", "days.jsonl", "j.db")))
    snap = {"health": {"crew": crew(offline=True)}}
    rep = health.report(CFG, OPEN, snap, True, paths=paths, platform="linux")
    row = next(r for r in rep["checks"] if r["name"] == "Crew Claude calls")
    assert row["state"] == "bad" and rep["state"] == "bad"
