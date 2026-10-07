"""Unattended Robinhood sign-in (2026-10-07): the engine's token expired at 14:02 CT and every reconnect opened a
Robinhood sign-in tab (about 67 by 15:10). Unattended runs (the launchd paper engine, the recorder daemon) never open
a browser: they fail closed with one "sign-in expired: run <cmd>" line, and only rh-inspect and record-quotes --once
may open one sign-in page per process. The refresh_token grant never ran because the MCP SDK keeps a token's expiry
only in memory: after the daily restart it sent the expired token, got a 401 and went straight to the browser."""
import asyncio
import copy
import json
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.brokers import robinhood as R
from agentdesk.brokers.base import SignInRequired
from agentdesk.brokers.robinhood import FileTokenStorage, RobinhoodMCP

from test_rh_signin import CFG, Session, Transport, client, free_port

URL = "https://robinhood.example/oauth/authorize?client_id=x&state=y"


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    """Sign-out memory and the one-page budget are per process; every test starts as a new process."""
    monkeypatch.setattr(RobinhoodMCP, "_refused", {})
    monkeypatch.setattr(RobinhoodMCP, "_browser_opened", False)
    opened = []
    monkeypatch.setattr(R.webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    return opened


def counting_transport(rh, entered):
    async def enter():                      # what the SDK does when Robinhood refuses the saved token
        entered.append(1)
        await rh._redirect(URL)
    return lambda: Transport(enter)


# ----------------------------------------------------------------------------- never a browser when unattended
def test_unattended_redirect_never_opens_a_browser_or_binds_the_callback_port(tmp_path, fresh_process, caplog):
    rh = client(tmp_path)
    told = []
    rh.on_signed_out = told.append

    async def go():
        for _ in range(3):
            with pytest.raises(SignInRequired):
                await rh._redirect(URL)
    with caplog.at_level(logging.INFO, logger="agentdesk.robinhood"):
        asyncio.run(go())
    assert fresh_process == []                                              # no tab
    assert getattr(rh._cb, "server", None) is None                         # no callback server waiting
    lines = [r for r in caplog.records if "sign-in expired" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.ERROR            # one clear line, not one per retry
    assert "rh-inspect" in lines[0].getMessage()
    assert told == [lines[0].getMessage()]                                  # the engine hears it once
    assert URL not in caplog.text                                           # the sign-in URL isn't logged either


def test_a_refused_sign_in_fails_fast_and_never_reconnects(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "CONNECT_TIMEOUT_S", 30.0)
    entered = []
    rh = client(tmp_path)
    rh._connect = (counting_transport(rh, entered), lambda r, w: Session())

    async def go():
        t0 = time.monotonic()
        with pytest.raises(SignInRequired):
            await rh.start()
        first = time.monotonic() - t0
        for _ in range(5):                                  # the engine's pollers keep calling
            with pytest.raises(SignInRequired):
                await rh.call("get_accounts", {}) if rh.tools else await rh.start()
        return first
    assert asyncio.run(go()) < 5.0                          # not the connect timeout, not the 330 s browser wait
    assert entered == [1]                                   # one try; the next calls fail without a reconnect


def test_a_new_client_on_the_same_refused_token_fails_without_connecting(tmp_path, caplog):
    entered = []
    first = client(tmp_path)
    first._connect = (counting_transport(first, entered), lambda r, w: Session())
    with pytest.raises(SignInRequired):
        asyncio.run(first.start())
    second = client(tmp_path)                               # the recorder makes a new client after 3 failed polls
    second._connect = (counting_transport(second, entered), lambda r, w: Session())
    told = []
    second.on_signed_out = told.append
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="agentdesk.robinhood"):
        with pytest.raises(SignInRequired, match="rh-inspect"):
            asyncio.run(second.start())
    assert entered == [1] and len(told) == 1
    assert "sign-in expired" not in caplog.text             # logged once per process, by the first client


def test_a_fresh_sign_in_on_disk_lets_it_connect_again(tmp_path):
    entered = []
    rh = client(tmp_path)
    rh._connect = (counting_transport(rh, entered), lambda r, w: Session())
    with pytest.raises(SignInRequired):
        asyncio.run(rh.start())
    token = tmp_path / "rh_oauth.json"
    token.write_text(json.dumps({"tokens": {"access_token": "new", "token_type": "Bearer"}}))   # rh-inspect signed in
    os.utime(token, (time.time() + 5, time.time() + 5))
    rh._transport = lambda: (lambda: Transport(), lambda r, w: Session())
    asyncio.run(rh.start())                                 # picks up the new token: connects, loads the account
    assert rh.signed_out() is None and rh.tools


# ----------------------------------------------------------------------------- interactive commands: one page
def test_interactive_sign_in_opens_one_page_per_process(tmp_path, fresh_process, capsys):
    a = client(tmp_path)
    a.interactive = True
    b = client(tmp_path / "b")
    b.interactive = True

    async def go():
        await a._redirect(URL)
        await a._redirect(URL + "&retry=1")
        a._cb.close()
        await b._redirect(URL + "&other=1")
        b._cb.close()
    asyncio.run(go())
    assert fresh_process == [URL]
    assert (URL + "&retry=1") in capsys.readouterr().out    # later tries still print their URL


def test_only_rh_inspect_and_record_once_are_interactive(monkeypatch, tmp_path):
    from agentdesk import __main__ as M
    from agentdesk import recorder
    made = []

    class Fake(RobinhoodMCP):
        def __init__(self, cfg, *a, **kw):
            made.append(kw.get("interactive", False))
            raise SystemExit(0)
    monkeypatch.setattr(R, "RobinhoodMCP", Fake)
    with pytest.raises(SystemExit):
        asyncio.run(R.inspect_main(CFG, str(tmp_path / "x.json")))
    assert made == [True]
    cfg = copy.deepcopy(CFG)
    cfg["journal_path"] = str(tmp_path / "j.db")
    monkeypatch.setattr(recorder, "RECORDER_DIR", tmp_path)
    assert recorder.RecorderDaemon(cfg).interactive is False                # the launchd daemon
    assert recorder.RecorderDaemon(cfg, interactive=True).interactive is True
    m = recorder.MeteredRobinhoodMCP(recorder.recorder_cfg(cfg, recorder.settings(cfg)),
                                     recorder.CallMeter(tmp_path / "j.db"))
    assert m.interactive is False and "record-quotes --once" in m.sign_in_msg
    assert RobinhoodMCP(CFG).interactive is False                           # the engine, backtest, iv-snapshot


# ----------------------------------------------------------------------------- the refresh that never ran
def write_token(path: Path, *, age_s: float, life_s: float = 830_000, refresh="r1", meta=True, saved_at=True):
    d = {"tokens": {"access_token": "old", "token_type": "Bearer", "expires_in": int(life_s), "refresh_token": refresh},
         "client": {"client_id": "cid", "redirect_uris": ["http://localhost:8766/callback"],
                    "token_endpoint_auth_method": "none"}}
    if saved_at:
        d["saved_at"] = time.time() - age_s
    if meta:
        d["oauth_metadata"] = {"issuer": "https://auth.example/", "authorization_endpoint": "https://auth.example/authorize",
                               "token_endpoint": "https://auth.example/oauth/token"}
    path.write_text(json.dumps(d))
    if not saved_at:
        os.utime(path, (time.time() - age_s, time.time() - age_s))


def server(seen, token_status=200):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, str(req.url), req.headers.get("authorization")))
        if req.url.path == "/oauth/token":
            form = parse_qs(req.content.decode())
            assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["r1"]
            if token_status != 200:
                return httpx.Response(token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "new", "token_type": "Bearer", "expires_in": 830000,
                                             "refresh_token": "r2"})
        if req.url.path == "/mcp/trading":
            ok = req.headers.get("authorization") in ("Bearer new", "Bearer old")
            return httpx.Response(200, json={"ok": True}) if ok else httpx.Response(401)
        return httpx.Response(404)
    return handler


