"""Port claims: the engine and paper_session.sh claim the dashboard port; the review server steps aside."""
import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import claims


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_claim_and_release(tmp_path):
    path = claims.claim(tmp_path / "claims")
    assert path.name == str(os.getpid())
    assert claims.claimed(tmp_path / "claims") == [os.getpid()]
    claims.release(path)
    assert claims.claimed(tmp_path / "claims") == []
    claims.release(path)                         # twice is fine


def test_dead_and_junk_claims_are_cleared(tmp_path):
    d = tmp_path / "claims"
    d.mkdir()
    (d / str(dead_pid())).touch()
    (d / "not-a-pid").touch()
    (d / str(os.getpid())).touch()
    assert claims.claimed(d) == [os.getpid()]
    assert sorted(p.name for p in d.iterdir()) == [str(os.getpid())]


def test_missing_dir_means_unclaimed(tmp_path):
    assert claims.claimed(tmp_path / "nope") == []


def test_port_in_use_and_wait():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    assert claims.port_in_use("127.0.0.1", port)

    async def main():
        assert not await claims.wait_port_free("127.0.0.1", port, timeout=0.3, poll=0.05)
        asyncio.get_running_loop().call_later(0.2, s.close)
        assert await claims.wait_port_free("127.0.0.1", port, timeout=3, poll=0.05)
    asyncio.run(main())
    assert not claims.port_in_use("127.0.0.1", port)


def test_wildcard_host_checks_loopback():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    try:
        assert claims.port_in_use("0.0.0.0", s.getsockname()[1])
    finally:
        s.close()


# ---------------------------------------------------------------- pid reuse (a reboot or kill -9 leaves a claim behind)

def test_claim_records_the_claimants_start_time(tmp_path):
    path = claims.claim(tmp_path / "claims")
    start = claims.start_time(os.getpid())
    assert start and path.read_text().split() == start.split()


def test_a_claim_whose_pid_now_belongs_to_another_process_is_stale(tmp_path):
    d = tmp_path / "claims"
    d.mkdir()
    (d / str(os.getpid())).write_text("Mon Jan  1 00:00:00 2001\n")   # same pid, an older process's start time
    assert claims.claimed(d) == []
    assert list(d.iterdir()) == []                                     # removed like a dead pid's claim


def test_a_claim_for_a_reused_root_pid_is_stale(tmp_path):
    d = tmp_path / "claims"
    d.mkdir()
    (d / "1").write_text("Mon Jan  1 00:00:00 2001\n")       # pid 1 is always alive (kill(1, 0) -> EPERM)
    assert claims.claimed(d) == []


def test_an_empty_legacy_claim_older_than_its_process_is_stale(tmp_path):
    d = tmp_path / "claims"
    d.mkdir()
    old = d / "1"                                             # written before pid 1 started (e.g. before a reboot)
    old.touch()
    os.utime(old, (1_000_000_000, 1_000_000_000))
    mine = d / str(os.getpid())                               # an old-format claim from a running process: kept
    mine.touch()
    assert claims.claimed(d) == [os.getpid()]


def test_start_times_one_second_apart_still_match():
    a = "Wed Oct  7 13:10:01 2026"
    assert claims.same_start(a, "Wed Oct 7 13:10:01 2026")
    assert claims.same_start(a, "Wed Oct  7 13:10:02 2026")          # Linux ps can round either way
    assert not claims.same_start(a, "Wed Oct  7 13:10:09 2026")
    assert not claims.same_start(a, "garbage")


def test_a_claim_is_kept_when_ps_cannot_tell(tmp_path, monkeypatch):
    d = tmp_path / "claims"
    d.mkdir()
    (d / str(os.getpid())).write_text("Mon Jan  1 00:00:00 2001\n")
    monkeypatch.setattr(claims, "start_time", lambda pid: None)       # no ps: fall back to the pid check
    assert claims.claimed(d) == [os.getpid()]


def test_a_child_claim_ends_with_the_child(tmp_path):
    d = tmp_path / "claims"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        claims.claim(d, child.pid)
        assert claims.claimed(d) == [child.pid]
    finally:
        child.kill()
        child.wait()
    assert claims.claimed(d) == []


def test_a_blocking_claim_is_logged_once_at_startup(tmp_path, caplog):
    from agentdesk.review import ReviewSupervisor
    d = tmp_path / "claims"
    claims.claim(d)
    sup = ReviewSupervisor(None, "127.0.0.1", 1, d)

    async def steps(n):
        for _ in range(n):
            await sup.step()
    with caplog.at_level("INFO", logger="agentdesk.review"):
        asyncio.run(steps(3))
    lines = [r.getMessage() for r in caplog.records if str(os.getpid()) in r.getMessage()]
    assert len(lines) == 1 and "claim" in lines[0] and str(d) in lines[0]
    assert not sup.serving


# ---------------------------------------------------------------- the shell side (tools/claims.sh)

SH = Path(__file__).resolve().parent.parent / "tools" / "claims.sh"


def sh(script: str):
    return subprocess.run(["bash", "-c", f'. "{SH}"; {script}'], capture_output=True, text=True)


def test_shell_claims_and_python_claims_read_each_other(tmp_path):
    d = tmp_path / "claims"
    d.mkdir()
    shell = subprocess.Popen(["bash", "-c", f'. "{SH}"; write_claim "$CLAIMS" $$; sleep 30'],
                             env={**os.environ, "CLAIMS": str(d)})
    try:
        for _ in range(100):
            f = d / str(shell.pid)
            if f.exists() and f.read_text().strip():
                break
            time.sleep(0.05)
        assert claims.claimed(d) == [shell.pid]                         # Python reads the shell's claim
        mine = claims.claim(d)
        assert sh(f'claim_alive "{mine}" && echo alive').stdout.strip() == "alive"   # and the shell reads Python's
    finally:
        shell.kill()
        shell.wait()
    assert claims.claimed(d) == [os.getpid()]


def test_shell_rejects_a_reused_pid_a_dead_one_and_junk(tmp_path):
    d = tmp_path / "claims"
    d.mkdir()
    (d / "1").write_text("Mon Jan  1 00:00:00 2001\n")
    (d / str(dead_pid())).write_text("")
    (d / "junk").write_text("")
    r = sh(f'for p in $(ls "{d}"); do claim_alive "{d}/$p" && echo "$p"; done; live_claims "{d}"')
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_shell_lists_live_claims(tmp_path):
    d = tmp_path / "claims"
    claims.claim(d)
    assert sh(f'live_claims "{d}"').stdout.split() == [str(os.getpid())]
    assert sh(f'live_claims "{tmp_path / "none"}"').stdout.split() == []
