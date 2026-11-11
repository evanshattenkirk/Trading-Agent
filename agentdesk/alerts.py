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

from datetime import time

from . import health
from .clock import ct, session_date

DEFAULTS = {"enabled": True, "day_start": "07:00", "day_end": "21:00", "repeat_bad_min": 60, "morning": True,
            "morning_at": "08:20", "state_dir": "~/.agentdesk/alerts"}
ICON = {health.BAD: "🔴", health.WARN: "🟠"}


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
    """What to text now, and the state to keep. State: rows[name] = {sent: "warn"|"bad", at: ts} for rows whose
    alert hasn't been answered by a "fixed" text yet; `morning` = the last day the all-clear went. A row missing from
    the report counts as green. Red goes at once (repeats hourly in the day), amber and fixed wait for the day."""
    s = settings(cfg)
    state = dict(prev or {})
    sent_rows = {k: dict(v) for k, v in (state.get("rows") or {}).items()}
    day = daytime(now, cfg)
    rows = {r["name"]: r for r in report.get("checks") or []}
    red, amber, fixed = [], [], []
    for name in list(dict.fromkeys([*rows, *sent_rows])):
        r = rows.get(name) or health.row(name, health.OK, "")
        st, rec = r["state"], sent_rows.get(name)
        was = rec["sent"] if rec else None
        if st == health.BAD:
            if was != health.BAD:
                red.append(_block(r))
                sent_rows[name] = {"sent": health.BAD, "at": now}
            elif day and now - rec["at"] >= float(s["repeat_bad_min"]) * 60:
                red.append(_block(r, "still red: "))
                sent_rows[name] = {"sent": health.BAD, "at": now}
        elif st == health.WARN:
            if was != health.WARN and day:
                amber.append(_block(r))
                sent_rows[name] = {"sent": health.WARN, "at": now}
        elif was and day:
            fixed.append(f"✅ {name}: fixed" + (f" ({r['detail']})" if r.get("detail") else ""))
            sent_rows.pop(name)
    texts = []
    if red or amber or fixed:
        texts.append("AgentDesk\n" + "\n\n".join(red + amber + fixed))
    state["rows"] = sent_rows
    today = str(session_date(now))
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