def provider_for(tmp_path, monkeypatch):
    rh = RobinhoodMCP({**CFG, "robinhood": {**CFG["robinhood"], "call_budget": None, "token_dir": str(tmp_path),
                                            "mcp_url": "https://rh.example/mcp/trading", "redirect_port": free_port()}})
    rh._transport()
    return rh, rh._provider


async def post(provider, handler):
    async with httpx.AsyncClient(auth=provider, transport=httpx.MockTransport(handler)) as c:
        return await c.post("https://rh.example/mcp/trading", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})


def test_a_restart_refreshes_a_token_with_under_a_day_left_instead_of_opening_a_browser(tmp_path, monkeypatch,
                                                                                          fresh_process):
    write_token(tmp_path / "rh_oauth.json", age_s=830_000 - 6 * 3600)        # 6 hours left at the 08:10 start
    rh, provider = provider_for(tmp_path, monkeypatch)
    seen = []
    r = asyncio.run(post(provider, server(seen)))
    assert r.status_code == 200 and fresh_process == []
    assert seen[0][1] == "https://auth.example/oauth/token"                 # the saved token endpoint, refreshed first
    assert seen[-1][2] == "Bearer new"
    d = json.loads((tmp_path / "rh_oauth.json").read_text())
    assert d["tokens"]["access_token"] == "new" and d["tokens"]["refresh_token"] == "r2"
    assert abs(d["saved_at"] - time.time()) < 60
    assert d["oauth_metadata"]["token_endpoint"] == "https://auth.example/oauth/token"     # kept for the next refresh


