"""The research crew: desks that brief the agent, argue with each other, vote on size and pitch ideas.

Desks: Macro (econ calendar), Rates (bonds/yields), Fed Watch, Vol (VIX/expected move),
Quant (reviews our own trades), Risk (deterministic), Tape (Level 2 book, deterministic).

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
from datetime import time

from .clock import at_ct, ct, ct_time, is_rth, session_date
from .config import expand, hhmm
from .proposals import ProposalBook

log = logging.getLogger("agentdesk.crew")


@dataclass
class Desk:
    key: str
    name: str
    role: str
    uses_web: bool = True


DESKS = {
    "macro": Desk("macro", "Macro", "US economic calendar and data surprises today: CPI, PPI, NFP, jobless claims, ISM, retail sales, GDP, PCE, consumer sentiment. Times in CT, consensus vs prior, and what a miss does to SPY."),
    "rates": Desk("rates", "Rates", "Treasury market: 2Y and 10Y yields and their change today, curve shape, Treasury auctions today, and whether rates are a headwind or tailwind for equities intraday."),
    "fed": Desk("fed", "Fed Watch", "Federal Reserve: FOMC decision/minutes/presser timing if today, Fed speakers scheduled today with CT times, fed-funds futures pricing shifts."),
    "vol": Desk("vol", "Vol", "Volatility: VIX level and change, VIX9D vs VIX term structure, SPY 0DTE expected move from the ATM straddle if available, and whether the tape favors trend or chop."),
    "quant": Desk("quant", "Quant", "Reviews the engine's own trades today: win rate, avg win/loss, which setup and time windows are working, whether the market is chopping the MACD triggers.", uses_web=False),
    "risk": Desk("risk", "Risk", "Deterministic risk manager.", uses_web=False),
    "tape": Desk("tape", "Tape", "Level 2 order book reader.", uses_web=False),
}
VOTERS_FOR_SIZE_UP = ("macro", "rates", "vol")

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
>= 0.6. A size-up only happens if Macro, Rates and Vol all vote above 1.0 and several market checks pass.
proposals (optional, usually empty): {"scope": "trade"|"day"|"standing"|"new_strategy", "title": "...",
 "rationale": "...", "params": {"<whitelisted key>": value}, "spec": "(new_strategy only) rules in plain words",
 "evidence": "numbers"}. Whitelisted keys: exits.stop_loss_pct, exits.swing.runner_trail_pct,
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

PREMARKET_DESKS = ["macro", "rates", "fed", "vol"]


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
        self.prep: dict[str, dict] = {}                     # briefs the premarket desks wrote at their desks
        self.prep_tasks: dict[str, asyncio.Task] = {}       # ... and the ones still researching
        self._client = None
        self.directive = {"size_mult": 1.0, "blackouts": [], "summary": "", "votes": {}}
        self.conviction = {"mult": 1.0, "checks": [], "ts": 0}
        path = None if engine.feed.is_sim else expand(cfg["journal_path"]).parent / "proposals.json"
        self.book = ProposalBook(cfg, path)
        self.mood = getattr(engine.feed, "mood", None)

    # ------------------------------------------------------------ schedule
    async def on_clock(self, now: float) -> None:
        if not self.enabled:
            return
        d = session_date(now)
        if d != self.day:
            self.day, self.ran = d, set()
            self._drop_prep()
            self._load_config_events(now)
        t = ct_time(now)
        sch = self.cfg["schedule"]
        if "arrive" in sch and "arrive" not in self.ran and t >= hhmm(sch["arrive"]) and ct(now).weekday() < 5:
            self.ran.add("arrive")
            if t < hhmm(sch["premarket"]):          # started after the huddle time: the huddle briefs in full
                await self._catch_up(PREMARKET_DESKS, now)
        plan = [("premarket", PREMARKET_DESKS), ("midday", ["vol", "rates", "macro"]),
                ("late", ["fed", "vol"]), ("postclose", ["quant", "vol"])]
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
                self.e.risk.add_blackout(at_ct(d, hhmm(ev["time"])), ev["name"])
        self._config_events = evs

    async def on_trade_closed(self, pos, net: float, now: float) -> None:
        st = self.e.risk.st
        q = self.briefs.get("quant")
        if net >= 0 and q and q.get("size_multiplier", 1.0) < 1.0:
            q["size_multiplier"] = 1.0
            q["headline"] = "Winner booked. My vote goes back to 100%."
            self.e.bus.emit("crew", now, desk="quant", phase="done", brief=q)
            self._apply(now)
        if net < 0 and st.loss_streak >= self.cfg.get("consult_quant_after_losses", 2):
            desks = ["quant", "risk"] + (["tape"] if self.e.l2 and self.e.l2.latest else [])
            await self._dispatch("loss-review", desks, now)
        if st.halted:
            await self._dispatch("halt", ["risk", "quant"], now)

    # ------------------------------------------------------------ premarket catch-up
    async def _catch_up(self, desks: list[str], now: float) -> None:
        """Desks arrive and research at their own desks. Their briefs wait for the premarket huddle.
        Online, each desk researches in its own task so a slow one can't hold up the others or the huddle."""
        e, bus = self.e, self.e.bus
        e.set_agent(now, "coffee", f"Team's in. Catching up; huddle at {clock_label(self.cfg['schedule']['premarket'])}.")
        bus.emit("crew", now, desk=desks[0], phase="say", who="agent", to="all",
                 text=f"Morning. Catch up at your desks; huddle at {clock_label(self.cfg['schedule']['premarket'])}.")
        for k in desks:
            if self.offline:
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
            b = self._finish(self._offline_brief(key, now, "premarket"))
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
            b = self._finish(self._offline_brief(key, now, slot))
            b["notes"] = ["Still researching at huddle time; using my offline read."] + list(b.get("notes") or [])
            return b
        return await self._brief(key, slot, now)

    async def _dispatch(self, slot: str, desks: list[str], now: float) -> None:
        if self.offline:
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
        for ln in talk.get("lines", [])[:6]:
            frm, to = ln.get("from"), ln.get("to", "agent")
            if frm in DESKS and ln.get("text"):
                bus.emit("crew", now, desk=frm, phase="say", who=frm, to=to, text=str(ln["text"])[:140])
        for k, v in (talk.get("votes") or {}).items():
            if k in self.briefs:
                self.briefs[k]["size_multiplier"] = self._clamp(v)
        for p in pitched + list(talk.get("proposals") or []):
            await self._handle_proposal(p.get("desk", desks[0]), p, now)
        self._apply(now)
        bus.emit("crew", now, desk=desks[0], phase="say", who="agent", to="all", text=self._wrap_line(slot))
        for k in desks:
            bus.emit("crew", now, desk=k, phase="done", brief=self.briefs.get(k))
        e.set_agent(now, "watching" if is_rth(now) else ("coffee" if ct_time(now) < time(8, 30) else "offline"),
                    self.directive["summary"])

    def _wrap_line(self, slot: str) -> str:
        v = self.directive.get("votes", {})
        cut = [k for k, m in v.items() if m < 1.0]
        up = [k for k in VOTERS_FOR_SIZE_UP if v.get(k, 1.0) > 1.0]
        if self.e.risk.st.halted:
            return "Done for the day. Thanks, team."
        if slot == "postclose":
            back = self.cfg["schedule"].get("arrive") or self.cfg["schedule"]["premarket"]
            return f"Good session. Journal's saved; see everyone at {clock_label(back)}."
        if cut:
            return f"Size to {int(self.e.risk.st.size_mult * 100)}% on {', '.join(DESKS[k].name for k in cut)}'s call."
        if len(up) == len(VOTERS_FOR_SIZE_UP):
            return "Macro, Rates and Vol all lean long. Size-up is on the table if the tape confirms."
        if up:
            return f"{', '.join(DESKS[k].name for k in up)} want more size. Not enough agreement; staying at 100%."
        return "Rules unchanged. Back to the screens."

    def _clamp(self, v) -> float:
        return max(self.cfg["min_size_multiplier"], min(self.cfg.get("max_size_multiplier", 1.25), float(v)))

    def _apply(self, now: float) -> None:
        votes = {k: b.get("size_multiplier", 1.0) for k, b in self.briefs.items() if b.get("day") == str(self.day)}
        cuts = [m for m in votes.values() if m < 1.0]
        self.e.risk.set_size_mult(min(cuts) if cuts else 1.0)
        have = {b.name for b in self.e.risk.st.blackouts}
        for k, b in self.briefs.items():
            for ev in b.get("events", []) or []:
                if ev.get("impact") == "high" and ev.get("time_ct") and ev.get("name") not in have:
                    try:
                        self.e.risk.add_blackout(at_ct(self.day, hhmm(ev["time_ct"])), ev["name"])
                        have.add(ev["name"])
                    except Exception:
                        pass
            if b.get("cooldown_minutes") and b.get("slot") == "loss-review" and abs(b.get("ts", 0) - now) < 1:
                self.e.risk.extend_cooldown(now, float(b["cooldown_minutes"]))
        biases = [b.get("bias") for k, b in self.briefs.items() if k in ("macro", "rates", "fed", "vol")]
        self.directive = {
            "size_mult": self.e.risk.st.size_mult, "votes": votes,
            "blackouts": [x.name for x in self.e.risk.st.blackouts],
            "summary": f"Size {int(self.e.risk.st.size_mult * 100)}%. "
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
        lows = [k for k, b in self.briefs.items() if b.get("day") == day and b.get("size_multiplier", 1.0) < 1.0]
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
    async def _handle_proposal(self, desk: str, p: dict, now: float) -> None:
        item = self.book.submit(desk, p, now)
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
        elif key == "quant":
            b = self._quant_brief(slot)
            if not self.offline:
                b = await self._llm(key, now, extra=json.dumps(b)) or b
        elif not self.offline and DESKS[key].uses_web:
            b = await self._llm(key, now)
        if b is None:
            b = self._offline_brief(key, now, slot)
        return self._finish(b)

    def _finish(self, b: dict) -> dict:
        b["day"] = str(self.day)
        b["size_multiplier"] = self._clamp(b.get("size_multiplier", 1.0))
        b["cooldown_minutes"] = max(0, min(30, int(b.get("cooldown_minutes") or 0)))
        return b

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
        tag = " (sim)" if sim else " (offline: set ANTHROPIC_API_KEY for live research)"
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
        if not self.offline and len([d for d in desks if d not in ("risk", "tape")]) >= 2:
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
            lines.append({"from": "quant", "to": "risk", "text": f"{stops} of the last {len(losers)} losers hit the -20% stop inside minutes. That's entry timing, not exits."
                          if losers else "Nothing broken. Variance."})
        if "tape" in b and "quant" in b:
            lines.append({"from": "tape", "to": "quant", "text": b["tape"].get("headline", "")[:110]})
        if "quant" in b and "vol" in b and slot == "postclose":
            lines.append({"from": "vol", "to": "quant", "text": "Straddles overpriced the move again today. That's the edge we keep paying for."})
            lines.append({"from": "quant", "to": "vol", "text": "Then pitch it properly. Put it in front of Evan with the numbers."})
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
                model=self.cfg["fast_model"], max_tokens=900, system=ROUNDTABLE_HINT + "\n\n" + SCHEMA_HINT.split("proposals (optional")[1],
                messages=[{"role": "user", "content": user}]), timeout=60)
        except Exception as ex:
            self.e.bus.emit("log", now, level="warn", msg=f"roundtable failed: {ex}")
            return None
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

    async def _llm(self, key: str, now: float, extra: str = "") -> dict | None:
        client = self._get_client()
        if client is None:
            return None
        desk = DESKS[key]
        ctx = (f"Today is {ct(now).strftime('%A %Y-%m-%d')}, {ct(now).strftime('%H:%M')} CT. SPY last {self.e.price}. "
               f"The trader runs an automated 0DTE SPY call strategy (MACD/RSI triggers, 4-5 contracts, flat by 14:40 CT).")
        system = (f"You are the {desk.name} desk on a small options trading team. Your beat: {desk.role} "
                  f"Be terse and numeric. Use web search for today's facts; never invent data. If you cannot verify "
                  f"something, leave it out.\n\n{SCHEMA_HINT}")
        user = ctx + (f"\n\nEngine stats JSON:\n{extra}" if extra else "") + f"\n\nGive the {desk.name} brief."
        kwargs = dict(model=self.cfg["fast_model"] if key == "quant" else self.cfg["model"], max_tokens=1400,
                      system=system, messages=[{"role": "user", "content": user}])
        if desk.uses_web and self.cfg.get("web_search", True):
            kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]
        try:
            msg = await asyncio.wait_for(client.messages.create(**kwargs), timeout=120)
        except Exception as ex:
            self.e.bus.emit("log", now, level="warn", msg=f"{desk.name} desk LLM call failed: {ex}")
            return None
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        return _extract_json(text)

    def state(self) -> dict:
        return {"briefs": self.briefs, "directive": self.directive, "offline": self.offline,
                "desks": {k: {"name": d.name} for k, d in DESKS.items()}, "conviction": self.conviction,
                "proposals": self.book.items[-30:]}


def _extract_json(text: str, key: str = "headline") -> dict | None:
    for m in reversed(list(re.finditer(r"\{", text))):
        chunk = text[m.start():]
        depth = 0
        for i, ch in enumerate(chunk):
            depth += ch == "{"
            depth -= ch == "}"
            if depth == 0:
                try:
                    obj = json.loads(chunk[: i + 1])
                    if isinstance(obj, dict) and key in obj:
                        return obj
                except json.JSONDecodeError:
                    pass
                break
    return None
