"""tools/install_paper.sh and tools/install_recorder.sh, run end to end against a fake checkout with stub commands
(uv, rsync, git, launchctl, plutil, lsof, sleep) and a temporary HOME. launchd itself is Mac-only; every launchctl
call goes to a stub that logs it, so these tests never touch the real jobs.
"""
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import claims

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

UV = f"""#!{PY}
import os, sys
args = sys.argv[1:]
open(os.environ["UV_LOG"], "a").write(" ".join(args) + "\\n")
if args[:1] == ["venv"]:
    venv = args[-1]
    os.makedirs(os.path.join(venv, "bin"), exist_ok=True)
    open(os.path.join(venv, "pyvenv.cfg"), "w").write("home = /usr/bin\\n")
    py = os.path.join(venv, "bin", "python")
    open(py, "w").write('#!/bin/sh\\n'
                        'if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then pwd >> "$PYTEST_LOG"; exit "${{FAKE_PYTEST_RC:-0}}"; fi\\n'
                        'exec "{PY}" "$@"\\n')
    os.chmod(py, 0o755)
sys.exit(int(os.environ.get("FAKE_UV_RC", "0")) if args[:1] == ["pip"] else 0)
"""
RSYNC = f"""#!{PY}
import os, shutil, sys
args, srcs, i = sys.argv[1:], [], 0
while i < len(args):
    if args[i] == "--exclude":
        i += 2
        continue
    if not args[i].startswith("-"):
        srcs.append(args[i])
    i += 1
dest = srcs.pop()
skip = shutil.ignore_patterns("__pycache__", ".venv", ".git")
for s in srcs:
    s = s.rstrip("/")
    t = os.path.join(dest, os.path.basename(s))
    if os.path.isdir(s):
        shutil.copytree(s, t, ignore=skip, dirs_exist_ok=True)
        shutil.rmtree(os.path.join(t, "data"), ignore_errors=True) if os.path.basename(s) == "research" else None
    else:
        shutil.copy2(s, t)
"""
GIT = """#!/bin/sh
case "$*" in
  *rev-parse*) [ -n "${FAKE_NOT_GIT:-}" ] && exit 128; exit 0 ;;
  *untracked-files=no*) printf '%s' "${FAKE_GIT_TRACKED:-}" ;;
  *untracked-files=all*) printf '%s' "${FAKE_GIT_UNTRACKED:-}" ;;
  *describe*) echo "v1-2-gabc1234" ;;
esac
exit 0
"""
LAUNCHCTL = """#!/bin/sh
echo "$*" >> "$LAUNCHCTL_LOG"
if [ "$1" = print ]; then
  case "$2" in *com.agentdesk.paper) [ -n "${FAKE_PAPER_STATE:-}" ] && echo "	state = $FAKE_PAPER_STATE" ;; esac
  exit "${FAKE_PRINT_RC:-0}"
fi
exit 0
"""
LSOF = """#!/bin/sh
[ -n "${FAKE_LSOF_PIDS:-}" ] || exit 1
echo "$FAKE_LSOF_PIDS"
"""


def stub(d: Path, name: str, body: str) -> None:
    (d / name).write_text(body)
    (d / name).chmod(0o755)


