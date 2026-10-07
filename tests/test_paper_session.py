"""tools/paper_session.sh's guards, driven with stub commands and a fake clock (launchd itself is Mac-only).

The script is zsh on the Mac (what the plist runs); it keeps to what bash also runs, so these tests drive it with
bash here and with zsh too wherever zsh is installed (the Mac, when tools/install_paper.sh runs the suite).
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import claims

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
CT = ZoneInfo("America/Chicago")
SHELLS = ["bash"] + (["zsh"] if shutil.which("zsh") else [])

DATE = f"""#!{sys.executable}
import datetime, os, sys, zoneinfo
now = float(open(os.environ["FAKE_CLOCK"]).read())
tz = os.environ.get("TZ") or os.environ.get("FAKE_SYSTEM_TZ", "America/Chicago")
fmt = next((a[1:] for a in sys.argv[1:] if a.startswith("+")), "%a %b %d %H:%M:%S %Z %Y")
print(datetime.datetime.fromtimestamp(now, zoneinfo.ZoneInfo(tz)).strftime(fmt))
"""
SLEEP = f"""#!{sys.executable}
import os, sys
p = os.environ["FAKE_CLOCK"]
now = float(open(p).read()) + float(sys.argv[1] if len(sys.argv) > 1 else 0)
open(p, "w").write(repr(now))
"""
ENGINE = """#!/bin/sh
if [ "$1" = "-c" ]; then exec "{py}" "$@"; fi
echo "$(cat "$FAKE_CLOCK") claims=$(ls "$HOME/.agentdesk/claims" | tr '\\n' ',') $*" >> "$ENGINE_LOG"
exit 0
"""


def stub(d: Path, name: str, body: str) -> None:
    f = d / name
    f.write_text(body)
    f.chmod(0o755)


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    app = home / ".agentdesk" / "paper-app"
    (app / "src" / "tools").mkdir(parents=True)
    (app / ".venv" / "bin").mkdir(parents=True)
    for f in ("paper_session.sh", "claims.sh"):
        shutil.copy(TOOLS / f, app / "src" / "tools" / f)
    shutil.copy(ROOT / "config.yaml", app / "src" / "config.yaml")
    (app / "DEPLOYED").write_text("abc1234\n2026-10-07T20:00:00Z\n")
    stub(app / ".venv" / "bin", "python", ENGINE.format(py=sys.executable))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    stub(bin_, "date", DATE)
    stub(bin_, "sleep", SLEEP)
    stub(bin_, "caffeinate", "#!/bin/sh\nexit 0\n")
    stub(bin_, "lsof", '#!/bin/sh\nexit "${FAKE_LSOF:-1}"\n')
    stub(bin_, "readlink", '#!/bin/sh\necho "${FAKE_LOCALTIME:-/var/db/timezone/zoneinfo/America/Chicago}"\n')
    e = {"HOME": str(home), "PATH": f"{bin_}:{os.environ['PATH']}", "FAKE_CLOCK": str(tmp_path / "clock"),
         "ENGINE_LOG": str(tmp_path / "engine.log"), "LC_ALL": "C"}
    e.pop("TZ", None)
    return {"home": home, "app": app, "env": e, "tmp": tmp_path}


def run(env, shell, when: str, **extra):
    """Runs the script at `when` (Central time, 'YYYY-MM-DD HH:MM'); returns (result, day log, engine calls)."""
    Path(env["env"]["FAKE_CLOCK"]).write_text(repr(datetime.fromisoformat(when).replace(tzinfo=CT).timestamp()))
    r = subprocess.run([shell, str(env["app"] / "src" / "tools" / "paper_session.sh")], capture_output=True,
                       text=True, env={**env["env"], **extra}, timeout=120)
    day = when[:10]
    log = env["home"] / ".agentdesk" / "paper" / f"{day}.log"
    eng = env["tmp"] / "engine.log"
    return r, (log.read_text() if log.exists() else None), (eng.read_text().splitlines() if eng.exists() else [])


@pytest.mark.parametrize("shell", SHELLS)
def test_a_normal_weekday_starts_the_paper_engine_and_releases_its_claim(env, shell):
    r, log, eng = run(env, shell, "2026-10-07 08:10")
    assert r.returncode == 0, r.stderr
    assert "deployed abc1234; starting paper engine" in log and "WARN" not in log
    assert eng and all("run --mode paper --no-browser" in c for c in eng)
    assert len(eng) == 6                                     # the stub engine exits at once: 1 start + 5 restarts
    first = eng[0].split()
    assert first[1].startswith("claims=") and first[1] != "claims="       # the port was claimed while it ran
    assert "done for the day" in log
    assert list((env["home"] / ".agentdesk" / "claims").iterdir()) == []  # and released at exit


@pytest.mark.parametrize("shell", SHELLS)
def test_weekends_and_night_logins_exit_quietly(env, shell):
    for when in ("2026-10-10 08:10", "2026-10-11 12:00", "2026-10-07 03:00"):    # Sat, Sun, a weekday at 03:00
        r, log, eng = run(env, shell, when)
        assert r.returncode == 0 and log is None and eng == [], (when, r.stdout, r.stderr)
        assert "paper_session" in r.stdout                   # one line in launchd.out.log, no day log


@pytest.mark.parametrize("shell", SHELLS)
def test_after_15_ct_it_does_not_start(env, shell):
    r, log, eng = run(env, shell, "2026-10-07 15:00")
    assert r.returncode == 0 and eng == [] and "after 15:00 CT" in log


@pytest.mark.parametrize("shell", SHELLS)
def test_a_config_holiday_is_skipped(env, shell):
    hol = yaml.safe_load((ROOT / "config.yaml").read_text())["calendar"]["holidays"]
    day = str(next(d for d in hol if d.weekday() < 5))
    r, log, eng = run(env, shell, f"{day} 08:10")
    assert r.returncode == 0 and eng == [] and "market holiday" in log


@pytest.mark.parametrize("shell", SHELLS)
def test_the_holiday_list_comes_from_the_deployed_config(env, shell):
    cfg = env["app"] / "src" / "config.yaml"
    data = yaml.safe_load(cfg.read_text())
    data["calendar"]["holidays"].append(datetime(2026, 10, 7).date())       # an ordinary Wednesday, made a holiday
    cfg.write_text(yaml.safe_dump(data))
    r, log, eng = run(env, shell, "2026-10-07 08:10")
    assert r.returncode == 0 and eng == [] and "market holiday" in log


def test_config_calendar_matches_the_nyse_rules_through_2028():
    """calendar.holidays and calendar.early_close vs research/nyse_calendar.py, from October 2026 to the end of 2028."""
    import importlib.util
    from datetime import date
    spec = importlib.util.spec_from_file_location("nyse_calendar_for_test", ROOT / "research" / "nyse_calendar.py")
    nyse = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nyse)
    cal = yaml.safe_load((ROOT / "config.yaml").read_text())["calendar"]
    lo, hi = date(2026, 10, 1), date(2028, 12, 31)
    want_h = sorted(d for d in nyse.holidays_between(2026, 2028) if lo <= d <= hi)
    want_e = sorted(d for d in nyse.half_days_between(2026, 2028) if lo <= d <= hi)
    assert sorted(cal["holidays"]) == want_h
    assert sorted(cal["early_close"]) == want_e
    assert not set(cal["holidays"]) & set(cal["early_close"])
    assert date(2027, 7, 2) not in cal["early_close"]           # July 4 2027 is a Sunday: a full day on Friday the 2nd
    assert date(2028, 1, 17) in cal["holidays"] and date(2028, 7, 3) in cal["early_close"]


def test_paper_plist_also_runs_at_login():
    import plistlib
    pl = plistlib.loads((TOOLS / "launchd" / "com.agentdesk.paper.plist").read_bytes())
    assert pl["RunAtLoad"] is True                              # a reboot or an off Mac at 08:10 still gets the day
    assert "KeepAlive" not in pl                                # one run a day; the script decides whether to start
    assert all(d["Hour"] == 8 and d["Minute"] == 10 for d in pl["StartCalendarInterval"])


def test_the_script_keeps_no_holiday_list_of_its_own():
    s = (TOOLS / "paper_session.sh").read_text()
    assert not re.search(r"20\d\d-\d\d-\d\d", s)
    assert "calendar" in s and "holidays" in s


@pytest.mark.parametrize("shell", SHELLS)
def test_an_eastern_mac_warns_and_waits_for_0810_central(env, shell):
    r, log, eng = run(env, shell, "2026-10-07 07:50", FAKE_SYSTEM_TZ="America/New_York",
                      FAKE_LOCALTIME="/var/db/timezone/zoneinfo/America/New_York")
    assert r.returncode == 0, r.stderr
    assert ("WARN: launchd runs StartCalendarInterval in the system time zone (America/New_York); "
            "set System Settings > General > Date & Time to Central") in log
    assert "waiting for 08:10 CT" in log
    started = datetime.fromtimestamp(float(eng[0].split()[0]), CT)
    assert started.strftime("%H:%M") >= "08:10"


@pytest.mark.parametrize("shell", SHELLS)
def test_a_running_engine_is_left_alone(env, shell):
    claims.claim(env["home"] / ".agentdesk" / "claims")          # this test process plays the running engine
    r, log, eng = run(env, shell, "2026-10-07 08:10")
    assert r.returncode == 0 and eng == [] and f"pid {os.getpid()}" in log
    assert sorted(p.name for p in (env["home"] / ".agentdesk" / "claims").iterdir()) == [str(os.getpid())]


@pytest.mark.parametrize("shell", SHELLS)
def test_a_stale_claim_from_a_reused_pid_does_not_block_the_day(env, shell):
    d = env["home"] / ".agentdesk" / "claims"
    d.mkdir(parents=True)
    (d / "1").write_text("Mon Jan  1 00:00:00 2001\n")
    r, log, eng = run(env, shell, "2026-10-07 08:10")
    assert r.returncode == 0 and len(eng) == 6


@pytest.mark.parametrize("shell", SHELLS)
def test_a_port_held_by_something_else_stops_the_start(env, shell):
    r, log, eng = run(env, shell, "2026-10-07 08:10", FAKE_LSOF="0")
    assert r.returncode == 1 and eng == [] and "port 8765 already in use" in log
    assert list((env["home"] / ".agentdesk" / "claims").iterdir()) == []


def test_paper_session_writes_its_claim_with_the_shared_helper():
    """tools/claims.sh writes the format agentdesk/claims.py reads (tests/test_claims.py checks both directions)."""
    s = (TOOLS / "paper_session.sh").read_text()
    assert 'write_claim "$CLAIMS" $$' in s and '. "$(dirname "$0")/claims.sh"' in s
