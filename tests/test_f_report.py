"""python -m agentdesk f-report (BOOK_F_HANDOFF section 4.6)."""
from __future__ import annotations

import json
import math
import sqlite3
import subprocess
import sys

import pytest

from f_fakes import CFG
from agentdesk.books import f_stocks_in_play as F
from agentdesk.books.f_journal import FJournal
from agentdesk.books.f_report import build_report, clustered_t, format_report


def pos(sym, sess, entry, stop, exit_px, rvol=3.0, news=None, n=[0]):
    n[0] += 1
    p = F.FPos(sym, 10, entry, stop, 1000.0 + n[0], atr=1.0, or_high=entry, rvol5=rvol, rank=1, news=news,
               id=f"{sym}-{sess}-{n[0]}")
    return p, sess, exit_px


def journal(rows):
    fj = FJournal(sqlite3.connect(":memory:"))
    for p, sess, px in rows:
        fj.open_position(sess, "paper", p)
        p.status, p.closed_ts, p.exit_px, p.exit_reason = "closed", p.opened_ts + 60, px, "exit 15:55 ET"
        fj.close_position(p)
    return fj


def test_clustered_t_one_per_cluster_is_the_plain_t():
    xs = [1.0, -0.5, 2.0, 0.3]
    m = sum(xs) / 4
    s = math.sqrt(sum((x - m) ** 2 for x in xs) / 3)
    assert clustered_t(xs, [1, 2, 3, 4]) == pytest.approx(m / (s / 2))


def test_report_numbers_and_splits():
    fj = journal([pos("NVDA", "2026-10-01", 100, 99, 102, rvol=5.5, news={"catalyst": "earnings", "priced_in": "early"}),
                  pos("JPM", "2026-10-01", 100, 99, 99, rvol=2.2, news={"catalyst": "none_found", "priced_in": "fully"}),
                  pos("MU", "2026-10-02", 100, 99, 99.5, rvol=3.5)])
    fj.record_shadow("2026-10-01", {"symbol": "AMD", "rvol5": 4.0, "rank": 1, "or_low": 99, "atr": 1.0, "entry_ts": 1,
                                    "entry": 99.0, "stop": 99.1, "qty": 10, "exit_ts": 2, "exit_px": 98.0,
                                    "exit_reason": "exit", "pnl": 10.0, "r": 10.0})
    r = build_report(fj)
    a = r["all"]
    assert a["trades"] == 3 and a["win_rate"] == pytest.approx(1 / 3)
    assert a["mean_r"] == pytest.approx((2 - 1 - 0.5) / 3) and a["pf"] == pytest.approx(20 / 15)
    assert a["worst_day"] == {"day": "2026-10-02", "pnl": -5.0}
    assert r["by_rvol5"]["5+"]["trades"] == 1 and r["by_rvol5"]["2-3"]["trades"] == 1
    assert r["by_priced_in"]["early"]["trades"] == 1 and r["by_priced_in"]["unknown"]["trades"] == 1
    assert r["by_catalyst"]["earnings"]["mean_r"] == pytest.approx(2.0)
    assert r["ai_list"]["trades"] == 2 and r["rest"]["trades"] == 1
    assert r["shadow_shorts"]["trades"] == 1 and r["shadow_shorts"]["mean_r"] == pytest.approx(10.0)
    txt = format_report(r)
    assert "3 trades" in txt and "shadow shorts" in txt.lower()


def test_report_since_filters_sessions():
    fj = journal([pos("NVDA", "2026-10-01", 100, 99, 102), pos("MU", "2026-10-02", 100, 99, 99.5)])
    assert build_report(fj, since="2026-10-02")["all"]["trades"] == 1


def test_empty_report_says_so():
    assert "No closed F trades" in format_report(build_report(FJournal(sqlite3.connect(":memory:"))))


def test_cli_f_report_runs_on_a_journal(tmp_path):
    db = tmp_path / "j.db"
    con = sqlite3.connect(db)
    fj = FJournal(con)
    p, sess, px = pos("NVDA", "2026-10-01", 100, 99, 102)
    fj.open_position(sess, "paper", p)
    p.status, p.closed_ts, p.exit_px, p.exit_reason = "closed", 2000.0, px, "exit 15:55 ET"
    fj.close_position(p)
    con.close()
    cfg = tmp_path / "c.yaml"
    import yaml
    from agentdesk.config import ROOT
    base = yaml.safe_load((ROOT / "config.yaml").read_text())
    base["journal_path"] = str(db)
    cfg.write_text(yaml.safe_dump(base))
    out = subprocess.run([sys.executable, "-m", "agentdesk", "--config", str(cfg), "f-report", "--since", "2026-09-01"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert "1 trades" in out.stdout


def test_cli_f_clear_stale_clears_only_earlier_sessions(tmp_path):
    db = tmp_path / "j.db"
    con = sqlite3.connect(db)
    fj = FJournal(con)
    old, _, _ = pos("NVDA", "2020-01-02", 100, 99, 0)
    fj.open_position("2020-01-02", "paper", old)
    con.close()
    import yaml
    from agentdesk.config import ROOT
    base = yaml.safe_load((ROOT / "config.yaml").read_text())
    base["journal_path"] = str(db)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump(base))
    out = subprocess.run([sys.executable, "-m", "agentdesk", "--config", str(cfg), "f-clear-stale"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert "Marked 1" in out.stdout
    assert FJournal(sqlite3.connect(db)).open_rows() == []
