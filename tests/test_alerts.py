"""Phone alerts (Evan, 2026-11-09/10: "imessage to myself is good"). A LaunchAgent checks the Manager panel every
5 min and texts Evan when a row turns amber or red, repeats red hourly in the day, says when it's fixed, and sends a
morning all-clear. Spec: docs/superpowers/specs/2026-11-10-phone-alerts-design.md.

No test here may run the real osascript: every send goes through a fake runner or sender."""
import copy
import http.server
import json
import logging
import subprocess
import sys
import threading

import pytest
from types import SimpleNamespace
from datetime import date, time

from agentdesk import alerts, health
from agentdesk.clock import at_ct
from agentdesk.config import load_config

CFG = load_config()
TUE = date(2026, 11, 10)


def at(hh, mm=0, d=TUE):
    return at_ct(d, time(hh, mm))


def rep(*rows):
    rows = list(rows)
    return {"checks": rows, "state": health.worst(rows)}


def sign_in(state, detail="signed in until Thu Nov 13 09:12 CT (2d 20h left)"):
    return health.row("Engine Robinhood sign-in", state, detail, "Sign in again before then: run this in Terminal.",
                      "cd ~/Trading-Agent && .venv/bin/python -m agentdesk rh-inspect")


def engine(state=health.OK, detail="running in paper mode"):
    return health.row("Engine", state, detail)


def cfg_with(**alerts_over):
    c = copy.deepcopy(CFG)
    c["alerts"] = {**(c.get("alerts") or {}), **alerts_over}
    return c


NO_MORNING = cfg_with(morning=False)


def run(prev, report, now, cfg=NO_MORNING):
    return alerts.diff(prev, report, now, cfg)


# ---------------------------------------------------------------- diff: transitions
def test_first_red_goes_at_any_hour():
    texts, st = run({}, rep(sign_in(health.BAD)), at(3))
    assert len(texts) == 1
    assert "🔴 Engine Robinhood sign-in" in texts[0] and "agentdesk rh-inspect" in texts[0]
    assert st["rows"]["Engine Robinhood sign-in"]["sent"] == "bad"


def test_red_repeats_hourly_only_in_the_day():
    _, st = run({}, rep(sign_in(health.BAD)), at(10))
    assert run(st, rep(sign_in(health.BAD)), at(10, 59))[0] == []
    texts, _ = run(st, rep(sign_in(health.BAD)), at(11))
    assert len(texts) == 1 and "still red" in texts[0]
    _, night = run({}, rep(sign_in(health.BAD)), at(2))
    assert run(night, rep(sign_in(health.BAD)), at(3, 30))[0] == []


def test_amber_waits_for_morning():
    texts, st = run({}, rep(sign_in(health.WARN)), at(22, d=date(2026, 11, 9)))
    assert texts == [] and "Engine Robinhood sign-in" not in st.get("rows", {})
    texts, st = run(st, rep(sign_in(health.WARN)), at(7))         # second amber check in a row, now in the day
    assert len(texts) == 1 and "🟠 Engine Robinhood sign-in" in texts[0]
    _, st = run({}, rep(sign_in(health.WARN)), at(22, d=date(2026, 11, 9)))
    texts, st = run(st, rep(sign_in(health.OK)), at(6))
    assert texts == []
    assert run(st, rep(sign_in(health.OK)), at(7))[0] == []


def test_red_then_green_overnight_sends_fixed_at_seven():
    _, st = run({}, rep(sign_in(health.BAD)), at(2))
    texts, st = run(st, rep(sign_in(health.OK)), at(5))
    assert texts == []
    texts, st = run(st, rep(sign_in(health.OK, "signed in until Fri Nov 20")), at(7))     # second green check
    assert len(texts) == 1 and "✅" in texts[0] and "fixed" in texts[0]
    assert "Engine Robinhood sign-in" not in st["rows"]


