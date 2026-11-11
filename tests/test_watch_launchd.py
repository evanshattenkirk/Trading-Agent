"""The phone-alerts LaunchAgent (com.agentdesk.watch) and its install (static checks; launchd itself is Mac-only).
Spec: docs/superpowers/specs/2026-11-10-phone-alerts-design.md."""
import plistlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"


def test_watch_plist_runs_alerts_every_five_minutes():
    pl = plistlib.loads((TOOLS / "launchd" / "com.agentdesk.watch.plist").read_bytes())
    assert pl["Label"] == "com.agentdesk.watch"
    assert pl["ProgramArguments"] == ["__APP__/.venv/bin/python", "-m", "agentdesk", "--config",
                                      "__APP__/src/config.yaml", "alerts"]
    assert pl["WorkingDirectory"] == "__APP__/src"
    assert pl["StartInterval"] == 300 and pl["RunAtLoad"] is True and "KeepAlive" not in pl
    assert pl["StandardErrorPath"].startswith("__HOME__/.agentdesk/alerts/")
    assert pl["StandardOutPath"].startswith("__HOME__/.agentdesk/alerts/")


def test_install_paper_loads_the_watch_job_and_resends_hello():
    s = (TOOLS / "install_paper.sh").read_text()
    assert subprocess.run(["bash", "-n", str(TOOLS / "install_paper.sh")]).returncode == 0
    assert "com.agentdesk.watch" in s and ".agentdesk/alerts" in s and "hello" in s and "ALERT_IMESSAGE_TO" in s
    m = re.search(r"load_job .*WATCH", s)
    assert m
    running_block_end = s.index("fi", s.index('if [ -n "$RUNNING" ]'))
    assert m.start() > running_block_end          # loaded even while an engine runs: it never touches the engine
    assert s.index("hello") < m.start()           # the hello is reset before the job's first run
