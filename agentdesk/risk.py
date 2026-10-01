"""Risk manager: sizing, daily limits, cooldowns, blackouts, kill switch.

The crew (LLM desks) can only *restrict* through this module: a size multiplier
clamped to [min_size_multiplier, 1.0], extra blackout windows, and cooldowns.
Nothing the crew says can raise size, loosen a limit, or place an order.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .clock import ct_time, hm, session_date
from .config import hhmm

log = logging.getLogger("agentdesk.risk")


@dataclass
class Blackout:
    start: float
    end: float
    name: str
    flatten_at: float | None = None


@dataclass
class RiskState:
    day_pnl: float = 0.0
    peak_day_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    loss_streak: int = 0
    cooldown_until: float = 0.0
    halted: bool = False
    halt_reason: str | None = None
    flatten_all: bool = False       # set by the kill switch and the safety watchdog
    halt_sticky: bool = True        # False for startup checks, which re-run on every start
    paused: bool = False
    size_mult: float = 1.0          # book A's crew multiplier
    book_mults: dict = field(default_factory=dict)      # crew multiplier per book letter (A..G), <= 1.0; missing = 1.0
    blackouts: list[Blackout] = field(default_factory=list)


class RiskStore:
    """Today's risk state on disk, so a restart can't reset the daily loss, trade count, cooldown or a halt.
    One small JSON file per mode, rewritten atomically on every change."""
    FIELDS = ("day_pnl", "peak_day_pnl", "trades", "wins", "losses", "loss_streak", "cooldown_until",
              "halted", "halt_reason", "flatten_all", "paused", "size_mult", "book_mults")

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self, day: str) -> dict | None:
        """The saved state for `day`, None if there is none (or it is from another day). Raises if unreadable."""
        if not self.path.exists():
            return None
        d = json.loads(self.path.read_text())
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
        return d if d.get("day") == day else None

    def save(self, day: str, st: RiskState) -> None:
        d = {"day": day, **{k: getattr(st, k) for k in self.FIELDS}}
        if st.halted and not st.halt_sticky:      # re-checked at startup; don't carry it over
            d.update(halted=False, halt_reason=None, flatten_all=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1))
        os.replace(tmp, self.path)


class RiskManager:
    def __init__(self, cfg, store: RiskStore | None = None):
        self.cfg = cfg
        self.r = cfg["risk"]
        self.s = cfg["sizing"]
        self.st = RiskState()
        self.store = store
        self.day: str | None = None
        self.clear_halt_on_restore = False      # run --clear-halt: lift a saved halt, keep the P&L and counts

    def reset_day(self, day: str | None = None) -> None:
        keep = [b for b in self.st.blackouts if day is None or str(session_date(b.end)) >= day]   # yesterday's are done
        self.st = RiskState(blackouts=keep)
        if day is not None:
            self.day = day
        self._save()

    def restore(self, day: str) -> str | None:
        """Load today's saved state (engine start). Returns a line for the log when something was restored."""
        self.day = day
        if self.store is None:
            return None
        try:
            saved = self.store.load(day)
        except Exception as ex:
            self.halt(f"could not read saved risk state {self.store.path} ({ex}); fix or delete it, then restart")
            return self.st.halt_reason
        if not saved:
            self._save()
            return None
        st = self.st
        for k in RiskStore.FIELDS:
            if k in saved:
                setattr(st, k, saved[k])
        st.size_mult = min(1.0, float(st.size_mult))       # a saved crew cut can only restrict
        st.book_mults = {str(k): min(1.0, float(v)) for k, v in (st.book_mults or {}).items()}
        st.halt_sticky = True
        note = (f"Restored today's risk state: day P&L {st.day_pnl:+.2f}, {st.trades} trades"
                + (f", cooldown until {hm(st.cooldown_until)}" if st.cooldown_until else "")
                + (f", halted: {st.halt_reason}" if st.halted else ""))
        if st.halted and self.clear_halt_on_restore:
            note += " (halt cleared by --clear-halt)"
            st.halted, st.halt_reason, st.flatten_all = False, None, False
        self._save()
        return note

    def _save(self) -> None:
        if self.store is None or self.day is None:
            return
        try:
            self.store.save(self.day, self.st)
        except Exception as ex:                  # never let a disk problem stop a flatten
            log.error("could not save risk state to %s: %s", self.store.path, ex)

    # ---- gates --------------------------------------------------------
    def can_enter(self, now: float, open_positions: int, open_pnl: float = 0.0) -> tuple[bool, str]:
        st, r = self.st, self.r
        win = dict(self.cfg["strategy"]["entry_window"])
        if self.early_close(now):
            win["end"] = min(win["end"], "11:20")
        t = ct_time(now)
        if st.halted:
            return False, f"halted: {st.halt_reason}"
        if st.paused:
            return False, "paused"
        if not (hhmm(win["start"]) <= t < hhmm(win["end"])):
            return False, f"outside entry window {win['start']}-{win['end']}"
        if open_positions >= r["max_open_positions"]:
            return False, "position already open"
        if st.trades >= r["max_trades_per_day"]:
            return False, f"max {r['max_trades_per_day']} trades hit"
        if st.day_pnl + min(0.0, open_pnl) <= -abs(r["max_daily_loss"]):
            self.halt(f"daily loss limit -${r['max_daily_loss']}")
            return False, st.halt_reason
        pl = r.get("profit_lock")
        if pl and st.peak_day_pnl >= pl["trigger"] and st.day_pnl <= st.peak_day_pnl * (1 - pl["giveback_pct"]):
            self.halt(f"profit lock: gave back {int(pl['giveback_pct'] * 100)}% of +${st.peak_day_pnl:.0f}")
            return False, st.halt_reason
        if now < st.cooldown_until:
            return False, f"cooldown until {hm(st.cooldown_until)}"
        for b in st.blackouts:
            if b.start <= now < b.end:
                return False, f"blackout: {b.name}"
        return True, "ok"

    def size(self, ask: float, up_mult: float = 1.0, cut: float | None = None) -> tuple[int, str]:
        """Budget = $max_trade x multiplier. Crew cuts (<1) always win; a size-up (>1, max 1.25) only
        applies when no cut is active and also scales the contract cap (5 -> 6). `cut` overrides today's crew cut
        (1.0 gives the quantity the crew's vote would have left alone, for the crew log)."""
        if ask <= 0:
            return 0, "no ask"
        cut = self.st.size_mult if cut is None else cut
        m = cut if cut < 1.0 else min(max(1.0, up_mult), self.cfg["crew"].get("max_size_multiplier", 1.25))
        budget = self.s["max_trade_dollars"] * m
        cap = round(self.s["max_contracts"] * m) if m > 1.0 else self.s["max_contracts"]
        qty = min(cap, int(budget // (ask * 100)))
        if qty < self.s["min_contracts"]:
            return 0, f"1 contract costs ${ask * 100:.0f} > budget ${budget:.0f}"
        tag = f" SIZE-UP {m:.2f}x" if m > 1.0 else ""
        return qty, f"{qty} x ${ask:.2f} = ${qty * ask * 100:.0f} (budget ${budget:.0f}{tag})"

    def early_close(self, now: float) -> bool:
        return str(session_date(now)) in {str(d) for d in (self.cfg.get("calendar") or {}).get("early_close", [])}

    def must_flatten(self, now: float) -> str | None:
        fl = "11:40" if self.early_close(now) else self.cfg["exits"]["flatten_at"]
        if ct_time(now) >= hhmm(fl):
            return f"flatten {fl} CT"
        for b in self.st.blackouts:
            if b.flatten_at is not None and b.flatten_at <= now < b.end:
                return f"flatten before {b.name}"
        if self.st.halted and self.st.flatten_all:
            return self.st.halt_reason or "kill switch"
        return None

    @staticmethod
    def open_pnl(positions) -> float:
        """What the open positions would realize if sold at the bid now."""
        return sum((p.bid - p.entry) * 100 * p.qty for p in positions if p.qty > 0 and p.bid > 0)

    def check_open_loss(self, positions) -> bool:
        """Realized + open P&L at or past the daily loss limit: halt and flatten (HANDOFF section 12)."""
        lim = abs(self.r["max_daily_loss"])
        if self.st.halted or self.st.day_pnl + min(0.0, self.open_pnl(positions)) > -lim:
            return False
        self.halt(f"daily loss limit -${lim:g} hit including open positions", flatten=True)
        return True

    # ---- bookkeeping --------------------------------------------------
    def on_trade_closed(self, pnl: float, now: float) -> None:
        """Counts and streaks only; dollars already booked via on_realized()."""
        st = self.st
        st.trades += 1
        if pnl >= 0:
            st.wins += 1
            st.loss_streak = 0
        else:
            st.losses += 1
            st.loss_streak += 1
            c = self.r["loss_streak_cooldown"]
            if st.loss_streak >= c["losses"]:
                st.cooldown_until = now + c["minutes"] * 60
        self._save()

    def on_realized(self, pnl: float) -> None:
        """Partial fills (scale-outs) count toward the daily P&L immediately."""
        self.st.day_pnl += pnl
        self.st.peak_day_pnl = max(self.st.peak_day_pnl, self.st.day_pnl)
        self._save()

    def halt(self, reason: str, flatten: bool = False, sticky: bool = True) -> None:
        """sticky=False for startup checks that re-run on every start; every other halt survives a restart."""
        st = self.st
        st.halt_sticky = sticky if not st.halted else (st.halt_sticky or sticky)
        st.halted, st.halt_reason = True, reason
        st.flatten_all = st.flatten_all or flatten
        self._save()

    def set_paused(self, on: bool) -> None:
        self.st.paused = on
        self._save()

    def add_blackout(self, event_ts: float, name: str) -> None:
        eb = self.r["event_blackout"]
        fl = self.r.get("flatten_before_high_impact_min")
        self.st.blackouts.append(Blackout(
            event_ts - eb["before_min"] * 60, event_ts + eb["after_min"] * 60, name,
            event_ts - fl * 60 if fl is not None else None))

    def set_size_mult(self, m: float) -> None:
        lo = self.cfg["crew"]["min_size_multiplier"]
        self.st.size_mult = max(lo, min(1.0, m))
        self._save()

    def set_book_mults(self, mults: dict) -> None:
        """The crew's multiplier per book (votes routed by topic). Book A's also drives size_mult."""
        lo = self.cfg["crew"]["min_size_multiplier"]
        self.st.book_mults = {str(k): max(lo, min(1.0, float(v))) for k, v in mults.items()}
        self.st.size_mult = self.st.book_mults.get("A", 1.0)
        self._save()

    def book_mult(self, book: str) -> float:
        """A crew cut for one book (<= 1.0). Book A reads size_mult so a direct set_size_mult still counts."""
        if book == "A":
            return min(1.0, self.st.size_mult)
        return min(1.0, float((self.st.book_mults or {}).get(book, 1.0)))

    def extend_cooldown(self, now: float, minutes: float) -> None:
        self.st.cooldown_until = max(self.st.cooldown_until, now + min(30, minutes) * 60)
        self._save()

    def to_dict(self) -> dict:
        st = self.st
        return {
            "day_pnl": round(st.day_pnl, 2), "peak_day_pnl": round(st.peak_day_pnl, 2), "trades": st.trades,
            "wins": st.wins, "losses": st.losses, "loss_streak": st.loss_streak,
            "cooldown_until": st.cooldown_until or None, "halted": st.halted, "halt_reason": st.halt_reason,
            "paused": st.paused, "size_mult": st.size_mult, "book_mults": dict(st.book_mults or {}),
            "max_daily_loss": self.r["max_daily_loss"], "max_trades": self.r["max_trades_per_day"],
            "blackouts": [{"start": b.start, "end": b.end, "name": b.name} for b in st.blackouts],
        }