def test_red_to_amber_is_sent():
    _, st = run({}, rep(sign_in(health.BAD)), at(9))
    texts, st = run(st, rep(sign_in(health.WARN)), at(10))
    assert texts == []
    texts, st = run(st, rep(sign_in(health.WARN)), at(10, 5))
    assert len(texts) == 1 and "🟠" in texts[0]
    assert st["rows"]["Engine Robinhood sign-in"]["sent"] == "warn"


def test_wording_change_sends_nothing():
    _, st = run({}, rep(sign_in(health.WARN, "2d 20h left")), at(9))
    texts, st = run(st, rep(sign_in(health.WARN, "2d 20h left")), at(9, 5))
    assert len(texts) == 1
    assert run(st, rep(sign_in(health.WARN, "2d 19h left")), at(10))[0] == []


def test_idle_rows_never_alert():
    texts, st = run({}, rep(health.row("SPY prints", health.IDLE, "market closed"), engine(health.IDLE)), at(10))
    assert texts == [] and st.get("rows", {}) == {}


def test_missing_row_counts_as_green():
    dash = health.row("Dashboard", health.BAD, "neither the engine nor the review page answers on :8765")
    _, st = run({}, rep(dash, engine()), at(9))
    assert run(st, rep(engine()), at(10))[0] == []
    texts, st = run(run(st, rep(engine()), at(10))[1], rep(engine()), at(10, 5))
    assert len(texts) == 1 and "✅ Dashboard" in texts[0]
    assert "Dashboard" not in st["rows"]


def test_one_message_worst_first():
    _, st = run({}, rep(sign_in(health.WARN)), at(9, 55))
    texts, _ = run(st, rep(sign_in(health.WARN), engine(health.BAD, "not running during the session")), at(10))
    assert len(texts) == 1
    assert texts[0].index("🔴") < texts[0].index("🟠")


def test_no_secret_reaches_a_message():
    r = sign_in(health.BAD)
    r["token"] = "eyJ-secret"
    texts, _ = run({}, rep(r), at(10))
    assert texts and all("eyJ-secret" not in t for t in texts)


def _day(states, start=(9, 0), end=(15, 0), cfg=NO_MORNING):
    """Run diff every 5 min from start to end, cycling through `states` for the sign-in row; all texts."""
    st, out, t, i = {}, [], at(*start), 0
    while t < at(*end):
        texts, st = alerts.diff(st, rep(sign_in(states[i % len(states)])), t, cfg)
        out += texts
        t, i = t + 300, i + 1
    return out


def test_amber_needs_two_checks_in_a_row():
    _, st = run({}, rep(sign_in(health.WARN)), at(10))
    assert run(st, rep(sign_in(health.OK)), at(10, 5))[0] == []
    texts, st = run(st, rep(sign_in(health.WARN)), at(10, 5))
    assert len(texts) == 1 and "🟠" in texts[0]


def test_fixed_needs_two_green_checks_in_a_row():
    _, st = run({}, rep(sign_in(health.BAD)), at(10))
    texts, st = run(st, rep(sign_in(health.OK)), at(10, 5))
    assert texts == [] and "Engine Robinhood sign-in" in st["rows"]
    texts, st = run(st, rep(sign_in(health.OK)), at(10, 10))
    assert len(texts) == 1 and "✅" in texts[0]


def test_a_flapping_amber_row_stays_quiet():
    assert _day([health.WARN, health.OK]) == []                       # one rate limit every 10 minutes all day


def test_a_flapping_red_row_texts_at_most_three_times():
    texts = _day([health.BAD, health.OK])
    assert 1 <= len(texts) <= 3 and "✅" not in "".join(texts)


def test_red_repeats_are_capped_and_resume_next_morning():
    texts = _day([health.BAD], start=(10, 0), end=(21, 0))
    assert len(texts) == 3                                            # the first text and two hourly repeats
    st = {}
    for t in range(int(at(10)), int(at(21)), 300):
        _, st = run(st, rep(sign_in(health.BAD)), t)
    texts, _ = run(st, rep(sign_in(health.BAD)), at(7, d=date(2026, 11, 11)))
    assert len(texts) == 1 and "still red" in texts[0]


