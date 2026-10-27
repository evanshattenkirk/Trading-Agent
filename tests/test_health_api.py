"""The Manager panel's data: the engine's snapshot carries its Robinhood call stats, sign-in verdict and last SPY
print, and both the engine and the after-hours review server answer GET /api/health without the control token,
with nothing secret in the body (2026-10-27)."""
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import health
from agentdesk.archive import SessionArchive
from agentdesk.bars import Trade
from agentdesk.bus import Bus
from agentdesk.review import create_review_app
from agentdesk.server import create_app

from test_safety import CFG, NOW, engine

SECRET = "eyJhbGciOi-access-token-value"
BASE = "http://127.0.0.1:8765"


class RH:
    def stats(self):
        return {"last_ok": 123.0, "last_err": None, "window": {"ok": 5}, "day": {"ok": 5, "reconnects": 1}}

    def sign_in_status(self):
        return True, "signed in until Fri Oct 30 09:12 CT"


def test_the_snapshot_carries_the_engine_health_inputs():
    e = engine()
    e.l2_rh = RH()
    h = e.snapshot()["health"]
    assert h["rh"]["day"]["reconnects"] == 1
    assert h["rh_sign_in"] == [True, "signed in until Fri Oct 30 09:12 CT"]
    assert h["feed"] == "test"


def test_the_last_print_is_stamped_by_the_engine_for_any_feed():
    """Every feed (Alpaca, Massive) reaches the engine through the same trade path, so the engine stamps the wall
    clock there; MassiveFeed has no last_print of its own (Codex, PR #60)."""
    e = engine()
    assert not hasattr(e.feed, "last_print")
    before = time.time()
    asyncio.run(e.on_item(Trade(NOW, 600.0)))
    assert e.snapshot()["health"]["last_print"] >= before


def test_without_robinhood_the_health_inputs_are_empty():
    h = engine().snapshot()["health"]
    assert h["rh"] is None and h["rh_sign_in"] is None and h["last_print"] is None


def test_the_risk_payload_says_whose_halt_it_is():
    e = engine()
    e.risk.halt("daily loss limit -$400", scope="A")
    assert e.risk.to_dict()["halt_scope"] == "A"


def fake_paths(tmp_path, monkeypatch):
    tok = tmp_path / "rh_oauth.json"
    tok.write_text(json.dumps({"tokens": {"access_token": SECRET, "expires_in": 9.6 * 86400},
                               "saved_at": time.time() - 86400}))
    paths = health.Paths(engine_token=tok, recorder_token=tok, heartbeat=tmp_path / "hb",
                         days_log=tmp_path / "days.jsonl", journal=tmp_path / "journal.db")
    monkeypatch.setattr(health.Paths, "from_cfg", classmethod(lambda cls, cfg: paths))


def test_the_engine_serves_health_without_the_token(tmp_path, monkeypatch):
    fake_paths(tmp_path, monkeypatch)
    e = engine()
    e.l2_rh = RH()
    c = TestClient(create_app(e, Bus(), token="t0ken"), base_url=BASE)
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True and len(body["checks"]) == 9
    assert body["checks"][0]["state"] == "ok"
    assert SECRET not in r.text and "t0ken" not in r.text
    assert r.headers["cache-control"].startswith("no-")


def test_the_review_server_serves_health_from_the_saved_session(tmp_path, monkeypatch):
    fake_paths(tmp_path, monkeypatch)
    e = engine()
    e.l2_rh = RH()
    e.day = date(2026, 10, 26)
    d = tmp_path / "sessions"
    assert SessionArchive(d).save(e)
    c = TestClient(create_review_app(d, cfg=CFG), base_url=BASE)
    body = c.get("/api/health").json()
    assert body["running"] is False
    rows = {r["name"]: r for r in body["checks"]}
    assert rows["Engine Robinhood calls"]["state"] == "idle"
    assert "last session 2026-10-26" in rows["Engine"]["detail"]
    assert c.post("/api/health").status_code == 405


def test_the_review_server_serves_health_with_no_saved_session(tmp_path, monkeypatch):
    fake_paths(tmp_path, monkeypatch)
    c = TestClient(create_review_app(tmp_path / "none", cfg=CFG), base_url=BASE)
    r = c.get("/api/health")
    assert r.status_code == 200 and len(r.json()["checks"]) == 9


def test_the_simulator_has_no_manager_panel():
    e = engine()
    e.mode = "sim"
    c = TestClient(create_app(e, Bus(), token="t0ken"), base_url=BASE)
    assert c.get("/api/health").status_code == 404
