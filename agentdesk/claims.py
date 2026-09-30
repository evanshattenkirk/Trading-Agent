"""Who owns the dashboard port. The engine (`run`) and tools/paper_session.sh drop a file named after their process
id in `review.claims_dir` while they run; the after-hours review server listens only while no live process holds a
claim, so the engine gets http://127.0.0.1:8765 back at 08:10. Claims of dead processes are deleted, so a crash or
a reboot can't lock the review page out.
"""
from __future__ import annotations

import asyncio
import os
import socket
import time
from pathlib import Path


def claim(directory: Path | str, pid: int | None = None) -> Path:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    path = d / str(pid or os.getpid())
    path.touch()
    return path


def release(path: Path | str) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def claimed(directory: Path | str) -> list[int]:
    """Live claimants' pids; stale or malformed claim files are removed."""
    d = Path(directory)
    if not d.is_dir():
        return []
    live = []
    for p in d.iterdir():
        pid = int(p.name) if p.name.isdigit() else 0
        if pid > 0 and pid_alive(pid):
            live.append(pid)
        else:
            release(p)
    return sorted(live)


def _probe_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "", "localhost") else "::1" if host == "::" else host


def port_in_use(host: str, port: int) -> bool:
    """True when something accepts connections on host:port."""
    try:
        with socket.create_connection((_probe_host(host), port), timeout=0.5):
            return True
    except OSError:
        return False


async def wait_port_free(host: str, port: int, timeout: float, poll: float = 0.25) -> bool:
    end = time.monotonic() + timeout
    while port_in_use(host, port):
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(poll)
    return True