# ---------------------------------------------------------------- diff: the morning all-clear
def test_morning_all_clear_once_on_trading_days():
    texts, st = alerts.diff({}, rep(engine(), sign_in(health.OK)), at(8, 25), CFG)
    assert len(texts) == 1 and texts[0].startswith("AgentDesk 08:25: all green")
    assert "running in paper mode" in texts[0]
    assert alerts.diff(st, rep(engine()), at(8, 40), CFG)[0] == []
    assert alerts.diff({}, rep(engine()), at(8, 25, d=date(2026, 11, 14)), CFG)[0] == []    # Saturday
    assert alerts.diff({}, rep(engine()), at(8, 25, d=date(2026, 11, 26)), CFG)[0] == []    # Thanksgiving
    assert alerts.diff({}, rep(engine()), at(8, 10), CFG)[0] == []
    assert alerts.diff({}, rep(engine()), at(8, 25), NO_MORNING)[0] == []


def test_morning_lists_what_is_not_green():
    _, st = alerts.diff({}, rep(engine(), sign_in(health.WARN)), at(7, 55), CFG)
    _, st = alerts.diff(st, rep(engine(), sign_in(health.WARN)), at(8), CFG)     # the amber goes out at 08:00
    texts, _ = alerts.diff(st, rep(engine(), sign_in(health.WARN)), at(8, 25), CFG)
    assert len(texts) == 1
    assert "not all green" in texts[0] and "Engine Robinhood sign-in (warn)" in texts[0]


def test_settings_have_defaults_without_an_alerts_section():
    c = copy.deepcopy(CFG)
    c.pop("alerts", None)
    s = alerts.settings(c)
    assert s["day_start"] == "07:00" and s["day_end"] == "21:00" and s["repeat_bad_min"] == 60
    assert s["morning"] is True and s["morning_at"] == "08:20" and s["enabled"] is True


# ---------------------------------------------------------------- send: osascript
class Runner:
    """Records every command; returns `rc` for the iMessage call (the banner always succeeds)."""

    def __init__(self, rc=0, raises=None):
        self.calls, self.rc, self.raises = [], rc, raises

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if args[2] == alerts.IMESSAGE_SCRIPT:
            if self.raises:
                raise self.raises
            return SimpleNamespace(returncode=self.rc, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


FAKE_TO = "+15550000000"


def test_send_passes_handle_and_text_as_arguments():
    text = 'He said "hi"\nline 2 🔴 & end tell'
    r = Runner()
    assert alerts.send(text, FAKE_TO, runner=r) is True
    assert ["/usr/bin/osascript", "-e", alerts.IMESSAGE_SCRIPT, FAKE_TO, text] in r.calls
    assert ["/usr/bin/osascript", "-e", alerts.BANNER_SCRIPT, text] in r.calls
    for script in (alerts.IMESSAGE_SCRIPT, alerts.BANNER_SCRIPT):
        assert FAKE_TO not in script and "hi" not in script and "on run argv" in script


def test_send_failure_returns_false():
    assert alerts.send("x", FAKE_TO, runner=Runner(rc=1)) is False
    assert alerts.send("x", FAKE_TO, runner=Runner(raises=subprocess.TimeoutExpired("osascript", 30))) is False


def test_a_failed_imessage_logs_why_without_the_number(caplog):
    class Err(Runner):
        def __call__(self, args, **kw):
            r = super().__call__(args, **kw)
            if args[2] == alerts.IMESSAGE_SCRIPT:
                r.stderr = f'execution error: Can\'t get participant "{FAKE_TO}". (-1728)'
            return r
    assert alerts.send("x", FAKE_TO, runner=Err(rc=1)) is False
    assert "Can't get participant" in caplog.text and FAKE_TO not in caplog.text and "…0000" in caplog.text


def test_no_recipient_shows_banner_only():
    r = Runner()
    assert alerts.send("x", None, runner=r) is False
    assert r.calls == [["/usr/bin/osascript", "-e", alerts.BANNER_SCRIPT, "x"]]


def test_retry_without_banner():
    r = Runner()
    assert alerts.send("x", FAKE_TO, banner=False, runner=r) is True
    assert len(r.calls) == 1 and r.calls[0][2] == alerts.IMESSAGE_SCRIPT


def test_mask_shows_last_four():
    assert alerts.mask(FAKE_TO) == "…0000" and alerts.mask(None) == "(none)"


# ---------------------------------------------------------------- check: one run of the job
class Sender:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, text, to, banner=True):
        self.calls.append((text, to, banner))
        return self.ok