def test_a_token_with_days_left_is_used_as_is(tmp_path, monkeypatch):
    write_token(tmp_path / "rh_oauth.json", age_s=2 * 86400)
    rh, provider = provider_for(tmp_path, monkeypatch)
    seen = []
    assert asyncio.run(post(provider, server(seen))).status_code == 200
    assert [s[1] for s in seen] == ["https://rh.example/mcp/trading"] and seen[0][2] == "Bearer old"


def test_a_token_file_from_before_this_fix_dates_itself_by_its_mtime(tmp_path, monkeypatch):
    write_token(tmp_path / "rh_oauth.json", age_s=830_000 - 3600, saved_at=False)
    assert FileTokenStorage(tmp_path / "rh_oauth.json").expires_at() == pytest.approx(time.time() + 3600, abs=60)
    rh, provider = provider_for(tmp_path, monkeypatch)
    seen = []
    assert asyncio.run(post(provider, server(seen))).status_code == 200
    assert seen[0][1] == "https://auth.example/oauth/token"


def test_a_failed_refresh_keeps_the_still_valid_token_and_warns_with_the_deadline(tmp_path, monkeypatch, caplog,
                                                                                   fresh_process):
    write_token(tmp_path / "rh_oauth.json", age_s=830_000 - 6 * 3600)
    rh, provider = provider_for(tmp_path, monkeypatch)
    warned = []
    rh.on_token_warning = warned.append
    seen = []
    with caplog.at_level(logging.WARNING, logger="agentdesk.robinhood"):
        r = asyncio.run(post(provider, server(seen, token_status=400)))
        r2 = asyncio.run(post(provider, server(seen, token_status=400)))
    assert r.status_code == 200 and r2.status_code == 200 and fresh_process == []
    assert [s[1] for s in seen].count("https://auth.example/oauth/token") == 1    # tried once, not on every call
    assert seen[-1][2] == "Bearer old"
    assert len(warned) == 1 and "refresh failed" in warned[0] and "rh-inspect" in warned[0]
    assert rh.sign_in_status()[0] is False                                 # Ops shows it red before the open


def test_a_refused_refresh_on_an_expired_token_fails_closed(tmp_path, monkeypatch, fresh_process):
    write_token(tmp_path / "rh_oauth.json", age_s=830_000 + 60)               # already expired
    rh, provider = provider_for(tmp_path, monkeypatch)
    seen = []
    with pytest.raises(SignInRequired):
        asyncio.run(post(provider, server(seen, token_status=400)))
    assert fresh_process == [] and rh.signed_out()
    assert rh.sign_in_status() == (False, rh.signed_out())


def test_sign_in_status_reports_the_token_deadline(tmp_path, monkeypatch):
    write_token(tmp_path / "rh_oauth.json", age_s=86400)
    rh, _ = provider_for(tmp_path, monkeypatch)
    ok, detail = rh.sign_in_status()
    assert ok is True and "signed in until" in detail


def test_the_sdk_hooks_this_relies_on_still_exist():
    """mcp is pinned <2 (requirements.txt); if an upgrade renames these, the refresh fix silently stops working."""
    from mcp.client.auth import OAuthClientProvider
    for name in ("_initialize", "_handle_token_response", "_handle_refresh_response"):
        assert callable(getattr(OAuthClientProvider, name, None)), name
    ctx = OAuthClientProvider.__init__.__code__.co_varnames
    assert "redirect_handler" in ctx and "storage" in ctx


# ----------------------------------------------------------------------------- the engine fails closed
MSG = "Robinhood sign-in expired: run `x` in Terminal"


def sim_engine():
    from agentdesk.__main__ import build
    eng, bus = build(copy.deepcopy(CFG), "sim", 0, 21)
    got = []
    bus.taps.append(got.append)
    return eng, got


