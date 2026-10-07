"""Standalone 0DTE quote recorder: python -m agentdesk record-quotes

Runs apart from the engine (launchd keeps it alive), so an engine crash or a code change never costs a day of quotes.
- Idles outside the recording window (weekdays 08:25-15:05 CT) and records every 10 s inside regular hours,
  into the same journal.option_quotes table the engine used.
- Keeps the Mac from idle-sleeping (caffeinate -i) only while the window is open.
- Uses its own Robinhood OAuth grant (~/.agentdesk/recorder, redirect port 8767), so its token refreshes can't
  invalidate the engine's grant in ~/.agentdesk/rh_oauth.json.
- Read-only by construction: MeteredRobinhoodMCP refuses every tool outside READ_ONLY_TOOLS.
- Meters every Robinhood call (tool, latency, outcome) into journal.rh_calls for the rate-budget report.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from .brokers.robinhood import OptionQuoteRecorder, RobinhoodMCP, dict_items
from .clock import CT, is_rth, session_date
from .config import expand, hhmm
from .iv_recorder import settings as iv_settings

log = logging.getLogger("agentdesk.recorder")

RECORDER_DIR = Path(os.path.expanduser("~/.agentdesk/recorder"))
HEARTBEAT = RECORDER_DIR / "heartbeat"
DAYS_LOG = RECORDER_DIR / "days.jsonl"
READ_ONLY_TOOLS = frozenset({"get_accounts", "get_earnings_calendar", "get_equity_quotes", "get_option_chains",
                             "get_option_instruments", "get_option_quotes"})
DEFAULTS = {"start_ct": "08:25", "end_ct": "15:05", "every_sec": 10, "width": 10,
            "token_dir": "~/.agentdesk/recorder", "redirect_port": 8767, "alert_after_sec": 300}

RH_CALLS = """CREATE TABLE IF NOT EXISTS rh_calls (
  ts REAL, tool TEXT, ms REAL, ok INTEGER, kind TEXT, err TEXT, tag TEXT
)"""


def standalone_active(max_age: float = 60.0) -> bool:
    """True while the standalone recorder has written quotes within the last max_age seconds."""
    try:
        return time.time() - HEARTBEAT.stat().st_mtime < max_age
    except OSError:
        return False


def settings(cfg) -> dict:
    return {**DEFAULTS, **(cfg.get("recorder") or {})}


def in_window(ts: float, start: str, end: str) -> bool:
    d = datetime.fromtimestamp(ts, CT)
    return d.weekday() < 5 and hhmm(start) <= d.time() < hhmm(end)


def next_window_start(ts: float, start: str) -> float:
    d = datetime.fromtimestamp(ts, CT)
    for i in range(8):
        day = (d + timedelta(days=i)).date()
        s = datetime.combine(day, hhmm(start), CT).timestamp()
        if day.weekday() < 5 and s > ts:
            return s
    return ts + 86400


def classify(err: str) -> str:
    e = err.lower()
    if "429" in e or "rate limit" in e or "too many" in e or "throttl" in e:
        return "throttled"
    if "401" in e or "403" in e or "unauthorized" in e or "forbidden" in e or "oauth" in e:
        return "auth"
    if any(c in e for c in ("500", "502", "503", "504", "internal server error", "bad gateway", "unavailable")):
        return "server"
    if "timeout" in e or "timed out" in e:
        return "timeout"
    return "other"


class CallMeter:
    """Buffers one row per Robinhood call and flushes them to journal.rh_calls."""

    def __init__(self, db_path: Path | str, tag: str = "recorder"):
        self.db = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        from .journal import use_wal
        use_wal(self.db)                # same file as the engine's journal: WAL on every writer
        self.db.execute(RH_CALLS)
        self.db.commit()
        self.tag = tag
        self.buf: list[tuple] = []

    def add(self, ts: float, tool: str, ms: float, ok: bool, kind: str, err: str | None, tag: str | None = None) -> None:
        self.buf.append((ts, tool, round(ms, 1), int(ok), kind, err, tag or self.tag))
        if len(self.buf) >= 50:
            self.flush()

    def flush(self) -> None:
        if self.buf:
            self.db.executemany("INSERT INTO rh_calls VALUES (?,?,?,?,?,?,?)", self.buf)
            self.db.commit()
            self.buf.clear()


class MeteredRobinhoodMCP(RobinhoodMCP):
    """RobinhoodMCP limited to read-only tools, timing every call into a CallMeter."""

    def __init__(self, cfg, meter: CallMeter):
        super().__init__(cfg)
        self.meter = meter

    async def call(self, tool: str, args: dict, tag: str | None = None):
        if tool not in READ_ONLY_TOOLS:
            raise PermissionError(f"recorder is read-only; refused {tool}")
        t0 = time.time()
        try:
            out = await super().call(tool, args)
        except Exception as ex:
            self.meter.add(t0, tool, (time.time() - t0) * 1000, False, classify(str(ex)), str(ex)[:300], tag)
            raise
        self.meter.add(t0, tool, (time.time() - t0) * 1000, True, "ok", None, tag)
        return out


async def spot_price(rh, symbol: str) -> float | None:
    """Latest SPY trade from get_equity_quotes (regular or extended hours, whichever printed last)."""
    data = await rh.call("get_equity_quotes", {"symbols": [symbol]})
    for it in dict_items(data):
        q = it.get("quote") if isinstance(it.get("quote"), dict) else it
        cands = [(str(q.get(t) or ""), q.get(p)) for t, p in (("venue_last_trade_time", "last_trade_price"),
                                                               ("venue_last_non_reg_trade_time", "last_non_reg_trade_price"))]
        cands = [(t, float(p)) for t, p in cands if p not in (None, "") and float(p) > 0]
        if cands:
            return max(cands)[1]
    return None


def recorder_cfg(cfg, s: dict):
    c = copy.deepcopy(dict(cfg))
    c["robinhood"] = {**c["robinhood"], "token_dir": s["token_dir"], "redirect_port": int(s["redirect_port"])}
    c["robinhood"].pop("call_budget", None)      # the engine's budget; the recorder paces itself (and the probe must not be capped)
    return c


def notify(msg: str) -> None:
    try:
        subprocess.run(["/usr/bin/osascript", "-e", f'display notification {json.dumps(msg)} with title "AgentDesk recorder"'],
                       timeout=5, check=False, capture_output=True)
    except Exception:
        pass


class IVClient:
    """The IV pass's view of the recorder's session: same read-only grant, calls metered under tag 'iv'."""

    def __init__(self, daemon):
        self.d = daemon

    async def call(self, tool: str, args: dict):
        await self.d.connect()
        return await self.d.rh.call(tool, args, tag="iv")


class RecorderDaemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.s = settings(cfg)
        self.symbol = cfg.get("symbol", "SPY")
        self.db_path = expand(cfg["journal_path"])
        from .journal import Journal
        self.journal = Journal(self.db_path)
        self.journal.db.execute("PRAGMA busy_timeout=30000")
        self.meter = CallMeter(self.db_path)
        self.rh_cfg = recorder_cfg(cfg, self.s)
        self.rh: MeteredRobinhoodMCP | None = None
        self.rec: OptionQuoteRecorder | None = None
        self.caff: subprocess.Popen | None = None
        self.day: str | None = None
        self.last_ok = 0.0
        self.last_alert = 0.0
        self.fails = 0
        self.iv_task: asyncio.Task | None = None
        self._conn = asyncio.Lock()
        self.ivs = iv_settings(cfg)
        RECORDER_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- session
    async def connect(self) -> None:
        async with self._conn:          # the quote loop and the IV loop share one session (one OAuth grant)
            if self.rh is not None:
                return
            rh = MeteredRobinhoodMCP(self.rh_cfg, self.meter)
            await rh.start()
            self.rh = rh
            self.rec = OptionQuoteRecorder(rh, self.journal, every=float(self.s["every_sec"]), width=int(self.s["width"]),
                                           symbol=self.symbol, standalone=True)

    async def disconnect(self) -> None:
        async with self._conn:
            rh, self.rh, self.rec = self.rh, None, None
            if rh is not None:
                try:
                    await rh.close()
                except BaseException as ex:          # anyio cancel scopes can raise on close after a dropped session
                    log.debug("close: %r", ex)

    def awake(self, on: bool) -> None:
        if on and (self.caff is None or self.caff.poll() is not None):
            try:
                self.caff = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
            except OSError as ex:
                log.warning("caffeinate unavailable: %s", ex)
        elif not on and self.caff is not None:
            self.caff.terminate()
            self.caff = None

    # ---------------------------------------------------------------- book E's IV pass
    def _iv_day(self):
        from .desks import holidays
        from .iv import Pacer, RobinhoodChains
        from .iv_recorder import IVDay, IVSnapshot, recorder_calendar
        client = IVClient(self)
        snap = IVSnapshot(RobinhoodChains(client, self.ivs["cache_dir"]), self.journal,
                          ((self.cfg.get("crew") or {}).get("earnings") or {}).get("universe") or [], holidays(self.cfg))
        return IVDay(snap, lambda d: recorder_calendar(client, self.cfg, d), self.ivs,
                     list_pacer=Pacer(float(self.ivs["list_calls_per_s"])),
                     quote_pacer=Pacer(float(self.ivs["quote_calls_per_s"])), log_fn=self._iv_log,
                     skip_days=holidays(self.cfg) | set((self.cfg.get("calendar") or {}).get("early_close") or []))

    def _iv_log(self, summary: dict) -> None:
        log.info("iv day done: %s", summary)
        try:
            with DAYS_LOG.open("a") as f:
                f.write(json.dumps(summary) + "\n")
        except OSError as ex:
            log.warning("iv day log failed: %s", ex)

    async def iv_loop(self) -> None:
        day = self._iv_day()
        while True:
            try:
                await day.step(time.time())
            except Exception as ex:
                log.warning("iv pass: %s", str(ex)[:300])
            self.meter.flush()
            await asyncio.sleep(5)

    # ---------------------------------------------------------------- one poll
    async def tick(self, now: float) -> int:
        await self.connect()
        if not is_rth(now):
            exp = str(session_date(now))
            if self.rec.chain_day != exp:
                await self.rec.load_chain(exp)       # warm the chain before the open
            return 0
        px = await spot_price(self.rh, self.symbol)
        if not px:
            return 0
        n = await self.rec.poll_once(px, now)
        if n:
            HEARTBEAT.touch()
        return n

    async def run(self, once: bool = False) -> None:
        (RECORDER_DIR / "recorder.pid").write_text(str(os.getpid()))
        start, end, every = self.s["start_ct"], self.s["end_ct"], float(self.s["every_sec"])
        log.info("recorder up: window %s-%s CT weekdays, every %.0fs, ATM+-%s, journal %s",
                 start, end, every, self.s["width"], self.db_path)
        backoff = 5.0
        while True:
            now = time.time()
            if not once and not in_window(now, start, end):
                if self.day:
                    self.end_day()
                self.awake(False)
                await self.disconnect()
                nxt = next_window_start(now, start)
                if int(now) % 3600 < 60:
                    log.info("idle until %s", datetime.fromtimestamp(nxt, CT).strftime("%a %Y-%m-%d %H:%M CT"))
                await asyncio.sleep(min(60.0, max(1.0, nxt - now)))
                continue
            self.awake(True)
            if not once and self.ivs.get("enabled") and self.iv_task is None:
                self.iv_task = asyncio.create_task(self.iv_loop())
            if self.day is None:
                self.day = str(session_date(now))
                self.last_ok = now
                log.info("recording window open for %s", self.day)
            try:
                n = await self.tick(now)
                if n or not is_rth(now) or (self.rec and self.rec.chain_day and not self.rec.chain):
                    self.last_ok = time.time()          # recorded, pre-open, or no 0DTE expiry today (holiday)
                self.fails, backoff = 0, 5.0
                self.maybe_alert(now)                   # polls that keep coming back empty alert like failed ones
                if once:
                    log.info("one poll: %d rows", n)
                    await self.disconnect()
                    return
                self.meter.flush()
                await asyncio.sleep(max(0.5, every - (time.time() - now)))
            except Exception as ex:
                self.fails += 1
                log.warning("poll failed (%d in a row): %s", self.fails, str(ex)[:300])
                self.meter.flush()
                if once:
                    await self.disconnect()
                    raise
                if self.fails >= 3:
                    await self.disconnect()
                self.maybe_alert(now)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120.0)

    def maybe_alert(self, now: float) -> None:
        if is_rth(now) and now - self.last_ok > float(self.s["alert_after_sec"]) and now - self.last_alert > 1800:
            self.last_alert = now
            notify("No quotes recorded for 5+ minutes. Check ~/.agentdesk/recorder/recorder.log "
                   "(Robinhood may need you to sign in again).")

    def end_day(self) -> None:
        self.meter.flush()
        try:
            summary = day_summary(self.db_path, self.day)
            with DAYS_LOG.open("a") as f:
                f.write(json.dumps(summary) + "\n")
            log.info("day done: %s", summary)
        except Exception as ex:
            log.warning("day summary failed: %s", ex)
        self.day = None


# -------------------------------------------------------------------- reports
def _day_bounds(day: str) -> tuple[float, float]:
    d = datetime.fromisoformat(day).date()
    return (datetime.combine(d, hhmm("00:00"), CT).timestamp(), datetime.combine(d, hhmm("23:59"), CT).timestamp() + 60)


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def day_summary(db_path, day: str, tag: str = "recorder") -> dict:
    lo, hi = _day_bounds(day)
    db = sqlite3.connect(str(db_path), timeout=30)
    db.execute(RH_CALLS)
    q = db.execute("SELECT COUNT(*), COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM option_quotes WHERE ts>=? AND ts<?",
                   (lo, hi)).fetchone()
    calls = db.execute("SELECT ts, tool, ms, ok, kind FROM rh_calls WHERE tag=? AND ts>=? AND ts<?", (tag, lo, hi)).fetchall()
    db.close()
    kinds: dict[str, int] = {}
    for c in calls:
        kinds[c[4]] = kinds.get(c[4], 0) + 1
    ms = [c[2] for c in calls if c[3]]
    fmt = lambda t: datetime.fromtimestamp(t, CT).strftime("%H:%M:%S") if t else None
    return {"day": day, "rows": q[0], "snapshots": q[1], "first": fmt(q[2]), "last": fmt(q[3]),
            "calls": len(calls), "outcomes": kinds, "p50_ms": round(_pct(ms, 0.5)), "p95_ms": round(_pct(ms, 0.95)),
            "max_ms": round(max(ms)) if ms else 0}


def rate_report(db_path, days: int = 5, tag: str | None = None) -> str:
    db = sqlite3.connect(str(db_path), timeout=30)
    db.execute(RH_CALLS)
    since = time.time() - days * 86400
    rows = db.execute("SELECT ts, tool, ms, ok, kind, tag FROM rh_calls WHERE ts>=?" + (" AND tag=?" if tag else ""),
                      (since, tag) if tag else (since,)).fetchall()
    db.close()
    if not rows:
        return "No metered Robinhood calls yet."
    out = [f"Robinhood calls, last {days} day(s): {len(rows)}"]
    by_tool: dict[str, list] = {}
    for r in rows:
        by_tool.setdefault(r[1], []).append(r)
    out.append(f"  {'tool':24s} {'calls':>6s} {'p50ms':>6s} {'p95ms':>6s} {'maxms':>6s}  outcomes")
    for tool, rs in sorted(by_tool.items()):
        ms = [r[2] for r in rs if r[3]]
        kinds: dict[str, int] = {}
        for r in rs:
            kinds[r[4]] = kinds.get(r[4], 0) + 1
        out.append(f"  {tool:24s} {len(rs):6d} {_pct(ms, .5):6.0f} {_pct(ms, .95):6.0f} {max(ms or [0]):6.0f}  {kinds}")
    per_min: dict[int, int] = {}
    per_10s: dict[int, int] = {}
    for r in rows:
        per_min[int(r[0] // 60)] = per_min.get(int(r[0] // 60), 0) + 1
        per_10s[int(r[0] // 10)] = per_10s.get(int(r[0] // 10), 0) + 1
    busy = sorted(per_min.values())
    out.append(f"  per minute: median {_pct(busy, .5):.0f}, p95 {_pct(busy, .95):.0f}, peak {busy[-1]}; "
               f"peak 10 s: {max(per_10s.values())}")
    bad = [r for r in rows if r[4] in ("throttled", "server", "auth", "timeout")]
    for r in bad[-10:]:
        out.append(f"  {datetime.fromtimestamp(r[0], CT):%m-%d %H:%M:%S} {r[1]} {r[4]} [{r[5]}]")
    if not any(r[4] == "throttled" for r in rows):
        out.append("  No throttling (HTTP 429) seen in this period.")
    return "\n".join(out)


def status(cfg) -> str:
    s = settings(cfg)
    db_path = expand(cfg["journal_path"])
    now = time.time()
    day = str(session_date(now))
    lines = []
    try:
        age = now - HEARTBEAT.stat().st_mtime
        lines.append(f"last quotes written {age:.0f}s ago" + ("" if age < 60 else " (not recording right now)"))
    except OSError:
        lines.append("no quotes written by the standalone recorder yet")
    lines.append(f"window {s['start_ct']}-{s['end_ct']} CT weekdays; now {'inside' if in_window(now, s['start_ct'], s['end_ct']) else 'outside'}")
    lines.append("today: " + json.dumps(day_summary(db_path, day)))
    try:
        r = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/com.agentdesk.recorder"],
                           capture_output=True, text=True, timeout=5)
        st = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith(("state =", "pid =", "last exit code"))]
        lines.append("launchd: " + ("; ".join(st) if st else "job not loaded"))
    except Exception:
        pass
    return "\n".join(lines)


# -------------------------------------------------------------------- rate probe
async def probe(cfg, rates=(1, 2, 4, 8), step_sec: float = 20.0, max_workers: int = 4) -> str:
    """Bounded read-only ramp on get_equity_quotes to look for Robinhood's throttle point.
    Each step targets `rate` calls/s for step_sec using up to max_workers sessions; the ramp stops at the first
    throttled response or 3 errors in a step. Run it outside the recording window."""
    s = settings(cfg)
    meter = CallMeter(expand(cfg["journal_path"]), tag="probe")
    rcfg = recorder_cfg(cfg, s)
    first = MeteredRobinhoodMCP(rcfg, meter)
    await first.start()                     # any token refresh happens here, before the other sessions read the file
    sessions = [first]
    for _ in range(max_workers - 1):
        m = MeteredRobinhoodMCP(rcfg, meter)
        await m.start()
        sessions.append(m)
    sym = cfg.get("symbol", "SPY")
    out = []
    try:
        for rate in rates:
            results: list[tuple[bool, str, float]] = []
            t_end = time.time() + step_sec
            gap = 1.0 / rate

            async def worker(i: int, rh) -> None:
                nxt = time.time() + i * gap
                while time.time() < t_end:
                    await asyncio.sleep(max(0.0, nxt - time.time()))
                    nxt += gap * len(sessions)
                    t0 = time.time()
                    try:
                        await rh.call("get_equity_quotes", {"symbols": [sym]})
                        results.append((True, "ok", time.time() - t0))
                    except Exception as ex:
                        results.append((False, classify(str(ex)), time.time() - t0))
                        if classify(str(ex)) == "throttled":
                            return

            await asyncio.gather(*(worker(i, rh) for i, rh in enumerate(sessions)))
            meter.flush()
            ok = [r[2] * 1000 for r in results if r[0]]
            errs = [r[1] for r in results if not r[0]]
            achieved = len(results) / step_sec
            out.append(f"target {rate}/s: achieved {achieved:.2f}/s, {len(ok)} ok, errors {errs[:5] or 'none'}, "
                       f"p50 {_pct(ok, .5):.0f} ms, p95 {_pct(ok, .95):.0f} ms")
            if "throttled" in errs or len(errs) >= 3:
                out.append("stopped: throttled or repeated errors")
                break
            await asyncio.sleep(5)
    finally:
        for m in sessions:
            try:
                await m.close()
            except BaseException:
                pass
    return "\n".join(out)