def ok_report(*_a, **_k):
    return rep(engine(), sign_in(health.OK))


def idle_local(cfg, now, snap, running):
    assert snap is None and running is False
    return rep(engine(health.IDLE, "not running"))


def checker(tmp_path, cfg=NO_MORNING, **kw):
    def go(now, **more):
        return alerts.check(cfg, now, state_path=tmp_path / "state.json", **{**kw, **more})
    return go


def test_first_run_sends_hello_once(tmp_path):
    s = Sender()
    go = checker(tmp_path, fetcher=ok_report, sender=s, to=FAKE_TO)
    assert go(at(10)) == [alerts.HELLO]
    assert go(at(10, 5)) == []
    assert [c[0] for c in s.calls] == [alerts.HELLO]


def test_corrupt_state_starts_fresh(tmp_path):
    (tmp_path / "state.json").write_text("{not json")
    assert checker(tmp_path, fetcher=ok_report, sender=Sender(), to=FAKE_TO)(at(10)) == [alerts.HELLO]
    assert json.loads((tmp_path / "state.json").read_text())["hello"]


def test_silent_port_waits_two_checks_then_alerts(tmp_path):
    go = checker(tmp_path, fetcher=lambda url: None, local=idle_local, sender=Sender(), to=FAKE_TO)
    first = go(at(10))
    assert not any("Dashboard" in t for t in first)
    second = go(at(10, 5))
    assert any("🔴 Dashboard" in t for t in second)
    assert not go(at(10, 20), fetcher=ok_report)
    back = go(at(10, 25), fetcher=ok_report)
    assert any("✅ Dashboard" in t for t in back)


def test_one_silent_check_texts_nothing_but_the_hello(tmp_path):
    def red_local(cfg, now, snap, running):
        return rep(engine(health.BAD, "not running during the session"))
    go = checker(tmp_path, fetcher=lambda url: None, local=red_local, sender=Sender(), to=FAKE_TO)
    assert go(at(10)) == [alerts.HELLO]                       # /api/health was slow once: no false Engine red
    second = go(at(10, 5))
    assert any("🔴 Engine" in t for t in second) and any("🔴 Dashboard" in t for t in second)


def test_the_health_url_uses_the_server_port(tmp_path):
    seen = []
    checker(tmp_path, fetcher=lambda url: seen.append(url) or ok_report(), sender=Sender(), to=FAKE_TO)(at(10))
    assert seen == [f"http://127.0.0.1:{CFG['server']['port']}/api/health"]


def _serve(body: bytes):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_bad_health_json_counts_as_silent():
    for body in (b"<html>", b'{"x": 1}', b"[1, 2]"):
        srv = _serve(body)
        try:
            assert alerts.fetch(f"http://127.0.0.1:{srv.server_port}/api/health") is None
        finally:
            srv.shutdown()
    srv = _serve(json.dumps(ok_report()).encode())
    try:
        assert alerts.fetch(f"http://127.0.0.1:{srv.server_port}/api/health")["checks"]
    finally:
        srv.shutdown()
    assert alerts.fetch("http://127.0.0.1:9/api/health", timeout=1) is None


