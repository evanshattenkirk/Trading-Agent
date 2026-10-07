"""Stub commands for driving the tools/*.sh scripts in tests: a fake clock shared by `date` and `sleep`.

FAKE_CLOCK names a file holding the current epoch seconds; `sleep N` advances it instead of waiting. `date +FMT`
prints it in $TZ, or in FAKE_SYSTEM_TZ (default America/Chicago) when TZ is unset, as launchd's jobs see it.
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

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


def stub(d: Path, name: str, body: str) -> None:
    f = d / name
    f.write_text(body)
    f.chmod(0o755)


def set_clock(path, when: str) -> None:
    """when: 'YYYY-MM-DD HH:MM' in Central time."""
    Path(path).write_text(repr(datetime.fromisoformat(when).replace(tzinfo=CT).timestamp()))


def clock_ct(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, CT)
