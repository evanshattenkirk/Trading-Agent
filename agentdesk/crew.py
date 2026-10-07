"""The research crew: desks that brief the agent, argue with each other, vote on size and pitch ideas.

Desks: Macro (econ calendar), Rates (bonds/yields), Fed Watch, Vol (VIX/expected move),
Quant (reviews our own trades), Risk (deterministic), Tape (Level 2 book, deterministic),
Ops (pre-flight), Earnings (book E's screen) and Post-mortem (rule audit). The last three live in desks.py and
are restrict-only: they can cut size, never raise it, pitch changes or add blackouts.

With ANTHROPIC_API_KEY set, Macro/Rates/Fed/Vol research with Claude + web search, and a
roundtable call lets desks respond to each other and revise their votes. Without a key (or in
the simulator) everything falls back to offline briefs and templated cross-talk.

What the crew can change
  size        each desk votes a multiplier in [0.5, 1.25]. Any vote below 1.0 cuts size immediately
              (lowest vote wins). A size-UP only happens on a SWING entry when every item of the
              conviction gate is true (see size_up()); then size = min(1.25, lowest of the
              Macro/Rates/Vol votes).
  blackouts   around high-impact events; cooldowns (Quant, max 30 min)
  proposals   per-trade / per-day tweaks inside whitelisted bounds (auto-applied), standing changes
              and new strategies (wait for your approval). See proposals.py.
It never places, cancels or sizes orders directly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
from dataclasses import dataclass
from datetime import date, time, timedelta
from pathlib import Path

from .clock import at_ct, ct, ct_time, is_rth, session_date
from .config import expand, hhmm
from . import desks as xdesks
from .desks import INFO_ONLY, RESTRICT_ONLY
from .proposals import ProposalBook

log = logging.getLogger("agentdesk.crew")


# Desk briefs run with web search on a thinking model: thinking, search calls and the JSON all count against
# max_tokens. At 1400 most desks were cut off before the JSON (stop_reason max_tokens) and fell back to offline.
DESK_MAX_TOKENS = 16000


def cached_system(text: str) -> list[dict]:
    """System prompt as one text block with an explicit cache breakpoint. Tools render before system, so this one
    marker caches the tool list plus the prompt; anything that changes per call belongs in the user message."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


@dataclass
class Desk:
    key: str
    name: str
    role: str
    uses_web: bool = True


DESKS = {
    "macro": Desk("macro", "Macro", "US economic calendar and data surprises today: CPI, PPI, NFP, jobless claims, ISM, retail sales, GDP, PCE, consumer sentiment. Times in CT, consensus vs prior, and what a miss does to SPY."
                  " You also cover the Federal Reserve (FOMC decision, minutes or presser if today, Fed speakers today with CT"
                  " times, fed-funds pricing) and Treasuries (2Y and 10Y yields and their change, curve, auctions today)."
                  ' Add two keys to the JSON: "fed": {"bias": "hawkish" | "neutral" | "dovish", "headline": "<= 90 chars",'
                  ' "notes": [...]} and "rates": {"headline": "<= 90 chars", "notes": [...]}. Fed and Treasury items that can'
                  " move SPY also go in events."),
    "rates": Desk("rates", "Rates", "Treasuries, read from the Macro desk's brief. Information only: no vote.", uses_web=False),
    "fed": Desk("fed", "Fed Watch", "The Federal Reserve, read from the Macro desk's brief. Information only: no vote.", uses_web=False),
    "vol": Desk("vol", "Vol", "Volatility. Robinhood's VIX and the expected move are in the message; don't search for them."
                 " Search for VIX1D and report vix1d_flag: true when VIX1D is more than 3 points above VIX, false when it"
                 " isn't; leave it out if you can't find VIX1D (book B skips the day on true). Then say whether the tape"
                 " favors trend or chop, and vote."),
    "quant": Desk("quant", "Quant", "Reviews the engine's own trades today: win rate, avg win/loss, which setup and time windows are working, whether the market is chopping the MACD triggers.", uses_web=False),
    "risk": Desk("risk", "Risk", "Deterministic risk manager.", uses_web=False),
    "tape": Desk("tape", "Tape", "Level 2 order book reader.", uses_web=False),
    "ops": Desk("ops", "Ops", "Pre-flight: paper mode, risk limits and watchdog armed, data and broker up, recorder landed.", uses_web=False),
    "earnings": Desk("earnings", "Earnings", "Robinhood earnings calendar: book E's windows and SPY heavyweights reporting.", uses_web=False),
    "postmortem": Desk("postmortem", "Post-mortem", "Audits the day's trades against the rules; writes the daily file.", uses_web=False),
}
VOTERS_FOR_SIZE_UP = ("macro", "vol")
BOOK_LETTERS = ("A", "B", "C", "D", "E", "F1", "F2", "G")
# Which books each desk's size vote reaches (crew.vote_books overrides). A desk with no entry (Risk, Tape, Earnings,
# Post-mortem) still cuts book A, as every vote did before; Rates and Fed (INFO_ONLY) reach no book.
DEFAULT_VOTE_BOOKS = {"macro": ["A", "C"], "vol": ["A", "B", "C", "D", "G"], "quant": ["A"], "ops": ["A"]}
BOOKS_TEXT = ("The desk runs paper books: A long SPY 0DTE calls on MACD/RSI triggers (4-5 contracts, flat by 14:40 CT);"
              " B iron fly sold at 08:45 CT; C bull-put spreads after a bullish opening-range break; D iron condor sold at"
              " 09:00 CT on quiet days; G SPY call calendar at 09:00 CT; E pre-earnings single-name straddles and calendars"
              " held for days; F1 stock longs on opening-range breakouts (flat by the close); F2 single-name call and put debit spreads held up to 3 days.")
