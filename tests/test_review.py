"""After-hours review server: read-only, serves the last saved session, and gives the port back to the engine."""
import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import claims
from agentdesk.archive import EventLog, SessionArchive
from agentdesk.review import ReviewSupervisor, create_review_app

BASE = "http://127.0.0.1:8765"


class Engine:
    def __init__(self, day, **extra):
        self.snap = {"type": "snapshot", "ts": 1.0, "day": day, "mode": "paper", **extra}

    def snapshot(self):
        return dict(self.snap)


def saved(tmp_path):
    d = tmp_path / "sessions"
    SessionArchive(d).save(Engine("2026-09-28", price=760.0))
    SessionArchive(d).save(Engine("2026-09-29", price=765.0, books={"f": {"scan": []}}))
    log = EventLog(d)
    log({"type": "log", "ts": 1790694000.0, "msg": "huddle"})      # 2026-09-29 10:00 CT
    log.close()
    return d


def client(d):
    return TestClient(create_review_app(d), base_url=BASE)


def test_page_and_review_config(tmp_path):
    c = client(saved(tmp_path))
    assert "AGENTDESK" in c.get("/").text
    js = c.get("/config.js")
    assert js.status_code == 200 and "source: 'review'" in js.text
    assert c.get("/static/app.js").status_code == 200


def test_page_config_and_assets_are_revalidated(tmp_path):
    """At 08:10 the same URL serves the live page again; nothing may come from a stale cache (M17)."""
    c = client(saved(tmp_path))
    assert c.get("/").headers["cache-control"] == "no-cache"
    assert c.get("/config.js").headers["cache-control"] == "no-store"
    for path in ("/static/app.js", "/static/book_f.js", "/static/styles.css"):
        assert c.get(path).headers["cache-control"] == "no-cache", path


def test_state_is_the_newest_saved_snapshot(tmp_path):
    c = client(saved(tmp_path))
    assert c.get("/api/state").json()["price"] == 765.0
    assert c.get("/api/state", params={"day": "2026-09-28"}).json()["price"] == 760.0
    assert c.get("/api/state", params={"day": "2026-01-01"}).status_code == 404


def test_review_payload(tmp_path):
    c = client(saved(tmp_path))
    r = c.get("/api/review").json()
    assert r["day"] == "2026-09-29" and r["days"] == ["2026-09-29", "2026-09-28"]
    assert r["snapshot"]["price"] == 765.0 and [e["msg"] for e in r["events"]] == ["huddle"]
    assert c.get("/api/review", params={"day": "2026-09-28"}).json()["events"] == []
    assert c.get("/api/review", params={"day": "../x"}).status_code == 404


def test_nothing_saved_yet(tmp_path):
    c = client(tmp_path / "empty")
    assert c.get("/api/review").status_code == 404
    assert c.get("/api/state").status_code == 404
    assert c.get("/").status_code == 200


def test_no_controls_at_all(tmp_path):
    c = client(saved(tmp_path))
    for path in ("/api/kill", "/api/flatten", "/api/pause", "/api/resume", "/api/proposals/p1/approve", "/api/state"):
        r = c.post(path, headers={"X-AgentDesk-Token": "anything"})
        assert r.status_code == 405, path


def test_other_hosts_refused(tmp_path):
    c = TestClient(create_review_app(saved(tmp_path)), base_url="http://evil.example:8765")
    assert c.get("/api/state").status_code == 403


def test_no_websocket(tmp_path):
    c = client(saved(tmp_path))
    try:
        with c.websocket_connect("/ws", headers={"origin": BASE}):
            assert False, "review server must not accept a websocket"
    except Exception:
        pass


# ------------------------------------------------------------------ supervisor

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(cond, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if await asyncio.to_thread(cond):
            return True
        await asyncio.sleep(0.05)
    return False


def serving(port):
    try:
        return httpx.get(f"http://127.0.0.1:{port}/config.js", timeout=0.5).status_code == 200
    except httpx.HTTPError:
        return False


def test_supervisor_steps_aside_for_a_claim_and_comes_back(tmp_path):
    async def main():
        port, cdir = free_port(), tmp_path / "claims"
        sup = ReviewSupervisor(create_review_app(saved(tmp_path)), "127.0.0.1", port, cdir, poll=0.05)
        stop = asyncio.Event()
        task = asyncio.create_task(sup.run(stop))
        assert await until(lambda: serving(port))
        mine = claims.claim(cdir)                              # the engine is starting
        assert await until(lambda: not claims.port_in_use("127.0.0.1", port))
        await asyncio.sleep(0.3)
        assert not claims.port_in_use("127.0.0.1", port)       # stays down while claimed
        claims.release(mine)                                   # the engine stopped
        assert await until(lambda: serving(port))
        stop.set()
        await asyncio.wait_for(task, 5)
        assert not claims.port_in_use("127.0.0.1", port)
    asyncio.run(main())


def test_supervisor_waits_while_someone_else_holds_the_port(tmp_path):
    async def main():
        port = free_port()
        other = socket.socket()
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        other.bind(("127.0.0.1", port))
        other.listen()                                          # e.g. an engine started by hand, before claims existed
        sup = ReviewSupervisor(create_review_app(saved(tmp_path)), "127.0.0.1", port, tmp_path / "claims", poll=0.05)
        stop = asyncio.Event()
        task = asyncio.create_task(sup.run(stop))
        await asyncio.sleep(0.4)
        assert not task.done()                                  # didn't crash on the busy port
        other.close()
        assert await until(lambda: serving(port))
        stop.set()
        await asyncio.wait_for(task, 5)
    asyncio.run(main())
