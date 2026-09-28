"""Signal engine: 15m + 5m MACD filter, 1m + 144t MACD cross triggers, RSI band.

Entry (calls):
  filter   15m and 5m MACD above signal (live/forming candle)
  rsi      RSI(14) inside [30, 70] on the rsi_check_timeframes (live candle)
  trigger  1m and 144t both have MACD above signal on their last closed bar,
           and at least one of them crossed up within confirm_window_sec
  setup    fresh 1m cross -> SWING (1m exit rules), otherwise SCALP (144t exit rules)

Puts mirror everything when allow_puts is on.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .bars import Bar
from .indicators import MACD, RSI, MACDValue


@dataclass
class TFState:
    tf: str
    macd: MACD
    rsi: RSI
    last: MACDValue | None = None          # last closed bar values
    prev: MACDValue | None = None
    rsi_val: float | None = None
    close: float | None = None
    live_close: float | None = None        # forming bar close (time TFs)
    cross_up_ts: float | None = None
    cross_dn_ts: float | None = None
    cross_up_id: int = 0
    cross_dn_id: int = 0
    bars: int = 0
    hist_prev: float | None = None

    def live(self) -> tuple[MACDValue | None, float | None]:
        if self.live_close is None:
            return self.last, self.rsi_val
        m = self.macd.preview(self.live_close)
        r = self.rsi.preview(self.live_close)
        return (m if m is not None else self.last), (r if r is not None else self.rsi_val)

    def snapshot(self, use_live: bool) -> dict:
        m, r = self.live() if use_live else (self.last, self.rsi_val)
        return {
            "tf": self.tf,
            "macd": round(m.macd, 4) if m else None,
            "signal": round(m.signal, 4) if m else None,
            "hist": round(m.hist, 4) if m else None,
            "bull": bool(m and m.bull),
            "rsi": round(r, 1) if r is not None else None,
            "cross_up_ts": self.cross_up_ts,
            "cross_dn_ts": self.cross_dn_ts,
            "ready": m is not None and r is not None,
        }


@dataclass
class EntrySignal:
    side: str               # "call" | "put"
    setup: str              # "SWING" | "SCALP"
    ts: float
    price: float
    trigger_tf: str
    cross_key: tuple
    reasons: list[str] = field(default_factory=list)


@dataclass
class CrossEvent:
    tf: str
    direction: str          # "up" | "down"
    ts: float
    value: MACDValue


class SignalEngine:
    def __init__(self, scfg):
        m, r = scfg["macd"], scfg["rsi"]
        self.cfg = scfg
        self.tick_tf = f"{scfg['tick_bar_size']}t"
        tfs = list(dict.fromkeys(scfg["filter_timeframes"] + scfg["trigger_timeframes"] + ["1m", "5m", "15m", self.tick_tf]))
        self.tf: dict[str, TFState] = {
            t: TFState(t, MACD(m["fast"], m["slow"], m["signal"]), RSI(r["period"])) for t in tfs
        }
        self.used_crosses: set[tuple] = set()

    # ---- feeding -------------------------------------------------------
    def on_bar_close(self, bar: Bar) -> CrossEvent | None:
        st = self.tf.get(bar.tf)
        if st is None:
            return None
        v = st.macd.update(bar.c)
        st.rsi_val = st.rsi.update(bar.c)
        st.close, st.live_close = bar.c, None
        st.bars += 1
        st.hist_prev = st.last.hist if st.last else None
        st.prev, st.last = st.last, v
        if v is None or st.prev is None:
            return None
        ts = bar.end or bar.t
        if not st.prev.bull and v.bull:
            st.cross_up_ts, st.cross_up_id = ts, st.cross_up_id + 1
            return CrossEvent(bar.tf, "up", ts, v)
        if st.prev.bull and not v.bull:
            st.cross_dn_ts, st.cross_dn_id = ts, st.cross_dn_id + 1
            return CrossEvent(bar.tf, "down", ts, v)
        return None

    def on_live_price(self, tf: str, px: float) -> None:
        if tf in self.tf:
            self.tf[tf].live_close = px

    # ---- evaluation ----------------------------------------------------
    def ready(self) -> bool:
        return all(self.tf[t].last is not None and self.tf[t].rsi_val is not None
                   for t in self.cfg["filter_timeframes"] + self.cfg["trigger_timeframes"])

    def check(self, now: float, side: str = "call") -> tuple[bool, list[str], list[str]]:
        """Returns (all_ok, passed, failed) human-readable condition lists."""
        bull = side == "call"
        passed, failed = [], []
        lo, hi = self.cfg["rsi"]["lower"], self.cfg["rsi"]["upper"]

        for t in self.cfg["filter_timeframes"]:
            m, _ = self.tf[t].live()
            ok = m is not None and (m.bull if bull else not m.bull)
            (passed if ok else failed).append(f"{t} MACD {'>' if bull else '<'} signal")
        for t in self.cfg["rsi_check_timeframes"]:
            _, r = self.tf[t].live()
            ok = r is not None and lo < r < hi
            (passed if ok else failed).append(f"{t} RSI {r:.0f} in {lo}-{hi}" if r is not None else f"{t} RSI warming up")
        for t in self.cfg["trigger_timeframes"]:
            m = self.tf[t].last
            ok = m is not None and (m.bull if bull else not m.bull)
            (passed if ok else failed).append(f"{t} MACD {'>' if bull else '<'} signal")
        fresh = self._fresh_cross(now, bull)
        (passed if fresh else failed).append("fresh trigger cross" if fresh else "no fresh trigger cross")
        return not failed, passed, failed

    def _fresh_cross(self, now: float, bull: bool) -> tuple[str, tuple] | None:
        win = self.cfg["confirm_window_sec"]
        best = None
        for t in self.cfg["trigger_timeframes"]:
            st = self.tf[t]
            ts = st.cross_up_ts if bull else st.cross_dn_ts
            cid = st.cross_up_id if bull else st.cross_dn_id
            key = (t, "up" if bull else "dn", cid)
            if ts is not None and now - ts <= win and key not in self.used_crosses:
                if best is None or (t == "1m"):      # a fresh 1m cross defines a SWING
                    best = (t, key)
        return best

    def evaluate(self, now: float, price: float) -> EntrySignal | None:
        sides = ["call"] + (["put"] if self.cfg.get("allow_puts") else [])
        for side in sides:
            ok, passed, _ = self.check(now, side)
            if not ok:
                continue
            tf, key = self._fresh_cross(now, side == "call")
            setup = "SWING" if tf == "1m" else "SCALP"
            return EntrySignal(side, setup, now, price, tf, key, passed)
        return None

    def consume(self, sig: EntrySignal) -> None:
        """Mark all currently fresh crosses as used so one cross = one entry."""
        for t in self.cfg["trigger_timeframes"]:
            st = self.tf[t]
            self.used_crosses.add((t, "up", st.cross_up_id))
            self.used_crosses.add((t, "dn", st.cross_dn_id))

    def snapshot(self) -> dict:
        live_tfs = {"15m", "5m"}
        return {t: s.snapshot(t in live_tfs) for t, s in self.tf.items()}

    def hist_rising(self, tf: str) -> bool:
        st = self.tf[tf]
        m, _ = st.live()
        return bool(m and st.last and st.prev and m.hist >= st.last.hist >= st.prev.hist)