# $ per million tokens (input, output) by model prefix; cache reads cost 0.1x input, cache writes 1.25x.
PRICES = {"claude-sonnet-5": (2.0, 10.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
          "claude-opus-5-5": (4.0, 20.0)}
WEB_SEARCH_USD = 0.01           # $10 per 1,000 searches
CITE_TAG = re.compile(r"</?cite\b[^>]*>")     # web-search citation markup the model leaves around quoted facts
# A desk's web-search brief is untrusted text: at most this many of its events a day become blackouts (or passed
# events for book B), only times still ahead block entries, and none of them flattens (Evan, decision D4,
# 2026-10-07); a flatten time comes only from the weekly calendar or crew.events in config.
MAX_BRIEF_EVENTS = 4


def price_of(model: str | None) -> tuple[float, float]:
    keys = [k for k in PRICES if (model or "").startswith(k)]
    return PRICES[max(keys, key=len)] if keys else (0.0, 0.0)

SCHEMA_HINT = """Reply with ONLY a JSON object:
{"headline": "<= 90 chars, the one thing the trader must know",
 "bias": "bullish" | "neutral" | "bearish",
 "confidence": 0.0-1.0,
 "events": [{"time_ct": "HH:MM", "name": "...", "impact": "high" | "medium" | "low"}],
 "size_multiplier": 0.5-1.25,
 "cooldown_minutes": 0-30,
 "notes": ["<= 4 short bullets with specific numbers"],
 "proposals": []}
size_multiplier is your vote: 1.0 = normal. Below 1.0 needs a concrete reason (event risk, vol spike).
Above 1.0 (max 1.25) only when your evidence clearly supports long SPY exposure today, with confidence
>= 0.6. A size-up only happens if Macro and Vol both vote above 1.0 and several market checks pass.
proposals (optional, usually empty): {"scope": "trade"|"day"|"standing"|"new_strategy", "title": "...",
 "rationale": "...", "params": {"<whitelisted key>": value}, "spec": "(new_strategy only) rules in plain words",
 "evidence": "numbers", "until": "YYYY-MM-DD or YYYY-MM-DD HH:MM CT, when the reason for it has passed (e.g. the
 event it is for); omit if it doesn't depend on a date"}. Whitelisted keys: exits.stop_loss_pct, exits.swing.runner_trail_pct,
 exits.scalp.runner_trail_pct, exits.swing.scale_outs.0.at, exits.scalp.scale_outs.0.at,
 exits.swing.time_stop_min, exits.scalp.time_stop_min, strategy.rsi.upper, strategy.entry_window.end,
 risk.max_trades_per_day, strategy.enabled_setups, strikes.max_offset.
events = only items scheduled for TODAY."""

ROUNDTABLE_HINT = """You moderate a short roundtable between research desks on a 0DTE SPY call-buying team.
Given each desk's brief, write the conversation where desks respond TO EACH OTHER (not just to the trader):
challenge weak reasoning, connect dots (e.g. Rates reacting to Macro's data, Vol pricing Fed risk), and let
any desk revise its size vote after hearing the others. Keep it terse and numeric. Reply ONLY with JSON:
{"lines": [{"from": "<desk>", "to": "<desk or agent>", "text": "<= 110 chars"}],   // 3-6 lines
 "votes": {"<desk>": 0.5-1.25},        // revised votes, only for desks that changed
 "proposals": [ ...same shape as in the briefs, include "desk" ... ]}   // usually empty"""

PREMARKET_DESKS = ["macro", "rates", "fed", "vol", "ops", "earnings"]


def clock_label(hm: str) -> str:
    """'08:15' -> '8:15'"""
    t = hhmm(hm)
    return f"{t.hour}:{t.minute:02d}"


HUDDLE_SPOTS = [(80, 84), (112, 84), (78, 98), (114, 98), (90, 102), (102, 102), (96, 104)]


class Crew:
    def __init__(self, engine, cfg):
        self.e = engine
        self.cfg = cfg["crew"]
        self.enabled = self.cfg.get("enabled", True)
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.offline = engine.feed.is_sim or not self.api_key
        self.briefs: dict[str, dict] = {}
        self.ran: set[str] = set()
        self.day = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()                 # background tasks, referenced until they finish
        self._halt_huddled = None                           # (day, halt reason) that already had its huddle
        self.prep: dict[str, dict] = {}                     # briefs the premarket desks wrote at their desks
        self.prep_tasks: dict[str, asyncio.Task] = {}       # ... and the ones still researching
        self._client = None
        self.cache_usage = self._new_usage()
        self.vix = None                                     # VIX source for the Vol desk (Robinhood or the sim)
        self._calendar: dict | None = None                  # this week's saved economic calendar
        self._event_src: dict[str, str] = {}                # blackout name -> desk that sourced it
        self._brief_events: dict[str, set] = {}             # desk -> event times its briefs added today (the cap)
        self._logged: set = set()                           # crew_log block keys already written today
        self._cal_check = None
        self.directive = {"size_mult": 1.0, "blackouts": [], "summary": "", "votes": {}}
        self.conviction = {"mult": 1.0, "checks": [], "ts": 0}
        path = None if engine.feed.is_sim else expand(cfg["journal_path"]).parent / "proposals.json"
        self.book = ProposalBook(cfg, path)
        self._last_expire = -1e18
        self.mood = getattr(engine.feed, "mood", None)

    # ------------------------------------------------------------ schedule
    async def on_clock(self, now: float) -> None:
        if not self.enabled:
            return
        if abs(now - self._last_expire) >= 30:
            self.expire_proposals(now)
        d = session_date(now)
        if d != self.day:
            rolled = self.day is not None           # a day change in a running process, not the first tick
            self.day, self.ran = d, set()
            self._drop_prep()
            self._logged, self._cal_check, self.cache_usage = set(), None, self._new_usage()
            self._brief_events = {}
            self.briefs, self.conviction = {}, {"mult": 1.0, "checks": [], "ts": 0}     # yesterday's reads are done
            self._load_config_events(now)
            st = self.e.risk.st
            self.directive = {"size_mult": st.size_mult, "blackouts": [x.name for x in st.blackouts], "summary": "",
                              "votes": {}}
            if rolled:
                self.e.bus.emit("directive", now, directive=self.directive, risk=self.e.risk.to_dict())
        t = ct_time(now)
        sch = self.cfg["schedule"]
        if "calendar" not in self.ran and t >= hhmm(sch.get("arrive") or sch["premarket"]) and ct(now).weekday() < 5:
            self.ran.add("calendar")
            if self.offline:                        # offline it only reads the saved file
                await self._ensure_calendar(now)
            else:                                   # one web call a week; never holds up the clock
                self._spawn(self._ensure_calendar(now))
        if "arrive" in sch and "arrive" not in self.ran and t >= hhmm(sch["arrive"]) and ct(now).weekday() < 5:
            self.ran.add("arrive")
            if t < hhmm(sch["premarket"]):          # started after the huddle time: the huddle briefs in full
                await self._catch_up(PREMARKET_DESKS, now)
        plan = [("premarket", PREMARKET_DESKS), ("midday", ["macro", "rates", "vol"]),
                ("late", ["fed", "vol"]), ("postclose", ["quant", "vol", "postmortem"])]
        for slot, desks in plan:
            if slot not in self.ran and t >= hhmm(sch[slot]) and ct(now).weekday() < 5:
                self.ran.add(slot)
                if slot != "premarket" and now - at_ct(d, hhmm(sch[slot])) > 1800:
                    continue
                await self._dispatch(slot, desks, now)

    def _load_config_events(self, now: float) -> None:
        d = session_date(now)
        evs = [e for e in self.cfg.get("events") or [] if str(e.get("date")) == str(d)]
        if self.e.feed.is_sim and getattr(self.e.feed, "event", None):
            evs.append({"time": self.e.feed.event["time"], "name": self.e.feed.event["name"], "impact": "high"})
        for ev in evs:
            if ev.get("impact") == "high":
                self.e.risk.add_blackout(at_ct(d, hhmm(ev["time"])), ev["name"], added_ts=now)
                self._event_src.setdefault(ev["name"], "config")
        self._config_events = evs

    async def on_trade_closed(self, pos, net: float, now: float) -> None:
        st = self.e.risk.st
        q = self.briefs.get("quant")
        if net >= 0 and q and q.get("size_multiplier", 1.0) < 1.0:
            q["size_multiplier"] = 1.0
            q["headline"] = "Winner booked. My vote goes back to 100%."
            self.e.bus.emit("crew", now, desk="quant", phase="done", brief=q)
            self._apply(now)
            self._log(now, "directive", None, {"slot": "trade-closed", "desks": ["quant"],
                                               "votes": self.directive.get("votes", {}),
                                               "book_mults": self.directive.get("book_mults", {}),
                                               "revised": {"quant": 1.0}, "blackouts": self.directive.get("blackouts", [])})
        if net < 0 and st.loss_streak >= self.cfg.get("consult_quant_after_losses", 2):
            desks = ["quant", "risk"] + (["tape"] if self.e.l2 and self.e.l2.latest else [])
            await self._dispatch("loss-review", desks, now)
        if st.halted and self._halt_huddled != (self.day, st.halt_reason):    # one huddle per halt, not per close
            self._halt_huddled = (self.day, st.halt_reason)
            await self._dispatch("halt", ["risk", "quant", "ops"], now)

    # ------------------------------------------------------------ premarket catch-up
    async def _catch_up(self, desks: list[str], now: float) -> None:
        """Desks arrive and research at their own desks. Their briefs wait for the premarket huddle.
        Online, each desk researches in its own task so a slow one can't hold up the others or the huddle."""
        e, bus = self.e, self.e.bus
        e.set_agent(now, "coffee", f"Team's in. Catching up; huddle at {clock_label(self.cfg['schedule']['premarket'])}.")
        bus.emit("crew", now, desk=desks[0], phase="say", who="agent", to="all",
                 text=f"Morning. Catch up at your desks; huddle at {clock_label(self.cfg['schedule']['premarket'])}.")
        for k in desks:
            if k in INFO_ONLY:                      # read from Macro's brief at the huddle
                continue
            if self._inline():
                await self._prepare(k, now)
            else:
                self.prep_tasks[k] = asyncio.create_task(self._prepare(k, now))

    async def _prepare(self, key: str, now: float) -> dict:
        try:
            b = await self._brief(key, "premarket", now)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.warning("crew %s catch-up failed: %s", key, ex)
            b = self._finish(self._offline_brief(key, now, "premarket"), key)
        b["prepared_ts"] = now
        self.prep[key] = b
        self.prep_tasks.pop(key, None)
        self.e.bus.emit("crew", self.e.feed.now(), desk=key, phase="say", who=key,
                        text=f"{DESKS[key].name}: ready for the huddle.")
        return b

    def _drop_prep(self) -> None:
        for t in self.prep_tasks.values():
            t.cancel()
        self.prep, self.prep_tasks = {}, {}

    async def _huddle_brief(self, key: str, slot: str, now: float) -> dict:
        """The premarket huddle uses what each desk prepared at arrival. A desk still researching attends with its
        offline read, so the huddle never slips past the open."""
        if slot == "premarket" and key in self.prep:
            return self.prep.pop(key)
        if slot == "premarket" and key in self.prep_tasks:
            self.prep_tasks.pop(key).cancel()
            b = self._finish(self._offline_brief(key, now, slot), key)
            b["notes"] = ["Still researching at huddle time; using my offline read."] + list(b.get("notes") or [])
            return b
        return await self._brief(key, slot, now)

    def _inline(self) -> bool:
        """Huddles run on the engine's clock only in the simulator, so a sim day replays the same way. Paper and live
        always queue them (online or not): a huddle's Robinhood reads (VIX, the earnings calendar) can each wait up to
        15 s, and the engine's one-second loop (exits, books, watchdog) must not wait with them."""
        return self.offline and self.e.feed.is_sim

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)                             # asyncio keeps only a weak reference to a running task
        t.add_done_callback(self._bg.discard)
        return t

    async def _dispatch(self, slot: str, desks: list[str], now: float) -> None:
        if self._inline():
            await self._consult(slot, desks, now)
        else:
            await self.queue.put((slot, desks))
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while not self.queue.empty():
            slot, desks = await self.queue.get()
            try:
                await self._consult(slot, desks, self.e.feed.now())
            except Exception as ex:
                log.exception("crew consult failed")
                self.e.bus.emit("log", self.e.feed.now(), level="warn", msg=f"crew {slot} failed: {ex}")

    # ------------------------------------------------------------ huddle
    async def _consult(self, slot: str, desks: list[str], now: float) -> None:
        e, bus = self.e, self.e.bus
        e.set_agent(now, "consulting", f"{slot.replace('-', ' ').title()} huddle: {', '.join(DESKS[d].name for d in desks)}")
        for i, k in enumerate(desks):
            bus.emit("crew", now, desk=k, phase="walk", slot=slot, spot=i)
        pitched = []
        for k in desks:
            b = await self._huddle_brief(k, slot, now)
            self.briefs[k] = {**b, "ts": now, "slot": slot}
            e.journal.record_brief(str(self.day), now, k, b)
            bus.emit("crew", now, desk=k, phase="say", who=k, to="agent", text=b.get("headline", ""))
            for note in (b.get("notes") or [])[:1]:
                bus.emit("crew", now, desk=k, phase="say", who=k, to="agent", text=note)
            pitched += [{**p, "desk": k} for p in b.get("proposals") or []]
        talk = await self._roundtable(slot, desks, now)
        for ln in [x for x in talk.get("lines") or [] if isinstance(x, dict)][:6]:
            frm, to = ln.get("from"), ln.get("to", "agent")
            if frm in DESKS and ln.get("text"):
                bus.emit("crew", now, desk=frm, phase="say", who=frm, to=to, text=str(ln["text"])[:140])
        revised = {}
        for k, v in (talk.get("votes") or {}).items():
            if k in desks and k in self.briefs:         # only desks at this huddle can change their vote
                try:
                    self.briefs[k]["size_multiplier"] = revised[k] = self._clamp(v, k)
                except (TypeError, ValueError):
                    continue
        for p in pitched + [x for x in talk.get("proposals") or [] if isinstance(x, dict)]:
            if p.get("desk", desks[0]) in RESTRICT_ONLY + INFO_ONLY:
                continue
            await self._handle_proposal(p.get("desk", desks[0]), p, now)
        self._apply(now)
        self._log(now, "directive", None, {"slot": slot, "desks": desks, "votes": self.directive.get("votes", {}),
                                           "book_mults": self.directive.get("book_mults", {}), "revised": revised,
                                           "blackouts": self.directive.get("blackouts", [])})
        if slot == "postclose":
            self._log(now, "usage", None, self.cache_usage)
        bus.emit("crew", now, desk=desks[0], phase="say", who="agent", to="all", text=self._wrap_line(slot))
        for k in desks:
            bus.emit("crew", now, desk=k, phase="done", brief=self.briefs.get(k))
        e.set_agent(now, "watching" if is_rth(now) else ("coffee" if ct_time(now) < time(8, 30) else "offline"),
                    self.directive["summary"])

    def _wrap_line(self, slot: str) -> str:
        v = self.directive.get("votes", {})
        cut = [k for k, m in v.items() if m < 1.0]
        up = [k for k in VOTERS_FOR_SIZE_UP if v.get(k, 1.0) > 1.0]
        both = " and ".join(DESKS[k].name for k in VOTERS_FOR_SIZE_UP)
        if self.e.risk.st.halted:
            return "Done for the day. Thanks, team."
        if slot == "postclose":
            back = self.cfg["schedule"].get("arrive") or self.cfg["schedule"]["premarket"]
            return f"Good session. Journal's saved; see everyone at {clock_label(back)}."
        if cut:
            return f"Size to {int(min(self.directive.get('book_mults', {'A': 1.0}).values()) * 100)}% on {', '.join(DESKS[k].name for k in cut)}'s call."
        if len(up) == len(VOTERS_FOR_SIZE_UP):
            return f"{both} both lean long. Size-up is on the table if the tape confirms."
        if up:
            return f"{', '.join(DESKS[k].name for k in up)} want more size. Not enough agreement; staying at 100%."
        return "Rules unchanged. Back to the screens."

    def _clamp(self, v, key: str | None = None) -> float:
        if key in INFO_ONLY:                        # Fed and Rates inform; they don't vote
            return 1.0
        hi = 1.0 if key in RESTRICT_ONLY else self.cfg.get("max_size_multiplier", 1.25)
        return max(self.cfg["min_size_multiplier"], min(hi, _num(v, 1.0)))

    # ------------------------------------------------------------ votes by book
    def _routes(self, desk: str) -> list[str]:
        vb = self.cfg.get("vote_books")
        vb = vb if isinstance(vb, dict) else DEFAULT_VOTE_BOOKS
        if desk in INFO_ONLY:
            return []
        return list(vb[desk] or []) if desk in vb else ["A"]

    def _votes(self) -> dict[str, float]:
        return {k: b.get("size_multiplier", 1.0) for k, b in self.briefs.items() if b.get("day") == str(self.day)}

    def cut_by(self, book: str) -> list[str]:
        """Desks whose vote is cutting this book's size today."""
        return [k for k, m in self._votes().items() if m < 1.0 and book in self._routes(k)]

    def book_mults(self) -> dict[str, float]:
        votes = self._votes()
        return {bk: min([1.0] + [m for k, m in votes.items() if m < 1.0 and bk in self._routes(k)]) for bk in BOOK_LETTERS}

    def effect(self, book: str, qty: int, qty_1x: int, up: float = 1.0, tweaks: dict | None = None) -> dict:
        """What the crew did to one entry, saved with the trade for the weekly scorecard."""
        return {"mult": self.e.risk.book_mult(book), "qty": qty, "qty_1x": qty_1x, "up": up,
                "cut_by": self.cut_by(book), "tweaks": dict(tweaks or {})}

    # ------------------------------------------------------------ crew log
    def _log(self, now: float, kind: str, book: str | None, detail: dict) -> None:
        rec = getattr(self.e.journal, "record_crew", None)
        if rec is None:
            return
        try:
            rec(str(session_date(now)), now, kind, book, json.loads(json.dumps(detail, default=str)))
        except Exception as ex:
            log.warning("crew log %s failed: %s", kind, ex)

    def note_block(self, book: str, why: str, now: float) -> None:
        """An entry a crew blackout or the VIX1D flag stopped; logged once per book and reason per day."""
        key = (str(session_date(now)), book, why)
        if key in self._logged:
            return
        self._logged.add(key)
        if "VIX1D" in why:
            desk = "vol"
        else:
            name = why.split(": ", 1)[1] if ": " in why else why
            desk = self._event_src.get(name, "config")
        self._log(now, "block", book, {"reason": why, "desk": desk})

    def _apply(self, now: float) -> None:
        votes = self._votes()
        mults = self.book_mults()
        self.e.risk.set_book_mults(mults)
        for k, b in self.briefs.items():
            if b.get("day") not in (None, str(self.day)):
                continue
            taken = self._brief_events.setdefault(k, set())
            for ev in b.get("events", []) or []:
                if ev.get("impact") == "high" and ev.get("time_ct"):
                    try:
                        ts = at_ct(self.day, hhmm(str(ev["time_ct"])))
                    except (TypeError, ValueError):
                        continue
                    if ts not in taken and len(taken) >= MAX_BRIEF_EVENTS:
                        if ("brief-cap", k) not in self._logged:
                            self._logged.add(("brief-cap", k))
                            self.e.bus.emit("log", now, level="warn", msg=f"{DESKS[k].name if k in DESKS else k} desk: more"
                                            f" than {MAX_BRIEF_EVENTS} events in today's briefs; ignoring {ev['name']} and later ones")
                        continue
                    taken.add(ts)
                    if ts > now:                    # risk.add_blackout's one-per-event-window rule is the only de-dup
                        self.e.risk.add_blackout(ts, ev["name"], added_ts=now, flatten=False)
                    else:                           # already out: book B's day-skip still sees it; it blocks nothing
                        self.e.risk.note_passed_event(ts, ev["name"])
                    self._event_src.setdefault(ev["name"], k)
            if b.get("cooldown_minutes") and b.get("slot") == "loss-review" and abs(b.get("ts", 0) - now) < 1:
                self.e.risk.extend_cooldown(now, float(b["cooldown_minutes"]))
        self._check_calendar(now)
        biases = [b.get("bias") for k, b in self.briefs.items() if k in ("macro", "rates", "fed", "vol")]
        self.directive = {
            "size_mult": self.e.risk.st.size_mult, "votes": votes, "book_mults": mults,
            "blackouts": [x.name for x in self.e.risk.st.blackouts],
            "summary": f"Size {int(self.e.risk.st.size_mult * 100)}%"
                       + ("".join(f", {bk} {int(m * 100)}%" for bk, m in mults.items() if bk != "A" and m < 1.0)) + ". "
                       + (f"Blackouts: {', '.join(x.name for x in self.e.risk.st.blackouts)}. " if self.e.risk.st.blackouts else "")
                       + (f"Desk lean: {max(set(biases), key=biases.count)}." if biases else ""),
        }
        self.e.bus.emit("directive", now, directive=self.directive, risk=self.e.risk.to_dict())

    # ------------------------------------------------------------ size-up gate
    def size_up(self, now: float, setup: str, spot: float) -> tuple[float, list[dict]]:
        """All checks must pass for size > 100%. Returns (multiplier, checklist)."""
        e, st = self.e, self.e.risk.st
        day = str(self.day)
        chk = []

        def add(name, ok, detail=""):
            chk.append({"name": name, "ok": bool(ok), "detail": detail})

        for k in VOTERS_FOR_SIZE_UP:
            b = self.briefs.get(k) or {}
            fresh = b.get("day") == day
            add(f"{DESKS[k].name} votes up", fresh and b.get("size_multiplier", 1.0) > 1.0 and b.get("confidence", 0) >= 0.6,
                f"{b.get('size_multiplier', 1.0):.2f}x, conf {b.get('confidence', 0):.1f}" if fresh else "no brief today")
        fb = self.briefs.get("fed") or {}
        add("Fed not hawkish", fb.get("day") == day and fb.get("bias") != "bearish" and fb.get("size_multiplier", 1.0) >= 1.0,
            fb.get("bias", "no brief"))
        lows = self.cut_by("A")
        add("No desk voting down", not lows, ", ".join(DESKS[k].name for k in lows))
        soon = [b for b in st.blackouts if b.start - 3600 <= now < b.end]
        add("No high-impact event within 60m", not soon, soon[0].name if soon else "")
        vw = e.vwap.value
        add("SPY above VWAP", vw is not None and spot > vw, f"{spot:.2f} vs {vw:.2f}" if vw else "")
        add("15m MACD histogram rising", e.sig.hist_rising("15m"))
        if e.l2 and e.l2.enabled and e.l2.latest is not None:
            ok, why = e.l2.gate("call", spot, now)
            add("Level 2 not leaning against", ok, why)
        add("Green on the day, no loss streak", st.day_pnl >= 0 and st.loss_streak == 0, f"${st.day_pnl:+.0f}, streak {st.loss_streak}")
        add("SWING setup before 13:30 CT", setup == "SWING" and ct_time(now) < time(13, 30), setup)
        ok = all(c["ok"] for c in chk) and st.size_mult >= 1.0
        mult = min([self.cfg.get("max_size_multiplier", 1.25)] + [self.briefs[k]["size_multiplier"] for k in VOTERS_FOR_SIZE_UP]) if ok else 1.0
        self.conviction = {"mult": mult, "checks": chk, "ts": now}
        return mult, chk

    # ------------------------------------------------------------ proposals
    def known_events(self, now: float) -> list[dict]:
        """Scheduled events a suggestion can be tied to, each with the end of its blackout window: today's
        blackouts, today's brief events, and today's or later events from the weekly calendar and config."""
        after = self.e.cfg["risk"]["event_blackout"]["after_min"] * 60
        d = session_date(now)
        out = [{"name": b.name, "end": b.end} for b in self.e.risk.st.blackouts]
        dated = [(ev.get("date"), ev.get("time_ct"), ev.get("name")) for ev in (self._calendar or {}).get("events") or []
                 if isinstance(ev, dict)]
        dated += [(ev.get("date"), ev.get("time"), ev.get("name")) for ev in self.cfg.get("events") or []]
        dated += [(str(d), ev.get("time_ct"), ev.get("name")) for b in self.briefs.values() if b.get("day") == str(d)
                  for ev in b.get("events") or [] if isinstance(ev, dict)]
        for day, hm_, name in dated:
            try:
                ed = date.fromisoformat(str(day))
                if ed >= d and name:
                    out.append({"name": str(name), "end": at_ct(ed, hhmm(str(hm_))) + after})
            except (TypeError, ValueError):
                continue
        return out

    def expire_proposals(self, now: float) -> list[dict]:
        """Clear suggestions whose reason has passed (proposals.py) and tell the dashboard."""
        self._last_expire = now
        gone = self.book.expire(now)
        for item in gone:
            self.e.bus.emit("proposal", now, item=item)
        return gone

    async def _handle_proposal(self, desk: str, p: dict, now: float) -> None:
        item = self.book.submit(desk, _clean_pitch(p), now, events=self.known_events(now))
        if not item:
            return
        self.e.bus.emit("proposal", now, item=item)
        if item["status"] == "applied":
            self.e.apply_tweak(item, now)
            self.e.bus.emit("crew", now, desk=desk, phase="say", who="agent", to=desk,
                            text=f"Applied for {'the next trade' if item['scope'] == 'trade' else 'today'}: {item['title']}")
        elif item["status"] == "pending":
            self.e.bus.emit("crew", now, desk=desk, phase="say", who="agent", to=desk,
                            text=f"Sent to Evan for approval: {item['title']}")

    # ------------------------------------------------------------ briefs
    async def _brief(self, key: str, slot: str, now: float) -> dict:
        b = None
        if key == "risk":
            b = self._risk_brief()
        elif key == "tape":
            b = self._tape_brief()
        elif key == "ops":
            b = self._ops_brief(slot)
        elif key == "earnings":
            b = await self._earnings_brief(now)
        elif key == "postmortem":
            b = self._postmortem_brief(now)
        elif key == "quant":
            b = self._quant_brief(slot)
            if not self.offline:                    # the rules keep the vote, cooldown and pitches; Haiku adds notes
                llm = await self._llm(key, now, extra=json.dumps(b))
                if llm:
                    b["notes"] = (list(b.get("notes") or []) + [str(n) for n in llm.get("notes") or []])[:4]
        elif key in INFO_ONLY:
            b = self._derived_brief(key, now, slot)
        elif key == "vol" and not self.e.feed.is_sim:
            b = await self._vol_read(slot, now)
        elif not self.offline and DESKS[key].uses_web:
            b = await self._llm(key, now)
        if b is None:
            b = self._offline_brief(key, now, slot)
        return self._finish(b, key)

    def _finish(self, b: dict, key: str | None = None) -> dict:
        """Every brief leaves here with the shape the rest of the crew reads. An LLM reply can carry a null or text
        confidence, a null vote, or events and pitches that aren't objects; any of those used to raise in _apply or
        in size_up on every book A entry, which the engine counts toward its safety halt."""
        b["day"] = str(self.day)
        b["headline"] = CITE_TAG.sub("", str(b.get("headline") or ""))
        if b.get("bias") not in ("bullish", "neutral", "bearish"):
            b["bias"] = "neutral"
        b["confidence"] = max(0.0, min(1.0, _num(b.get("confidence"), 0.0)))
        notes = b.get("notes")
        b["notes"] = [CITE_TAG.sub("", str(n)) for n in (notes if isinstance(notes, list) else [notes] if notes else [])]
        b["events"] = ([{**ev, "name": CITE_TAG.sub("", str(ev["name"]))} for ev in b.get("events") or [] if _good_event(ev)]
                       if isinstance(b.get("events"), list) else [])
        props = b.get("proposals")
        b["proposals"] = [_clean_pitch(x) for x in props if isinstance(x, dict)] if isinstance(props, list) else []
        b["size_multiplier"] = self._clamp(b.get("size_multiplier", 1.0), key)
        b["cooldown_minutes"] = max(0, min(30, int(_num(b.get("cooldown_minutes"), 0))))
        if key in RESTRICT_ONLY + INFO_ONLY:    # inform (or cut) only: no pitches, no blackouts
            b["proposals"], b["events"] = [], []
        return b

    def _ops_brief(self, slot: str = "") -> dict:
        return xdesks.ops_brief(self, slot)

    async def _earnings_brief(self, now: float) -> dict:
        return await xdesks.earnings_brief(self, now)

    def _postmortem_brief(self, now: float) -> dict:
        return xdesks.postmortem_brief(self, now)

    def _risk_brief(self) -> dict:
        r = self.e.risk.to_dict()
        head = (f"Halted: {r['halt_reason']}" if r["halted"] else
                f"{r['loss_streak']} losses in a row. Cooldown to {ct(r['cooldown_until']).strftime('%H:%M')}"
                if r["cooldown_until"] and r["cooldown_until"] > self.e.feed.now()
                else f"Day P&L ${r['day_pnl']:+.0f}, {r['trades']}/{r['max_trades']} trades")
        return {"headline": head, "bias": "neutral", "confidence": 1.0, "events": [], "size_multiplier": 1.0,
                "notes": [f"Loss limit -${r['max_daily_loss']}, used ${max(0, -r['day_pnl']):.0f}"]}

    def _tape_brief(self) -> dict:
        st = self.e.l2.latest if self.e.l2 else None
        if st is None:
            return {"headline": "No book right now.", "bias": "neutral", "confidence": 0.3, "events": [], "notes": []}
        lean = "bid-heavy" if st.imbalance > 0.2 else "ask-heavy" if st.imbalance < -0.2 else "balanced"
        w = f" Wall {st.ask_wall[0]:.2f} x {st.ask_wall[1] / 1000:.0f}k overhead." if st.ask_wall else ""
        return {"headline": f"Book is {lean} ({st.imbalance:+.2f}).{w}", "bias": "bullish" if st.imbalance > 0.2 else "bearish" if st.imbalance < -0.2 else "neutral",
                "confidence": 0.4, "events": [], "size_multiplier": 1.0, "notes": [f"microprice {st.micro_edge_c:+.1f}c vs mid"]}

    def _quant_brief(self, slot: str = "") -> dict:
        tr = self.e.closed
        n = len(tr)
        if not n:
            return {"headline": "No trades yet today.", "bias": "neutral", "confidence": 0.5, "events": [], "notes": []}
        nets = [p.realized - p.fees for p in tr]
        wins = [x for x in nets if x >= 0]
        losses = [x for x in nets if x < 0]
        by_setup: dict = {}
        for p, x in zip(tr, nets):
            by_setup.setdefault(p.setup, []).append(x)
        streak = self.e.risk.st.loss_streak
        aw = sum(wins) / len(wins) if wins else 0.0
        al = sum(losses) / len(losses) if losses else 0.0
        notes = [f"{n} trades, {len(wins)}W/{len(losses)}L, net ${sum(nets):+.0f}"]
        if wins and losses:
            notes.append(f"avg win ${aw:.0f} vs avg loss ${al:.0f}")
        for s, xs in by_setup.items():
            notes.append(f"{s}: {len(xs)} trades, ${sum(xs):+.0f}")
        props = []
        if slot == "postclose":
            gave_back = [p for p in tr if p.peak >= p.entry * 1.35 and (p.exit_reason or "").startswith("trailing")]
            if len(gave_back) >= 2:
                props.append({"scope": "standing", "title": "Tighter runner trail on SWING (25% -> 20%)",
                              "rationale": f"{len(gave_back)} runners peaked above +35% today and gave most of it back on the 25% trail.",
                              "params": {"exits.swing.runner_trail_pct": 0.20},
                              "evidence": ", ".join(f"{p.contract.label} peak {p.peak:.2f} vs entry {p.entry:.2f}" for p in gave_back[:3])})
            return {"headline": f"Day done: {len(wins)}W/{len(losses)}L, net ${sum(nets):+.0f}", "bias": "neutral",
                    "confidence": 0.6, "events": [], "size_multiplier": 1.0, "notes": notes[1:4], "proposals": props}
        chop = streak >= 2 and (not wins or abs(al) >= aw * 0.9)
        scalp = by_setup.get("SCALP", [])
        if chop and len(scalp) >= 3 and sum(scalp) < 0 and "SWING" in by_setup:
            props.append({"scope": "day", "title": "Skip SCALP entries for the rest of today",
                          "rationale": f"SCALPs are {len(scalp)} trades, ${sum(scalp):+.0f}; the 144t is getting chopped.",
                          "params": {"strategy.enabled_setups": ["SWING"]}})
        if chop:
            head = f"{streak} straight losers, losses outsize wins. Voting 75% until the next winner."
        elif streak >= 2:
            head = f"{streak} losers in a row but winners still pay for them. Hold size."
        else:
            head = "Numbers look fine. Stay the course."
        return {"headline": head, "bias": "neutral", "confidence": 0.6, "events": [],
                "size_multiplier": 0.75 if chop else 1.0, "cooldown_minutes": 0, "notes": notes[:3], "proposals": props}

    def _mood_vote(self, key: str) -> tuple[str, float, float]:
        """Simulator only: a seeded 'day mood' so the demo exercises both size cuts and size-ups."""
        rng = random.Random(f"{self.day}-{key}")
        if self.mood == "risk_on" and key in ("macro", "rates", "vol"):
            return "bullish", round(rng.uniform(1.10, 1.25), 2), round(rng.uniform(0.6, 0.8), 2)
        if self.mood == "risk_off" and key in ("macro", "rates", "fed"):
            return "bearish", round(rng.uniform(0.8, 0.95), 2), round(rng.uniform(0.5, 0.7), 2)
        return "neutral", 1.0, 0.5

    def _offline_brief(self, key: str, now: float, slot: str = "") -> dict:
        tnow = ct_time(now)
        evs = [{"time_ct": e.get("time"), "name": e.get("name"), "impact": e.get("impact", "medium")}
               for e in getattr(self, "_config_events", []) if hhmm(e.get("time")) > tnow]
        sim = self.e.feed.is_sim
        tag = (" (sim)" if sim else " (offline: set ANTHROPIC_API_KEY for live research)" if self.offline
               else " (live research unavailable; offline read)")
        bias, vote, conf = self._mood_vote(key) if sim else ("neutral", 1.0, 0.4)
        if key == "macro":
            hi = [e for e in evs if e["impact"] == "high"]
            head = (f"{hi[0]['name']} at {hi[0]['time_ct']} CT. Flat into it." if hi else "Quiet calendar today.")
            if sim and bias == "bullish":
                head = ("Cooling prices in the ISM detail (sim). " + head)[:90]
            elif sim and bias == "bearish":
                head = ("Hot wage data overnight (sim). " + head)[:90]
            return {"headline": head + ("" if sim else tag), "bias": bias, "confidence": conf, "events": evs,
                    "size_multiplier": vote,
                    "notes": [f"No entries {self.e.cfg['risk']['event_blackout']['before_min']}m before to "
                              f"{self.e.cfg['risk']['event_blackout']['after_min']}m after; flat "
                              f"{self.e.cfg['risk']['flatten_before_high_impact_min']}m before"] if hi else []}
        if key == "rates":
            head = {"bullish": "2Y down 6bp, curve bull-steepening (sim)", "bearish": "10Y up 7bp into the auction (sim)"}.get(
                bias, "10Y range-bound, no auction today") + ("" if sim else tag)
            return {"headline": head, "bias": bias, "confidence": conf, "events": [], "size_multiplier": vote, "notes": []}
        if key == "fed":
            fed = [e for e in evs if "fed" in (e["name"] or "").lower() or "fomc" in (e["name"] or "").lower()]
            return {"headline": (f"{fed[0]['name']} at {fed[0]['time_ct']} CT" if fed else "Nothing else from the Fed today")
                    + ("" if sim else tag), "bias": bias, "confidence": conf, "events": fed, "size_multiplier": vote, "notes": []}
        if key == "ops":
            return self._ops_brief(slot)
        if key == "postmortem":
            return self._postmortem_brief(now)
        if key == "earnings":        # the huddle couldn't wait for the calendar: use today's cached copy or config
            cal = getattr(self, "_earnings_cal", None)
            return {"headline": "Calendar still loading; screen after the huddle." if not cal else
                    f"E screen from {cal[2]}.", "bias": "neutral", "confidence": 0.3, "events": [], "notes": []}
        if key == "vol":
            b = self._vol_brief(tag)
            if sim and bias == "bullish":
                b.update(bias="bullish", confidence=conf, size_multiplier=vote,
                         headline=b["headline"].replace(" (sim)", "") + ", VIX sliding (sim)")
            if slot == "postclose" or (sim and slot == "premarket"):
                b["proposals"] = [self._vrp_pitch()]
            return b
        return {"headline": "n/a", "bias": "neutral", "confidence": 0.0, "events": [], "notes": []}

    def _vrp_pitch(self) -> dict:
        return {"scope": "new_strategy", "title": "Defined-risk 0DTE iron fly on non-event days",
                "rationale": "Our call-buying signals show no significant edge; the premium seller's side of 0DTE does.",
                "spec": "At 08:45 CT on days with no high-impact event: sell the ATM SPY straddle, buy wings about 1 expected "
                        "move away (iron butterfly). Size to max loss = 1% of account. Close at 50% of max profit, at 2x "
                        "credit loss, or 14:45 CT. Skip if VIX1D > VIX + 3.",
                "evidence": "S&P 500 1m data 2005-2020 + SPY 2025-26: open-to-close move averaged 0.40-0.55x the VIX-implied "
                            "day; modeled iron fly +8 to +26 bp/day, t 5.5-15 across all 3 periods. Model-priced; needs a "
                            "backtest on real option prices before any capital."}

    def _vol_brief(self, tag: str) -> dict:
        closes = [b["c"] for b in list(self.e.bars["1m"])] or []
        rv = None
        if len(closes) > 30:
            rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
            mu = sum(rets) / len(rets)
            rv = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(390 * 252)
        px = self.e.price or 0
        iv = getattr(self.e.feed, "base_iv", None)
        vol = iv or rv or 0.16
        em = px * vol * math.sqrt(1 / 252)
        mult = 0.75 if vol > 0.28 else 1.0
        return {"headline": f"Expected move ±${em:.2f} ({vol * 100:.0f} vol)" + tag, "bias": "neutral", "confidence": 0.5,
                "events": [], "size_multiplier": mult, "em": round(em, 2),
                "notes": [f"realized 1m vol {rv * 100:.0f}%" if rv else "realized vol warming up",
                          "vol > 28: size cut to 75%" if mult < 1 else "vol normal"]}

    # ------------------------------------------------------------ roundtable
    async def _roundtable(self, slot: str, desks: list[str], now: float) -> dict:
        if not self.offline and len([d for d in desks if d not in ("risk", "tape") + RESTRICT_ONLY + INFO_ONLY]) >= 2:
            out = await self._llm_roundtable(slot, desks, now)
            if out:
                return out
        return self._templated_roundtable(slot, desks, now)

    def _templated_roundtable(self, slot: str, desks: list[str], now: float) -> dict:
        b = {k: self.briefs.get(k, {}) for k in desks}
        lines, votes = [], {}
        ev = next((e for k in ("macro", "fed") for e in (b.get(k) or {}).get("events", []) if e.get("impact") == "high"), None)
        if "macro" in b and "rates" in b:
            if ev:
                lines.append({"from": "rates", "to": "macro", "text": f"If {ev['name']} surprises, the 2-year reprices first. Anything on consensus?"})
                lines.append({"from": "macro", "to": "rates", "text": "Consensus is tight, so the miss is what matters. I'm not voting size up into it."})
                if b["macro"].get("size_multiplier", 1) > 1.0:
                    votes["macro"] = 1.0
            elif b["macro"].get("bias") == b["rates"].get("bias") == "bullish":
                lines.append({"from": "rates", "to": "macro", "text": "Yields are easing with your data. Same read: I'm voting up."})
                lines.append({"from": "macro", "to": "vol", "text": "Vol, do you see it too, or is this a head fake?"})
            elif b["macro"].get("bias") != b["rates"].get("bias"):
                lines.append({"from": "rates", "to": "macro", "text": f"I don't buy the {b['macro'].get('bias')} read. Bonds aren't confirming it."})
                lines.append({"from": "macro", "to": "rates", "text": "Fair. Until yields agree I'll hold my vote at 100%."})
                if b["macro"].get("size_multiplier", 1) > 1.0:
                    votes["macro"] = 1.0
            else:
                lines.append({"from": "rates", "to": "macro", "text": "Nothing on your calendar to move my curve. Quiet day in bonds."})
        if "fed" in b and "vol" in b:
            em = b["vol"].get("em")
            lines.append({"from": "fed", "to": "vol", "text": f"Is the {ev['time_ct']} window priced in?" if ev else "Nothing from the Fed today. Is vol selling off?"})
            lines.append({"from": "vol", "to": "fed", "text": f"Straddle implies ±${em:.2f}. " + ("Normal for this tape." if b['vol'].get('size_multiplier', 1) >= 1 else "Rich. I'm cutting.") if em else "Priced normally."})
        elif "vol" in b and "macro" in b and "fed" not in b:
            up = b["vol"].get("size_multiplier", 1) > 1.0
            lines.append({"from": "vol", "to": "macro", "text": "VIX is bleeding lower. Trend days like this favor the runners." if up else "Realized is tracking implied. No edge from my side."})
        if "quant" in b and "risk" in b:
            lines.append({"from": "risk", "to": "quant", "text": "What's actually breaking: entries or exits?"})
            losers = [p for p in self.e.closed[-3:] if p.realized - p.fees < 0]
            stops = sum(1 for p in losers if (p.exit_reason or "").startswith("stop"))
            lines.append({"from": "quant", "to": "risk", "text": f"{stops} of the last {len(losers)} losers hit the -{self.e.cfg['exits']['stop_loss_pct'] * 100:.0f}% stop inside minutes. That's entry timing, not exits."
                          if losers else "Nothing broken. Variance."})
        if "tape" in b and "quant" in b:
            lines.append({"from": "tape", "to": "quant", "text": b["tape"].get("headline", "")[:110]})
        if "quant" in b and "vol" in b and slot == "postclose":
            lines.append({"from": "vol", "to": "quant", "text": "Straddles overpriced the move again today. That's the edge we keep paying for."})
            lines.append({"from": "quant", "to": "vol", "text": "Then pitch it properly. Put it in front of Evan with the numbers."})
        lines += xdesks.templated_lines(slot, b)
        return {"lines": lines, "votes": votes, "proposals": []}

    async def _llm_roundtable(self, slot: str, desks: list[str], now: float) -> dict | None:
        client = self._get_client()
        if client is None:
            return None
        briefs = {k: {kk: v for kk, v in self.briefs.get(k, {}).items() if kk in ("headline", "bias", "confidence", "size_multiplier", "notes", "events")}
                  for k in desks}
        user = (f"{ct(now).strftime('%A %H:%M')} CT, slot '{slot}'. SPY {self.e.price}. Engine today: "
                f"{self.e.risk.st.trades} trades, day P&L ${self.e.risk.st.day_pnl:+.0f}.\nBriefs:\n{json.dumps(briefs)}")
        try:
            msg = await asyncio.wait_for(client.messages.create(
                model=self.cfg["fast_model"], max_tokens=900,
                system=cached_system(ROUNDTABLE_HINT + "\n\n" + SCHEMA_HINT.split("proposals (optional")[1]),
                messages=[{"role": "user", "content": user}]), timeout=60)
        except Exception as ex:
            self.e.bus.emit("log", now, level="warn", msg=f"roundtable failed: {ex}")
            return None
        self.note_usage(msg, "roundtable", self.cfg["fast_model"])
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        return _extract_json(text, key="lines")

    # ------------------------------------------------------------ LLM briefs
    def _get_client(self):
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError:
                return None
            self._client = AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def _llm(self, key: str, now: float, extra: str = "", extra_label: str = "Engine stats JSON") -> dict | None:
        client = self._get_client()
        if client is None:
            return None
        desk = DESKS[key]
        books = self._routes(key)
        reach = (f"Your size vote applies to books {', '.join(books)}." if books
                 else "Your size vote applies to no book: you inform only.")
        ctx = (f"Today is {ct(now).strftime('%A %Y-%m-%d')}, {ct(now).strftime('%H:%M')} CT. SPY last {self.e.price}. "
               f"{BOOKS_TEXT} {reach}")
        system = (f"You are the {desk.name} desk on a small options trading team. Your beat: {desk.role} "
                  f"Be terse and numeric. Use web search for today's facts; never invent data. If you cannot verify "
                  f"something, leave it out.\n\n{SCHEMA_HINT}")
        user = ctx + (f"\n\n{extra_label}:\n{extra}" if extra else "") + f"\n\nGive the {desk.name} brief."
        model = self.cfg["fast_model"] if key == "quant" else self.cfg["model"]
        kwargs = dict(model=model, max_tokens=DESK_MAX_TOKENS,
                      system=cached_system(system), messages=[{"role": "user", "content": user}])
        if desk.uses_web and self.cfg.get("web_search", True):
            kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]
        try:
            msg = await asyncio.wait_for(client.messages.create(**kwargs), timeout=120)
        except Exception as ex:
            self.e.bus.emit("log", now, level="warn", msg=f"{desk.name} desk LLM call failed: {ex}")
            return None
        self.note_usage(msg, key, model)
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        cut = getattr(msg, "stop_reason", None) == "max_tokens"      # a cut-off reply is no brief, even if part parses
        b = None if cut else _extract_json(text)
        if b is None:
            why = f"stop_reason={getattr(msg, 'stop_reason', None)}, {len(text)} chars of text"
            log.warning("crew %s: no JSON brief in the reply (%s); using the offline read", key, why)
            self.e.bus.emit("log", now, level="warn", msg=f"{desk.name} desk reply had no brief ({why})")
        return b

    @staticmethod
    def _new_usage() -> dict:
        return {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "web_searches": 0,
                "est_cost_usd": 0.0, "by_desk": {}}

    def note_usage(self, msg, desk: str = "other", model: str | None = None) -> None:
        """Tally the day's tokens, web searches and estimated cost, in total and per desk (a cache_read of 0 on repeat
        calls means a cache miss). The cost is an estimate from list prices (PRICES, WEB_SEARCH_USD)."""
        u = getattr(msg, "usage", None)
        n = {"calls": 1}
        for k, attr in (("input", "input_tokens"), ("output", "output_tokens"), ("cache_read", "cache_read_input_tokens"),
                        ("cache_write", "cache_creation_input_tokens")):
            n[k] = int(getattr(u, attr, 0) or 0)
        n["web_searches"] = int(getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0)
        pin, pout = price_of(model or getattr(msg, "model", None))
        n["est_cost_usd"] = ((n["input"] * pin + n["output"] * pout + n["cache_read"] * pin * 0.1
                              + n["cache_write"] * pin * 1.25) / 1e6 + n["web_searches"] * WEB_SEARCH_USD)
        c = self.cache_usage
        d = c["by_desk"].setdefault(desk, {k: 0 for k in n})
        for k, v in n.items():
            c[k] += v
            d[k] += v
        log.debug("crew usage: %s", c)

    # ------------------------------------------------------------ Fed and Rates, read from Macro
    def _derived_brief(self, key: str, now: float, slot: str) -> dict:
        m = self.briefs.get("macro") or {}
        sub = m.get(key) if m.get("day") == str(self.day) else None
        if not isinstance(sub, dict):
            return self._offline_brief(key, now, slot)
        b = {"headline": str(sub.get("headline") or ("Nothing from the Fed today" if key == "fed" else "No read on Treasuries"))[:90],
             "bias": "neutral", "confidence": 0.5, "events": [], "size_multiplier": 1.0,
             "notes": [str(x) for x in sub.get("notes") or []][:4], "source": "macro"}
        if key == "fed":
            fb = str(sub.get("bias") or "").lower()
            b["bias"] = {"hawkish": "bearish", "dovish": "bullish"}.get(fb, "neutral")
            b["fed_bias"] = fb or "neutral"
        return b

    # ------------------------------------------------------------ Vol, from Robinhood data
    def _vix_source(self):
        if self.vix is None:
            from .books.vol import RobinhoodVix, SimVix
            if self.e.feed.is_sim:
                self.vix = SimVix(self.e.feed)
            elif getattr(self.e, "l2_rh", None) is not None:
                self.vix = RobinhoodVix(self.e.l2_rh)
        return self.vix

    async def _vix(self, what: str) -> float | None:
        src = self._vix_source()
        if src is None:
            return None
        day = session_date(self.e.feed.now())
        if what == "prev" and getattr(self, "_vix_prev", (None,))[0] == day:
            return self._vix_prev[1]                # one historicals call a day
        try:
            call = src.current() if what == "now" else src.prior_close(day)
            v = await asyncio.wait_for(call, timeout=15)
            v = float(v) if v else None
            if what == "prev" and v:
                self._vix_prev = (day, v)
            return v
        except Exception as ex:
            log.warning("vol desk: VIX %s unavailable: %s", what, ex)
            return None

    def _expected_move(self, now: float, vix: float | None) -> tuple[float | None, str]:
        spot = self.e.price
        if not spot:
            return None, ""
        if is_rth(now):
            fn = getattr(self.e.journal, "latest_straddle", None)
            try:
                got = fn(str(session_date(now)), float(spot), now - 120) if fn else None
            except Exception:
                got = None
            if got:
                return round(got[1], 2), f"{got[0]:g} straddle"
        if vix:
            m = ((self.e.cfg.get("books") or {}).get("D_iron_condor") or {}).get("em_rth_mult", 0.80)
            return round(spot * vix / 100 * math.sqrt(1 / 252) * m, 2), "VIX"
        return None, ""

    async def _vol_read(self, slot: str, now: float) -> dict:
        """Vol's numbers come from Robinhood: VIX now (prior close as fallback) and the expected move from the recorded
        0DTE ATM straddle (from VIX before the open). Robinhood has no VIX1D, so the premarket read makes one web call
        for vix1d_flag, the bias and the vote; later reads keep that vote (`vote_base`). Each read re-applies the VIX
        rule from the current VIX: above 28 cuts to 75%, and the cut lifts when VIX falls back."""
        vix_prev = await self._vix("prev")
        vix = await self._vix("now") or vix_prev
        em, src = self._expected_move(now, vix)
        prev = self.briefs.get("vol") or {}
        prev = prev if prev.get("day") == str(self.day) else {}
        parts = [f"VIX {vix:.2f}" if vix else "VIX n/a"]
        if vix_prev:
            parts.append(f"prior close {vix_prev:.2f}")
        if em:
            parts.append(f"{'straddle implies' if src.endswith('straddle') else 'expected move'} ±${em:.2f}"
                         + (" for the rest of today" if src.endswith("straddle") else ""))
        b = {"headline": ", ".join(parts)[:90], "bias": prev.get("bias", "neutral"),
             "confidence": prev.get("confidence", 0.5), "events": [],
             "size_multiplier": prev.get("vote_base", prev.get("size_multiplier", 1.0)),
             "em": em, "vix": vix, "vix_prev": vix_prev, "notes": [f"Source: Robinhood VIX, {src or 'no'} expected move"]}
        if "vix1d_flag" in prev:
            b["vix1d_flag"] = prev["vix1d_flag"]
        if slot == "premarket" and not self.offline:
            facts = f"Robinhood: {', '.join(parts)}."
            llm = await self._llm("vol", now, extra=facts, extra_label="Numbers already known")
            if llm:
                b.update({k: llm[k] for k in ("headline", "bias", "confidence", "size_multiplier", "vix1d_flag") if k in llm})
                b["notes"] = ([str(x) for x in llm.get("notes") or []] + b["notes"])[:4]
        b["vote_base"] = b["size_multiplier"]
        if vix and vix > 28:
            b["size_multiplier"] = min(float(b.get("size_multiplier") or 1.0), 0.75)
            b["headline"] = f"VIX {vix:.1f} is above 28: cutting size to 75%."
        return b

    # ------------------------------------------------------------ weekly economic calendar
    def _calendar_path(self) -> Path:
        return Path(expand(self.cfg.get("calendar_path", "~/.agentdesk/econ_calendar.json")))

    async def _ensure_calendar(self, now: float) -> None:
        """One web call a week: the week's scheduled US events, saved to crew.calendar_path. Its high-impact events
        today become blackouts next to Macro's (the union: blackouts only restrict)."""
        if self.e.feed.is_sim:                      # the simulator's days are made up; its events come from the feed
            return
        d = session_date(now)
        monday = d - timedelta(days=d.weekday())
        path, cal = self._calendar_path(), None
        try:
            data = json.loads(path.read_text())
            cal = data if isinstance(data, dict) and data.get("week") == str(monday) else None
        except (OSError, ValueError):
            pass
        if cal is None and not self.offline and not self.e.feed.is_sim:
            events = await self._llm_calendar(now, monday)
            if events is not None:
                cal = {"week": str(monday), "fetched_ts": now, "events": events}
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(cal, indent=1))
                except OSError as ex:
                    log.warning("calendar save failed: %s", ex)
        self._calendar = cal
        if not cal:
            return
        for ev in self._calendar_today(d):
            if ev["impact"] == "high":              # one blackout per event window (risk.add_blackout), not per name
                self.e.risk.add_blackout(at_ct(d, hhmm(ev["time_ct"])), ev["name"], added_ts=now)
                self._event_src.setdefault(ev["name"], "calendar")

    def _calendar_today(self, d) -> list[dict]:
        out = []
        for ev in (self._calendar or {}).get("events") or []:
            try:
                if str(ev.get("date")) == str(d) and ev.get("name") and ev.get("time_ct"):
                    hhmm(str(ev["time_ct"]))
                    out.append({"time_ct": str(ev["time_ct"]), "name": str(ev["name"])[:60],
                                "impact": str(ev.get("impact") or "medium").lower()})
            except (TypeError, ValueError):
                continue
        return out

    async def _llm_calendar(self, now: float, monday) -> list[dict] | None:
        client = self._get_client()
        if client is None:
            return None
        system = ("You keep the economic calendar for a small SPY options desk. List every scheduled US event for the"
                  " week that can move SPY intraday: data releases (CPI, PPI, jobs, claims, ISM, retail sales, GDP, PCE,"
                  " JOLTS, sentiment), FOMC decisions, minutes and pressers, Fed speakers, and Treasury auctions. Times"
                  " in US Central time (CT), not ET. Use web search; never invent an event. Reply with ONLY JSON:"
                  ' {"events": [{"date": "YYYY-MM-DD", "time_ct": "HH:MM", "name": "...", "impact": "high" | "medium" | "low"}]}.'
                  " high = CPI, PPI, NFP, PCE, retail sales, ISM, FOMC decision or presser, the Fed chair speaking.")
        user = f"The week of Monday {monday} to Friday {monday + timedelta(days=4)}. Today is {ct(now).strftime('%A %Y-%m-%d')}."
        kwargs = dict(model=self.cfg["model"], max_tokens=DESK_MAX_TOKENS, system=cached_system(system),
                      messages=[{"role": "user", "content": user}])
        if self.cfg.get("web_search", True):
            kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}]
        try:
            msg = await asyncio.wait_for(client.messages.create(**kwargs), timeout=180)
        except Exception as ex:
            self.e.bus.emit("log", now, level="warn", msg=f"weekly calendar call failed: {ex}")
            return None
        self.note_usage(msg, "calendar", self.cfg["model"])
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        got = _extract_json(text, key="events")
        if got is None or not isinstance(got.get("events"), list):
            self.e.bus.emit("log", now, level="warn", msg="weekly calendar reply had no events list")
            return None
        return [e for e in got["events"] if isinstance(e, dict)]

    def _check_calendar(self, now: float) -> None:
        """Compare today's high-impact events in the weekly calendar with Macro's; log any disagreement."""
        m = self.briefs.get("macro") or {}
        if not self._calendar or m.get("day") != str(self.day):
            return
        cal = [e for e in self._calendar_today(self.day) if e["impact"] == "high"]
        mac = [e for e in m.get("events") or [] if e.get("impact") == "high" and e.get("time_ct") and e.get("name")]

        def near(a, b):
            try:
                ta, tb = hhmm(str(a["time_ct"])), hhmm(str(b["time_ct"]))
            except (TypeError, ValueError):
                return False
            return abs((ta.hour * 60 + ta.minute) - (tb.hour * 60 + tb.minute)) <= 15

        only_cal = [f"{e['name']} {e['time_ct']}" for e in cal if not any(near(e, x) for x in mac)]
        only_mac = [f"{e['name']} {e['time_ct']}" for e in mac if not any(near(e, x) for x in cal)]
        key = (tuple(only_cal), tuple(only_mac))
        if key == self._cal_check:
            return
        self._cal_check = key
        if only_cal or only_mac:
            self._log(now, "calendar_check", None, {"only_calendar": only_cal, "only_macro": only_mac})
            self.e.bus.emit("log", now, level="warn", msg="calendar check: weekly calendar and Macro disagree: "
                            + "; ".join([f"only in calendar: {x}" for x in only_cal] + [f"only from Macro: {x}" for x in only_mac]))

    def state(self) -> dict:
        return {"briefs": self.briefs, "directive": self.directive, "offline": self.offline, "cache_usage": self.cache_usage,
                "desks": {k: {"name": d.name} for k, d in DESKS.items()}, "conviction": self.conviction,
                "proposals": [i for i in self.book.items if i["status"] != "expired"][-30:]}


