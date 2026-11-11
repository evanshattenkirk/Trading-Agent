"""Phone alerts from the Manager panel (Evan, 2026-11-09: make the engine easier to run unattended; 2026-11-10:
"imessage to myself is good"). Spec: docs/superpowers/specs/2026-11-10-phone-alerts-design.md.

The LaunchAgent com.agentdesk.watch runs `python -m agentdesk alerts` every 5 minutes. Each run reads the Manager
panel's report (GET /api/health from the engine or the review page, or health.report on local files when port 8765
is silent), works out what changed since the last text (`diff`, pure), and texts Evan through the Mac's Messages
app, with the same text as a Mac banner. Only a row's name, detail, fix and cmd ever reach a message, never a token,
account number or balance. The recipient comes from ALERT_IMESSAGE_TO in the Mac's .env and is logged as its last
4 characters only.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.request
from datetime import time
from pathlib import Path

from . import health
from .clock import ct, session_date
from .config import expand

DEFAULTS = {"enabled": True, "day_start": "07:00", "day_end": "21:00", "repeat_bad_min": 60, "morning": True,
            "morning_at": "08:20", "state_dir": "~/.agentdesk/alerts"}
ICON = {health.BAD: "🔴", health.WARN: "🟠"}
CONFIRM = 2                 # amber, and green after an alert, must hold this many checks in a row (10 min)
MAX_REPEATS = 2             # "still red" repeats per row per day
log = logging.getLogger("agentdesk.alerts")


# The handle and the text reach osascript as arguments (argv), never inside the script source, so no quote, newline
# or "end tell" in a message can change what runs.
IMESSAGE_SCRIPT = """on run argv
  tell application "Messages"
    set svc to 1st account whose service type = iMessage
    send (item 2 of argv) to participant (item 1 of argv) of svc
  end tell
end run"""
BANNER_SCRIPT = """on run argv
  display notification (item 1 of argv) with title "AgentDesk"
end run"""
OSASCRIPT = "/usr/bin/osascript"


def mask(to: str | None) -> str:
    return "…" + to[-4:] if to else "(none)"


def send(text: str, to: str | None, banner: bool = True, runner=subprocess.run) -> bool:
    """Show the Mac banner (unless `banner` is off, as on a retry), then iMessage `to`. True only when the iMessage
    went (osascript exit 0). No recipient: banner only, False."""
    if banner:
        try:
            runner([OSASCRIPT, "-e", BANNER_SCRIPT, text], timeout=30, capture_output=True, text=True)
        except Exception:
            pass
    if not to:
        return False
    try:
        r = runner([OSASCRIPT, "-e", IMESSAGE_SCRIPT, to, text], timeout=30, capture_output=True, text=True)
    except Exception as ex:
        log.warning("alerts: iMessage to %s failed: %s", mask(to), str(ex).replace(to, mask(to)))
        return False
    if r.returncode != 0:
        why = (getattr(r, "stderr", "") or "").replace(to, mask(to)).strip()[:300]
        log.warning("alerts: iMessage to %s failed (osascript exit %s): %s", mask(to), r.returncode, why or "no reason given")
    return r.returncode == 0


def settings(cfg) -> dict:
    return {**DEFAULTS, **dict(cfg.get("alerts") or {})}


def _hm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def daytime(now: float, cfg) -> bool:
    s = settings(cfg)
    return _hm(s["day_start"]) <= ct(now).time() < _hm(s["day_end"])


def _block(r: dict, prefix: str = "") -> str:
    lines = [f"{ICON[r['state']]} {prefix}{r['name']}: {r['detail']}"]
    lines += [x for x in (r.get("fix"), r.get("cmd")) if x]
    return "\n".join(lines)


def diff(prev: dict, report: dict, now: float, cfg) -> tuple[list[str], dict]:
    """What to text now, and the state to keep. State: rows[name] = {sent: "warn"|"bad", at, repeats} for rows whose
    alert hasn't been answered by a "fixed" text yet; streak[name] = [state, checks in a row] (ok and idle count as
    green); `morning` = the last day the all-clear went. A row missing from the report counts as green.

    Red goes at once, then "still red" hourly in the day, at most MAX_REPEATS a day. Amber goes once it has held
    CONFIRM checks in a row, in the day. "Fixed" goes once green has held CONFIRM checks in a row, in the day. So a
    row that flips on every check (one rate limit in 15 minutes) sends one red at most, never a stream."""
    s = settings(cfg)
    state = dict(prev or {})
    sent_rows = {k: dict(v) for k, v in (state.get("rows") or {}).items()}
    old_streak = dict(state.get("streak") or {})
    day = daytime(now, cfg)
    today = str(session_date(now))
    rows = {r["name"]: r for r in report.get("checks") or []}
    streak = {}
    red, amber, fixed = [], [], []
    for name in list(dict.fromkeys([*rows, *sent_rows])):
        r = rows.get(name) or health.row(name, health.OK, "")
        st = r["state"]
        kind = st if st in (health.BAD, health.WARN) else "green"
        last = old_streak.get(name) or [None, 0]
        n = last[1] + 1 if last[0] == kind else 1
        streak[name] = [kind, n]
        rec = sent_rows.get(name)
        was = rec["sent"] if rec else None
        if st == health.BAD:
            if was != health.BAD:
                red.append(_block(r))
                sent_rows[name] = {"sent": health.BAD, "at": now, "repeats": 0}
            elif day and now - rec["at"] >= float(s["repeat_bad_min"]) * 60:
                repeats = 0 if str(session_date(rec["at"])) != today else int(rec.get("repeats") or 0)
                if repeats < MAX_REPEATS:
                    red.append(_block(r, "still red: "))
                    sent_rows[name] = {"sent": health.BAD, "at": now, "repeats": repeats + 1}
        elif st == health.WARN:
            if was != health.WARN and n >= CONFIRM and day:
                amber.append(_block(r))
                sent_rows[name] = {"sent": health.WARN, "at": now, "repeats": 0}
        elif was and n >= CONFIRM and day:
            fixed.append(f"✅ {name}: fixed" + (f" ({r['detail']})" if r.get("detail") else ""))
            sent_rows.pop(name)
    texts = []
    if red or amber or fixed:
        texts.append("AgentDesk\n" + "\n\n".join(red + amber + fixed))
    state["rows"] = sent_rows
    state["streak"] = streak
    if (s["morning"] and day and health.trading_day(now, cfg) and ct(now).time() >= _hm(s["morning_at"])
            and state.get("morning") != today):
        state["morning"] = today
        hm = ct(now).strftime("%H:%M")
        bad = [r for r in rows.values() if r["state"] not in (health.OK, health.IDLE)]
        if bad:
            texts.append(f"AgentDesk {hm}: not all green: " + ", ".join(f"{r['name']} ({r['state']})" for r in bad))
        else:
            eng = rows.get("Engine")
            texts.append(f"AgentDesk {hm}: all green" + (f"; {eng['detail']}" if eng and eng.get("detail") else ""))
    return texts, state


# ---------------------------------------------------------------- one run of the job
HELLO = "AgentDesk alerts are on. You'll get a text here when the Manager panel turns amber or red."
MAX_TRIES = 3
SILENT_CHECKS = 2           # the port must be silent on two checks in a row (10 min) before anything is judged


def dashboard_row() -> dict:
    return health.row("Dashboard", health.BAD, "neither the engine nor the review page answers on :8765",
                      "Restart the review page; if the engine should be running, check today's engine log:",
                      "launchctl kickstart -k gui/$(id -u)/com.agentdesk.review")


def fetch(url: str, timeout: float = 10) -> dict | None:
    """The Manager panel's report, or None when nothing answers or the answer isn't a report."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))    # loopback: never via a system proxy
    try:
        with opener.open(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("checks"), list) else None


