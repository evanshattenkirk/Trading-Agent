"""Who owns the dashboard port. The engine (`run`) and tools/paper_session.sh drop a file named after their process
id in `review.claims_dir` while they run; the after-hours review server listens only while no live process holds a
claim, so the engine gets http://127.0.0.1:8765 back at 08:10. Claims of dead processes are deleted, so a crash or
a reboot can't lock the review page out.

A claim file holds its process's start time (`ps -o lstart=` in UTC and the C locale; tools/claims.sh writes and
reads the same string). After a reboot or a `kill -9` the pid can come back as an unrelated process, so a claim
counts only while the pid is alive *and* started at the recorded time. An empty claim (the format before
2026-10-07) counts while its process started no later than the file was written.
"""
from __future__ import annotations

import asyncio
import calendar
import os
import socket
import subprocess
import time
from pathlib import Path

LSTART = "%a %b %d %H:%M:%S %Y"
SAME_START_S = 2          # Linux ps derives lstart from boot time + ticks and can differ by a second between calls


def start_time(pid: int) -> str | None:
    """`ps -o lstart= -p pid` in UTC and the C locale, whitespace collapsed; None when ps can't tell (no such
    process, or no ps)."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5,
                             env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return " ".join(out.split()) or None


def _epoch(lstart: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(" ".join(lstart.split()), LSTART)))
    except (ValueError, OverflowError):
        return None


def same_start(a: str, b: str) -> bool:
    if " ".join(a.split()) == " ".join(b.split()):
        return True
    ta, tb = _epoch(a), _epoch(b)
    return ta is not None and tb is not None and abs(ta - tb) <= SAME_START_S


def claim(directory: Path | str, pid: int | None = None) -> Path:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    pid = pid or os.getpid()
    path = d / str(pid)
    start = start_time(pid)
    path.write_text(f"{start}\n" if start else "")
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


def _holds(path: Path, pid: int) -> bool:
    """True while the claim's process is the one that wrote it."""
    if not pid_alive(pid):
        return False
    try:
        want, written = path.read_text(), path.stat().st_mtime
    except OSError:
        return False
    have = start_time(pid)
    if have is None:                                   # ps can't tell: the pid check alone, as before
        return True
    if want.strip():
        return same_start(want, have)
    started = _epoch(have)                             # old empty claim: its process must predate the file
    return started is None or started <= written + SAME_START_S


def claimed(directory: Path | str) -> list[int]:
    """Live claimants' pids; stale or malformed claim files are removed."""
    d = Path(directory)
    if not d.is_dir():
        return []
    live = []
    for p in d.iterdir():
        pid = int(p.name) if p.name.isdigit() else 0
        if pid > 0 and _holds(p, pid):
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