def _num(v, default: float) -> float:
    """A number from an LLM field; null, text or NaN -> default."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _clean_pitch(p: dict) -> dict:
    """A pitch without web-search citation markup in its text (the dashboard card, and the 12-hour re-pitch check
    compares titles)."""
    return {**p, **{k: CITE_TAG.sub("", p[k]) for k in ("title", "rationale", "evidence", "spec") if isinstance(p.get(k), str)}}


def _good_event(ev) -> bool:
    """An event object with a name, and a time_ct that parses when it has one."""
    if not isinstance(ev, dict) or not ev.get("name"):
        return False
    try:
        if ev.get("time_ct") is not None:
            hhmm(str(ev["time_ct"]))
    except (TypeError, ValueError):
        return False
    return True


def _extract_json(text: str, key: str = "headline") -> dict | None:
    """The largest JSON object in the reply that has `key`. Macro's brief nests "fed" and "rates" objects that
    carry their own "headline", so the first or last match can be a nested one; the outermost is the brief. When a
    top-level object doesn't parse (the reply was cut off inside it), nothing nested inside it counts: a complete
    "fed" sub-brief must never stand in for a truncated Macro brief."""
    dec, best = json.JSONDecoder(), None
    broken = [(a, z) for a, z in _top_level_spans(text) if not _decodes(dec, text, a)]
    for m in re.finditer(r"\{", text):
        if any(a < m.start() < z for a, z in broken):
            continue
        try:
            obj, end = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and key in obj and (best is None or end - m.start() > best[0]):
            best = (end - m.start(), obj)
    return best[1] if best else None


def _decodes(dec, text: str, at: int) -> bool:
    try:
        dec.raw_decode(text, at)
        return True
    except json.JSONDecodeError:
        return False


def _top_level_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each brace-balanced top-level {...} in the text, strings inside braces skipped; an object
    that never closes runs to the end of the text."""
    spans, depth, start, in_str, esc = [], 0, 0, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"' and depth:
            in_str = True
        elif ch == "{":
            if not depth:
                if text[i + 1:i + 40].lstrip()[:1] not in ('"', "}"):
                    continue                        # a brace in prose ("{today}"), not the start of a JSON object
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if not depth:
                spans.append((start, i + 1))
    if depth:
        spans.append((start, len(text)))
    return spans
