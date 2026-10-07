"""The Friday Quant LaunchAgent (com.agentdesk.quant) and tools/quant_week.sh, with stub commands and a fake clock."""
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from shell_stubs import DATE, SLEEP, clock_ct, set_clock, stub

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

PYSTUB = """#!/bin/sh
echo "$(cat "$FAKE_CLOCK") $PWD $*" >> "$CALLS"
case "$*" in *iex_vs_sip.py*) exit "${FAKE_SIP_RC:-0}" ;; esac
exit 0
"""


@pytest.fixture
def q(tmp_path):
    home = tmp_path / "home"
    src = home / ".agentdesk" / "paper-app" / "src"
    (src / "tools").mkdir(parents=True)
    (home / ".agentdesk" / "paper-app" / ".venv" / "bin").mkdir(parents=True)
    for f in ("quant_week.sh", "prune.sh", "prune_journal.py"):
        shutil.copy(TOOLS / f, src / "tools" / f)
    (src / ".env").write_text("ALPACA_API_KEY_ID=x\n")
    stub(home / ".agentdesk" / "paper-app" / ".venv" / "bin", "python", PYSTUB)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    stub(bin_, "date", DATE)
    stub(bin_, "sleep", SLEEP)
    env = {"HOME": str(home), "PATH": f"{bin_}:{os.environ['PATH']}", "FAKE_CLOCK": str(tmp_path / "clock"),
           "CALLS": str(tmp_path / "calls.log"), "LC_ALL": "C"}
    return {"home": home, "src": src, "env": env, "tmp": tmp_path}


def run(q, when: str, **extra):
    set_clock(q["env"]["FAKE_CLOCK"], when)
    r = subprocess.run(["bash", str(q["src"] / "tools" / "quant_week.sh")], capture_output=True, text=True,
                       env={**q["env"], **extra}, timeout=60)
    calls = q["tmp"] / "calls.log"
    log = q["home"] / ".agentdesk" / "reports" / f"quant-{when[:10]}.log"
    return r, (calls.read_text().splitlines() if calls.exists() else []), (log.read_text() if log.exists() else "")


def test_plist_runs_quant_week_fridays_at_16():
    pl = plistlib.loads((TOOLS / "launchd" / "com.agentdesk.quant.plist").read_bytes())
    assert pl["Label"] == "com.agentdesk.quant"
    assert pl["ProgramArguments"] == ["/bin/bash", "__APP__/src/tools/quant_week.sh"]
    assert pl["StartCalendarInterval"] == {"Weekday": 5, "Hour": 16, "Minute": 0}
    assert pl["RunAtLoad"] is False and pl["StandardErrorPath"].startswith("__HOME__/.agentdesk/reports/")


def test_every_plist_template_parses():
    """An invalid template (e.g. '--' inside an XML comment) fails here, before an install goes live."""
    files = sorted((TOOLS / "launchd").glob("*.plist"))
    assert len(files) >= 4
    for f in files:
        assert plistlib.loads(f.read_bytes())["Label"] == f.stem


def test_friday_runs_the_sip_replay_then_the_report(q):
    r, calls, log = run(q, "2026-10-09 16:00")
    assert r.returncode == 0, r.stderr
    ad, src = q["home"] / ".agentdesk", str(q["src"].resolve())
    sip, rep = calls[0].split(" ", 2)[2], calls[1].split(" ", 2)[2]
    assert calls[0].split()[1] == src
    assert sip == f"research/iex_vs_sip.py --days 5 --no-diag --env {src}/.env --out {ad}/reports/sip/2026-10-09"
    assert rep == (f"-m reporting.weekly_quant --journal {ad}/journal.db --out {ad}/reports --sip-dir {ad}/reports/sip "
                   f"--postmortem-dir {ad}/postmortems")
    assert len(calls) == 2 and "prune" not in log                    # the 9th: not the first Friday
    assert log.rstrip().endswith("quant_week: done")


def test_the_first_friday_also_prunes(q):
    r, calls, log = run(q, "2026-10-02 16:00")
    assert r.returncode == 0
    assert "tools/prune.sh" in log and any("prune_journal.py" in c and "--days 90" in c for c in calls)


def test_an_eastern_mac_waits_for_1515_central(q):
    r, calls, log = run(q, "2026-10-09 15:00")
    assert "waiting for 15:15 CT" in log
    assert clock_ct(float(calls[0].split()[0])).strftime("%H:%M") >= "15:15"


def test_no_keys_or_a_failed_replay_still_writes_the_report(q):
    r, calls, log = run(q, "2026-10-09 16:00", FAKE_SIP_RC="1")
    assert "SIP replay failed (code 1)" in log and "reporting.weekly_quant" in calls[-1]
    (q["src"] / ".env").unlink()
    (q["tmp"] / "calls.log").unlink()
    r, calls, log = run(q, "2026-10-09 16:00")
    assert "no .env" in log and len(calls) == 1 and "reporting.weekly_quant" in calls[0]


def test_the_cli_flags_exist_in_both_scripts():
    """quant_week.sh's flags must be ones research/iex_vs_sip.py and reporting/weekly_quant.py really accept."""
    s = (TOOLS / "quant_week.sh").read_text()
    for script, line in (("research/iex_vs_sip.py", r'"\$PY" research/iex_vs_sip\.py(.*?)>>'),
                         ("reporting/weekly_quant.py", r'"\$PY" -m reporting\.weekly_quant(.*?)>>')):
        used = set(re.findall(r"(--[a-z-]+)", re.search(line, s, re.S).group(1)))
        known = set(re.findall(r'add_argument\("(--[a-z-]+)"', (ROOT / script).read_text()))
        assert used and used <= known, (script, used - known)
