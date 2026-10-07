"""Dashboard controls need the per-run token; other sites (CSRF) and DNS-rebinding hosts are refused."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.bus import Bus
from agentdesk.server import create_app, resolve_token

TOKEN = "t0ken-for-tests"
BASE = "http://127.0.0.1:8765"


class Risk:
    class st:
        halted, halt_reason = False, None


class Engine:
    crew = None
    risk = Risk()

    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"type": "snapshot"}

    async def kill(self):
        self.calls.append("kill")

    async def flatten(self):
        self.calls.append("flatten")

    def pause(self, on):
        self.calls.append(("pause", on))


@pytest.fixture
def env():
    e = Engine()
    return e, TestClient(create_app(e, Bus(), token=TOKEN), base_url=BASE)


@pytest.mark.parametrize("path", ["/api/kill", "/api/flatten", "/api/pause", "/api/resume",
                                  "/api/proposals/p1/approve"])
def test_controls_refuse_requests_without_the_token(env, path):
    e, c = env
    r = c.post(path)
    assert r.status_code == 401 and r.json()["ok"] is False
    assert c.post(path, headers={"X-AgentDesk-Token": "wrong"}).status_code == 401
    assert e.calls == []


def test_controls_work_with_the_token(env):
    e, c = env
    for path in ("/api/kill", "/api/flatten", "/api/pause"):
        assert c.post(path, headers={"X-AgentDesk-Token": TOKEN}).json() == {"ok": True}
    assert e.calls == ["kill", "flatten", ("pause", True)]


def test_cross_site_post_is_refused_even_with_the_token(env):
    e, c = env
    r = c.post("/api/kill", headers={"X-AgentDesk-Token": TOKEN, "Origin": "https://evil.example"})
    assert r.status_code == 403 and e.calls == []
    ok = c.post("/api/kill", headers={"X-AgentDesk-Token": TOKEN, "Origin": BASE})
    assert ok.status_code == 200


def test_dns_rebinding_host_is_refused(env):
    e, c = env
    assert c.get("/api/state", headers={"Host": "attacker.example:8765"}).status_code == 403
    assert c.post("/api/kill", headers={"Host": "attacker.example:8765", "X-AgentDesk-Token": TOKEN}).status_code == 403
    assert e.calls == []
    assert c.get("/api/state", headers={"Host": "localhost:8765"}).status_code == 200


def test_read_only_state_needs_no_token(env):
    _, c = env
    assert c.get("/api/state").json()["type"] == "snapshot"


def test_websocket_from_another_site_is_refused(env):
    _, c = env
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("ws://127.0.0.1:8765/ws", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_text()
    with c.websocket_connect("ws://127.0.0.1:8765/ws", headers={"Origin": BASE}) as ws:
        assert '"snapshot"' in ws.receive_text()


def test_token_is_never_served_to_the_page(env):
    _, c = env
    for path in ("/", "/config.js", "/api/state"):
        assert TOKEN not in c.get(path).text


def test_non_ascii_token_header_is_refused_not_an_error(env):
    e, c = env
    r = c.post("/api/kill", headers={"X-AgentDesk-Token": "t0ken-é".encode("utf-8")})
    assert r.status_code == 401 and r.json()["ok"] is False and e.calls == []


def test_page_config_and_assets_are_revalidated(env):
    """A bookmark opened after hours must not run a cached live config.js against the review server (M17)."""
    _, c = env
    assert c.get("/").headers["cache-control"] == "no-cache"
    assert c.get("/config.js").headers["cache-control"] == "no-store"
    for path in ("/static/app.js", "/static/styles.css", "/static/panel.js"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path


def test_state_serialises_odd_values_once(env):
    from datetime import date
    e, _ = env
    calls = []
    e.snapshot = lambda: calls.append(1) or {"type": "snapshot", "day": date(2026, 10, 6), "px": 765.25}
    c = TestClient(create_app(e, Bus(), token=TOKEN), base_url=BASE)
    r = c.get("/api/state")
    assert r.json() == {"type": "snapshot", "day": "2026-10-06", "px": 765.25} and len(calls) == 1
    assert r.headers["content-type"].startswith("application/json")


def test_token_source_order(monkeypatch):
    monkeypatch.delenv("AGENTDESK_TOKEN", raising=False)
    a, b = resolve_token({}), resolve_token({})
    assert a != b and len(a) >= 20                       # generated fresh each run
    assert resolve_token({"token": "from-config"}) == "from-config"
    monkeypatch.setenv("AGENTDESK_TOKEN", "from-env")
    assert resolve_token({"token": "from-config"}) == "from-env"