@pytest.fixture
def box(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "tools", repo / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("config.yaml", "requirements.txt"):
        shutil.copy(ROOT / f, repo / f)
    for d in ("agentdesk", "reporting", "research", "tests"):
        (repo / d).mkdir()
        (repo / d / "__init__.py").write_text("")
    (repo / "research" / "data").mkdir()
    (repo / "research" / "data" / "big.csv").write_text("x")
    (repo / ".env").write_text("ALPACA_API_KEY_ID=x\n")
    home = tmp_path / "home"
    home.mkdir()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, body in (("uv", UV), ("rsync", RSYNC), ("git", GIT), ("launchctl", LAUNCHCTL), ("lsof", LSOF),
                       ("plutil", "#!/bin/sh\nexit 0\n"), ("sleep", "#!/bin/sh\nexit 0\n")):
        stub(bin_, name, body)
    env = {"HOME": str(home), "PATH": f"{bin_}:/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C",
           "UV_LOG": str(tmp_path / "uv.log"), "PYTEST_LOG": str(tmp_path / "pytest.log"),
           "LAUNCHCTL_LOG": str(tmp_path / "launchctl.log")}
    return {"repo": repo, "home": home, "tmp": tmp_path, "env": env,
            "app": home / ".agentdesk" / "paper-app", "rec": home / ".agentdesk" / "recorder-app"}


def install(box, *args, script="install_paper.sh", **extra):
    return subprocess.run(["bash", str(box["repo"] / "tools" / script), *args], capture_output=True, text=True,
                          env={**box["env"], **extra}, timeout=120)


def read(box, name):
    p = box["tmp"] / name
    return p.read_text() if p.exists() else ""


def releases(app):
    return sorted(p.name for p in (app / "releases").iterdir()) if (app / "releases").exists() else []


def test_a_fresh_install_builds_tests_and_links_a_release(box):
    r = install(box)
    assert r.returncode == 0, r.stdout + r.stderr
    app = box["app"]
    rel = (app / "current").resolve()
    assert rel.parent == (app / "releases").resolve()
    for x in ("src", ".venv", "DEPLOYED"):
        assert (app / x).is_symlink() and (app / x).resolve() == rel / x
    assert (app / "DEPLOYED").read_text().splitlines()[0] == "v1-2-gabc1234"
    assert read(box, "pytest.log").strip() == str(rel / "src")              # tests ran inside the new release
    assert (rel / "src" / ".env").stat().st_mode & 0o777 == 0o600
    assert "pip install --quiet --python" in read(box, "uv.log") and "requirements.txt" in read(box, "uv.log")
    la = read(box, "launchctl.log")
    assert "bootstrap" in la and "com.agentdesk.paper" in la and "com.agentdesk.review" in la
    pl = plistlib.loads((box["home"] / "Library" / "LaunchAgents" / "com.agentdesk.paper.plist").read_bytes())
    assert Path(pl["ProgramArguments"][1]).is_file()                       # .../paper-app/src/tools/paper_session.sh
    rv = plistlib.loads((box["home"] / "Library" / "LaunchAgents" / "com.agentdesk.review.plist").read_bytes())
    assert Path(rv["ProgramArguments"][0]).exists() and Path(rv["WorkingDirectory"]).is_dir()
    assert "Not deployed" not in r.stdout


def test_the_release_carries_the_docs_a_test_may_read(box):
    """The suite runs inside the release, so a test that opens README.md, docs/ or the CI workflow
    (tests/test_deps_and_ci.py) must find them there too."""
    (box["repo"] / "README.md").write_text("readme\n")
    (box["repo"] / "docs" / "superpowers").mkdir(parents=True)
    (box["repo"] / "docs" / "superpowers" / "spec.md").write_text("spec\n")
    (box["repo"] / ".github" / "workflows").mkdir(parents=True)
    (box["repo"] / ".github" / "workflows" / "tests.yml").write_text("name: tests\n")
    assert install(box).returncode == 0
    src = (box["app"] / "current").resolve() / "src"
    assert (src / "README.md").is_file() and (src / "docs" / "superpowers" / "spec.md").is_file()
    assert (src / ".github" / "workflows" / "tests.yml").is_file()
    assert not (src / "research" / "data").exists()                       # caches stay in the checkout


def test_a_lock_file_installs_with_uv_pip_sync(box):
    (box["repo"] / "requirements.lock").write_text("pyyaml==6.0.2\n")
    assert install(box).returncode == 0
    log = read(box, "uv.log")
    assert "pip sync --quiet --python" in log and log.rstrip().endswith("requirements.lock")
    assert "pip install" not in log


def test_failing_tests_leave_the_live_release_untouched(box):
    assert install(box).returncode == 0
    live = (box["app"] / "current").resolve()
    before = releases(box["app"])
    (box["tmp"] / "launchctl.log").unlink()
    r = install(box, FAKE_PYTEST_RC="1")
    assert r.returncode != 0
    assert "Tests failed." in r.stdout and "Not deployed; the live release is unchanged." in r.stdout
    assert (box["app"] / "current").resolve() == live and releases(box["app"]) == before
    assert "boot" not in read(box, "launchctl.log")                          # nothing reloaded (only looked at)


def test_a_failed_dependency_install_is_not_deployed(box):
    r = install(box, FAKE_UV_RC="1")
    assert r.returncode != 0 and "Not deployed" in r.stdout
    assert not (box["app"] / "current").exists() and releases(box["app"]) == []


def test_the_old_layout_moves_into_a_release_and_keeps_its_state(box):
    app = box["app"]
    (app / "src").mkdir(parents=True)
    (app / ".venv" / "bin").mkdir(parents=True)
    (app / "src" / "overrides.yaml").write_text("exits.stop_loss_pct: 0.2\n")
    (app / "src" / ".env").write_text("OLD=1\n")
    (app / "DEPLOYED").write_text("old\n")
    (box["repo"] / ".env").unlink()                                          # no .env in the checkout: keep the live one
    r = install(box)
    assert r.returncode == 0, r.stdout + r.stderr
    rel = (app / "current").resolve()
    assert (app / "src").is_symlink() and (app / "src" / "overrides.yaml").read_text() == "exits.stop_loss_pct: 0.2\n"
    assert (rel / "src" / ".env").read_text() == "OLD=1\n"
    old = app / "releases" / "00000000T000000Z-before-releases"
    assert (old / "src" / "overrides.yaml").exists() and (old / ".venv").is_dir() and (old / "DEPLOYED").exists()


def test_the_checkout_env_replaces_the_live_one(box):
    assert install(box).returncode == 0
    (box["repo"] / ".env").write_text("NEW=2\n")
    assert install(box).returncode == 0
    assert (box["app"] / "src" / ".env").read_text() == "NEW=2\n"


def test_only_the_current_release_and_two_others_are_kept(box):
    rels = box["app"] / "releases"
    for name in ("20260901T000000Z-1", "20260902T000000Z-1", "20260903T000000Z-1"):
        (rels / name / "src").mkdir(parents=True)
    assert install(box).returncode == 0
    cur = (box["app"] / "current").resolve().name
    assert releases(box["app"]) == ["20260902T000000Z-1", "20260903T000000Z-1", cur]


def test_a_live_claim_refuses_the_install(box):
    claims.claim(box["home"] / ".agentdesk" / "claims")                      # this test process plays the engine
    r = install(box)
    assert r.returncode == 1
    assert f"An engine is running: pid {os.getpid()} holds a dashboard claim" in r.stdout and "Not deployed" in r.stdout
    assert not (box["app"] / "releases").exists() and read(box, "launchctl.log").count("bootout") == 0


def test_a_stale_claim_does_not_refuse(box):
    d = box["home"] / ".agentdesk" / "claims"
    d.mkdir(parents=True)
    (d / "1").write_text("Mon Jan  1 00:00:00 2001\n")
    assert install(box).returncode == 0


def test_the_paper_job_running_refuses_the_install(box):
    r = install(box, FAKE_PAPER_STATE="running")
    assert r.returncode == 1 and "com.agentdesk.paper is running" in r.stdout and "Not deployed" in r.stdout
    r = install(box, FAKE_PAPER_STATE="not running")
    assert r.returncode == 0, r.stdout


def test_the_port_refuses_unless_the_review_page_holds_it(box):
    other = subprocess.Popen([PY, "-c", "import time; time.sleep(30)", "agentdesk", "run"])
    review = subprocess.Popen([PY, "-c", "import time; time.sleep(30)", "-m", "agentdesk", "--config", "c.yaml", "review"])
    try:
        time.sleep(0.2)
        r = install(box, FAKE_LSOF_PIDS=str(other.pid))
        assert r.returncode == 1 and f"port 8765 is in use by pid {other.pid}" in r.stdout
        r = install(box, FAKE_LSOF_PIDS=str(review.pid))
        assert r.returncode == 0, r.stdout
    finally:
        for p in (other, review):
            p.kill()
            p.wait()


def test_force_deploys_but_leaves_the_running_engine_alone(box):
    claims.claim(box["home"] / ".agentdesk" / "claims")
    r = install(box, "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOT RELOADED: com.agentdesk.paper" in r.stdout and "keeps the code it started with" in r.stdout
    la = read(box, "launchctl.log").splitlines()
    assert not [l for l in la if "com.agentdesk.paper" in l and l.split()[0] in ("bootout", "bootstrap")]
    assert [l for l in la if l.startswith("bootstrap") and "com.agentdesk.review" in l]   # review is reloaded
    assert (box["app"] / "current").exists()


def test_a_dirty_checkout_needs_dirty(box):
    r = install(box, FAKE_GIT_TRACKED=" M agentdesk/engine.py\n")
    assert r.returncode == 1 and "--dirty" in r.stdout and "Not deployed" in r.stdout
    assert not (box["app"] / "releases").exists()
    r = install(box, FAKE_GIT_UNTRACKED="?? agentdesk/new.py\n")
    assert r.returncode == 1
    r = install(box, "--dirty", FAKE_GIT_TRACKED=" M agentdesk/engine.py\n")
    assert r.returncode == 0
    assert (box["app"] / "DEPLOYED").read_text().splitlines()[0] == "v1-2-gabc1234-dirty"


def test_the_quant_job_is_installed_by_default_and_no_quant_removes_it(box):
    assert install(box).returncode == 0
    qp = box["home"] / "Library" / "LaunchAgents" / "com.agentdesk.quant.plist"
    pl = plistlib.loads(qp.read_bytes())
    assert Path(pl["ProgramArguments"][1]).is_file()                       # .../paper-app/src/tools/quant_week.sh
    assert "bootstrap" in read(box, "launchctl.log") and "com.agentdesk.quant" in read(box, "launchctl.log")
    assert (box["home"] / ".agentdesk" / "reports").is_dir()
    r = install(box, "--no-quant")
    assert r.returncode == 0 and "Removed com.agentdesk.quant (--no-quant)." in r.stdout and not qp.exists()


def test_no_load_deploys_without_launchctl(box):
    r = install(box, "--no-load")
    assert r.returncode == 0 and "Not loaded (--no-load)." in r.stdout
    assert "bootstrap" not in read(box, "launchctl.log")


def test_unknown_option_is_refused(box):
    r = install(box, "--frce")
    assert r.returncode == 1 and "Unknown option --frce" in r.stdout


def test_the_recorder_installer_tests_first_and_verifies_the_load(box):
    r = install(box, script="install_recorder.sh", FAKE_PYTEST_RC="1")
    assert r.returncode != 0 and "Not deployed" in r.stdout and not (box["rec"] / "current").exists()
    r = install(box, script="install_recorder.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box["rec"] / "src").is_symlink() and read(box, "pytest.log").strip().endswith("/src")
    assert "print gui/" in read(box, "launchctl.log")                          # L15: the load is checked
    r = install(box, script="install_recorder.sh", FAKE_PRINT_RC="1")
    assert r.returncode != 0
    assert "did not load (attempt 3 of 3)" in r.stdout and "FAILED: com.agentdesk.recorder is not loaded" in r.stdout
