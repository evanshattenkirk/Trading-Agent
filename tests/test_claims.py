"""Port claims: the engine and paper_session.sh claim the dashboard port; the review server steps aside."""
import asyncio
import os
import socket
import subprocess
import sys
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
