"""The review LaunchAgent, its install, and paper_session.sh's port claim (static checks; launchd itself is Mac-only)."""
import plistlib
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"


def test_review_plist_runs_the_review_command_from_the_pinned_deploy():
    pl = plistlib.loads((TOOLS / "launchd" / "com.agentdesk.review.plist").read_bytes())
    assert pl["Label"] == "com.agentdesk.review"
    assert pl["ProgramArguments"] == ["__APP__/.venv/bin/python", "-m", "agentdesk", "--config",
                                      "__APP__/src/config.yaml", "review"]
    assert pl["KeepAlive"] is True and pl["RunAtLoad"] is True
    assert pl["StandardErrorPath"].startswith("__HOME__/.agentdesk/review/")


def test_install_paper_installs_review_and_verifies_both_jobs_loaded():
    s = (TOOLS / "install_paper.sh").read_text()
    lib = (TOOLS / "deploy_lib.sh").read_text()                   # load_job is shared with install_recorder.sh
    for f in ("install_paper.sh", "install_recorder.sh", "deploy_lib.sh", "claims.sh"):
        assert subprocess.run(["bash", "-n", str(TOOLS / f)]).returncode == 0, f
    assert "com.agentdesk.review" in s and "mkdir -p" in s and ".agentdesk/review" in s
    assert 'deploy_lib.sh"' in s and re.search(r"launchctl print", lib)   # load is verified, not assumed
    assert re.search(r"load_job .*paper", s) and re.search(r"load_job .*REVIEW", s)


def test_paper_session_claims_the_port_with_the_same_dir_as_config():
    s = (TOOLS / "paper_session.sh").read_text()
    claims_dir = yaml.safe_load((ROOT / "config.yaml").read_text())["review"]["claims_dir"]
    assert claims_dir == "~/.agentdesk/claims" and 'CLAIMS="$HOME/.agentdesk/claims"' in s
    claim, wait, check = s.index('"$CLAIMS/$$"'), s.index("waiting for the review page"), s.index("already in use")
    assert claim < wait < check                                   # claim, let the review page go, then the old check
    assert "trap" in s[claim - 200:claim + 200]