def state_file(cfg) -> Path:
    return Path(expand(settings(cfg)["state_dir"])) / "state.json"


def _load(path: Path) -> dict:
    try:
        st = json.loads(path.read_text())
        if isinstance(st, dict):
            return st
        raise ValueError("not an object")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as ex:
        log.warning("alerts: state file %s unreadable (%s); starting fresh", path, ex)
        return {}


def _save(path: Path, st: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


def check(cfg, now: float, *, fetcher=fetch, local=None, sender=send, to: str | None = None,
          state_path: Path | None = None) -> list[str]:
    """One check: read the report, text what changed, retry earlier failed texts, save state. Returns the new texts
    it sent or tried to send."""
    if not settings(cfg)["enabled"]:
        log.info("alerts: off (alerts.enabled: false)")
        return []
    path = state_path or state_file(cfg)
    st = _load(path)
    new: list[str] = []
    if "hello" not in st:
        new.append(HELLO)
        st["hello"] = now
    rep = fetcher(f"http://127.0.0.1:{cfg['server']['port']}/api/health")
    if rep is None:
        st["unreachable"] = int(st.get("unreachable") or 0) + 1
        log.info("alerts: nothing answers on :%s (%d checks in a row)", cfg["server"]["port"], st["unreachable"])
        if st["unreachable"] >= SILENT_CHECKS:      # one slow answer is not an outage: judge nothing until the second
            rep = (local or health.report)(cfg, now, None, running=False)
            rep = {**rep, "checks": [*(rep.get("checks") or []), dashboard_row()]}
    else:
        st["unreachable"] = 0
    if rep is not None:
        texts, st = diff(st, rep, now, cfg)
        new += texts
    today = str(session_date(now))
    if not to and st.get("no_to") != today:
        st["no_to"] = today
        log.warning("alerts: ALERT_IMESSAGE_TO is not set in .env; Mac banners only")
    pending = []
    for item in st.get("pending") or []:
        if sender(item["text"], to, banner=False):
            log.info("alerts: retried text sent to %s", mask(to))
            continue
        item = {"text": item["text"], "tries": int(item.get("tries") or 1) + 1}
        if item["tries"] >= MAX_TRIES:
            log.warning("alerts: text dropped after %d tries to %s: %s", item["tries"], mask(to),
                        item["text"].splitlines()[0])
        else:
            pending.append(item)
    for text in new:
        ok = sender(text, to)
        log.info("alerts: %s to %s: %s", "sent" if ok else ("banner only" if not to else "FAILED, will retry"),
                 mask(to), text.replace("\n", " | "))
        if not ok and to:
            pending.append({"text": text, "tries": 1})
    st["pending"] = pending
    _save(path, st)
    return new