def errors(got):
    return [m["msg"] for m in got if m.get("type") == "log" and m.get("level") == "error"]


def test_a_sign_in_expiry_mid_session_halts_and_flattens_every_book_but_not_sticky():
    async def go():
        eng, got = sim_engine()
        eng._booted = True
        eng.robinhood_signed_out(MSG)
        eng.robinhood_signed_out(MSG)                       # every failing poller reports it; one halt
        return eng, got
    eng, got = asyncio.run(go())
    st = eng.risk.st
    assert st.halted and st.halt_scope == "account" and st.flatten_all and not st.halt_sticky
    assert MSG in st.halt_reason and st.sticky_halt is None                 # a restart after signing in starts clean
    assert sum(MSG in m for m in errors(got)) == 1


def test_a_sign_in_expiry_before_the_open_halts_without_selling_held_positions():
    async def go():
        eng, got = sim_engine()
        eng.robinhood_signed_out(MSG)                       # during broker.start(), before restore()
        halted_early = eng.risk.st.halted
        eng._apply_startup_sign_in(eng.feed.now())
        return eng, got, halted_early
    eng, got, halted_early = asyncio.run(go())
    st = eng.risk.st
    assert not halted_early                                 # restore() would overwrite it, so it waits
    assert st.halted and st.halt_scope == "account" and not st.flatten_all and not st.halt_sticky
    assert sum(MSG in m for m in errors(got)) == 1


def test_a_signed_out_poll_is_not_counted_as_a_broker_error():
    async def poll():
        raise SignInRequired(MSG)

    async def go():
        eng, got = sim_engine()
        eng._booted = True
        for _ in range(5):
            await eng._guard(poll(), "manage")
        return eng, got
    eng, got = asyncio.run(go())
    assert eng._errors == 0 and eng.risk.st.halted and not eng.risk.st.halt_sticky
    assert "broker/API errors" not in (eng.risk.st.halt_reason or "")
    assert not any("failed (" in m for m in errors(got))     # no error line per poll


def test_build_wires_the_robinhood_sign_out_to_the_engine(monkeypatch, tmp_path):
    from agentdesk import __main__ as M
    from agentdesk.brokers import robinhood

    made = []

    class FakeRH:
        budget = None

        def __init__(self, cfg):
            self.on_signed_out = None
            made.append(self)

        async def close(self):
            pass
    monkeypatch.setattr(robinhood, "RobinhoodMCP", FakeRH)
    cfg = copy.deepcopy(CFG)
    cfg["data"]["provider"] = "sim"
    cfg["data"]["quotes_source"] = "robinhood"
    cfg["journal_path"] = str(tmp_path / "j.db")
    cfg["risk"]["state_path"] = str(tmp_path / "risk_{mode}.json")
    eng, bus = M.build(cfg, "paper", 0, 21)
    assert [rh.on_signed_out for rh in made] == [eng.robinhood_signed_out]


def test_ops_shows_the_sign_in_line(tmp_path):
    from test_crew_desks import make
    e, c = make(sim=False, tmp=tmp_path)
    e.l2_rh = SimpleNamespace(sign_in_status=lambda: (False, "Robinhood sign-in expired: run `x` in Terminal"))
    ch = next(ch for ch in c._ops_brief("premarket")["checks"] if ch["name"] == "Robinhood")
    assert ch["ok"] is False and "sign-in expired" in ch["detail"]
    e.l2_rh = SimpleNamespace(sign_in_status=lambda: (True, "signed in until Oct 17 14:02 CT"))
    ch = next(ch for ch in c._ops_brief("premarket")["checks"] if ch["name"] == "Robinhood")
    assert ch["ok"] is True and "until" in ch["detail"]


def test_the_sdk_browser_flow_on_a_refused_token_fails_closed_unattended(tmp_path, monkeypatch, fresh_process):
    """End to end through the SDK: a token Robinhood answers 401 to, with no refresh token, reaches the redirect
    handler, which raises instead of opening a page."""
    write_token(tmp_path / "rh_oauth.json", age_s=3600, refresh=None)
    rh, provider = provider_for(tmp_path, monkeypatch)
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(401) if req.url.path == "/mcp/trading" else httpx.Response(404)
    with pytest.raises(SignInRequired, match="rh-inspect"):
        asyncio.run(post(provider, handler))
    assert fresh_process == [] and rh.signed_out()
    assert getattr(rh._cb, "server", None) is None