def test_fetch_ignores_a_system_proxy(monkeypatch):
    srv = _serve(json.dumps(ok_report()).encode())
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    import urllib.request
    monkeypatch.setattr(urllib.request, "_opener", None)       # urlopen caches the proxies it saw first
    try:
        assert alerts.fetch(f"http://127.0.0.1:{srv.server_port}/api/health", timeout=2) is not None
    finally:
        srv.shutdown()


def test_failed_imessage_is_retried_then_dropped(tmp_path, caplog):
    s = Sender(ok=False)
    go = checker(tmp_path, fetcher=ok_report, sender=s, to=FAKE_TO)
    go(at(10))
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["pending"] == [{"text": alerts.HELLO, "tries": 1}]
    go(at(10, 5))
    assert json.loads((tmp_path / "state.json").read_text())["pending"] == [{"text": alerts.HELLO, "tries": 2}]
    go(at(10, 10))
    assert json.loads((tmp_path / "state.json").read_text())["pending"] == []
    go(at(10, 15))
    assert "dropped" in caplog.text
    hello_calls = [c for c in s.calls if c[0] == alerts.HELLO]
    assert len(hello_calls) == 3 and hello_calls[0][2] is True and all(c[2] is False for c in hello_calls[1:])


def test_no_recipient_warns_once_a_day_and_queues_nothing(tmp_path, caplog):
    go = checker(tmp_path, fetcher=ok_report, sender=Sender(ok=False), to=None)
    go(at(10))
    go(at(10, 5))
    assert caplog.text.count("ALERT_IMESSAGE_TO is not set") == 1
    assert json.loads((tmp_path / "state.json").read_text()).get("pending", []) == []


def test_disabled_does_nothing(tmp_path):
    called = []
    go = checker(tmp_path, cfg=cfg_with(enabled=False), fetcher=lambda url: called.append(url), sender=Sender())
    assert go(at(10)) == [] and called == []


def test_log_never_shows_the_full_number(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    go = checker(tmp_path, fetcher=lambda url: rep(sign_in(health.BAD)), sender=Sender(ok=False), to=FAKE_TO)
    for m in range(0, 25, 5):
        go(at(10, m))
    assert "0000" in caplog.text and FAKE_TO not in caplog.text


# ---------------------------------------------------------------- the CLI command
def _cfg_file(tmp_path):
    import yaml
    from agentdesk.config import ROOT
    data = yaml.safe_load((ROOT / "config.yaml").read_text())
    data["alerts"]["state_dir"] = str(tmp_path / "alerts")
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_cli_alerts_runs_one_check(tmp_path, monkeypatch):
    from agentdesk import __main__ as cli
    seen = {}
    monkeypatch.setattr(alerts, "check", lambda cfg, now, **kw: seen.update(kw) or [])
    monkeypatch.setenv("ALERT_IMESSAGE_TO", FAKE_TO)
    monkeypatch.setattr(sys, "argv", ["agentdesk", "--config", str(_cfg_file(tmp_path)), "alerts"])
    cli.main()
    assert seen["to"] == FAKE_TO
    assert (tmp_path / "alerts" / "alerts.log").exists()


def test_cli_alerts_test_exit_code(tmp_path, monkeypatch):
    from agentdesk import __main__ as cli
    sent = []
    monkeypatch.setattr(alerts, "send", lambda text, to, **kw: sent.append((text, to)) or False)
    monkeypatch.setenv("ALERT_IMESSAGE_TO", FAKE_TO)
    monkeypatch.setattr(sys, "argv", ["agentdesk", "--config", str(_cfg_file(tmp_path)), "alerts", "--test"])
    with pytest.raises(SystemExit) as ex:
        cli.main()
    assert ex.value.code == 1 and sent == [(alerts.HELLO, FAKE_TO)]


def test_tests_never_see_a_real_recipient():
    import os
    assert "ALERT_IMESSAGE_TO" not in os.environ          # conftest scrubs it, so no test can text Evan
