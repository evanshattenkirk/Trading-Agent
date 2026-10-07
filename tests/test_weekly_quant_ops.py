"""The weekly Quant report's ops inputs: the Post-mortem desk's daily files (HANDOFF section 5 says they feed the
report) and the yearly quote archives tools/prune.sh moves old journal rows into."""
import json
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

from reporting import weekly_quant as wq

CT = ZoneInfo("America/Chicago")

PM_CLEAN = """# Post-mortem 2026-09-29

All 2 trades followed the rules.

| Contract | Setup | Qty | Open | Close | P&L % | Peak % | Exit | Rule breaks |
|---|---|---|---|---|---|---|---|---|
| SPY 765C | SWING | 2 | 09:05 | 09:40 | +12% | +30% | tp | none |
| SPY 766C | SCALP | 1 | 10:05 | 10:20 | -8% | +5% | stop | none |
"""
PM_BREAK = """# Post-mortem 2026-10-01

1 rule break(s) in 1 trades. Check the engine: lost 60%, stop is -35%

| Contract | Setup | Qty | Open | Close | P&L % | Peak % | Exit | Rule breaks |
|---|---|---|---|---|---|---|---|---|
| SPY 764C | SWING | 1 | 09:12 | 11:02 | -60% | +55% | stop | lost 60%, stop is -35% |

Review:
- SPY 764C: round trip: peaked +55%, closed -60%

Skipped entries:
- RSI above cap: 3
"""


def write_pm(d, day, text):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.md").write_text(text)


def test_postmortems_of_the_week_are_read(tmp_path):
    d = tmp_path / "pm"
    write_pm(d, "2026-09-25", PM_CLEAN.replace("2026-09-29", "2026-09-25"))       # prior week: left out
    write_pm(d, "2026-09-29", PM_CLEAN)
    write_pm(d, "2026-10-01", PM_BREAK)
    (d / "notes.txt").write_text("not a post-mortem")
    pm = wq.load_postmortems(d, "2026-09-28", "2026-10-02")
    assert [x["day"] for x in pm["days"]] == ["2026-09-29", "2026-10-01"]
    clean, brk = pm["days"]
    assert clean["headline"] == "All 2 trades followed the rules." and clean["trades"] == 2 and clean["breaks"] == []
    assert brk["trades"] == 1 and brk["breaks"] == ["SPY 764C: lost 60%, stop is -35%"]
    assert brk["reviews"] == ["SPY 764C: round trip: peaked +55%, closed -60%"]


def test_postmortem_section_renders_in_the_report(tmp_path):
    db = tmp_path / "journal.db"
    sqlite3.connect(db).close()
    d = tmp_path / "pm"
    write_pm(d, "2026-09-29", PM_CLEAN)
    write_pm(d, "2026-10-01", PM_BREAK)
    out = tmp_path / "reports"
    assert wq.main(["--journal", str(db), "--week-ending", "2026-10-02", "--out", str(out),
                    "--postmortem-dir", str(d)]) == 0
    md = (out / "quant-2026-10-02.md").read_text()
    assert "## Post-mortem audits" in md
    assert "| 2026-10-01 | 1 | 1 rule break(s) in 1 trades. Check the engine: lost 60%, stop is -35% |" in md
    assert "- 2026-10-01 SPY 764C: lost 60%, stop is -35%" in md and "round trip" in md
    assert md.index("## Post-mortem audits") < md.index("## Notes")
    rep = json.loads((out / "quant-2026-10-02.json").read_text())
    assert len(rep["postmortems"]["days"]) == 2


def test_a_week_without_postmortems_says_so(tmp_path):
    db = tmp_path / "journal.db"
    sqlite3.connect(db).close()
    out = tmp_path / "reports"
    assert wq.main(["--journal", str(db), "--week-ending", "2026-10-02", "--out", str(out),
                    "--postmortem-dir", str(tmp_path / "none")]) == 0
    assert "No post-mortem files for this week" in (out / "quant-2026-10-02.md").read_text()
