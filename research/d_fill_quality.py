"""Book D fill-quality study: what does a resting-then-walking limit order pay per leg, and does D make money at it?

Spec and pre-registered pass rule: docs/superpowers/specs/2026-11-09-d-fill-quality-design.md (approved 2026-11-10).
Plan: docs/superpowers/plans/2026-11-10-d-fill-quality.md. Research only; nothing here places or reviews orders.

Part 1 replays each D leg's order on ThetaData tick trades (each trade carries the NBBO at that moment): the limit
starts at the price nearest the mid on the passive side, holds H seconds, then moves 1c toward the far side every
15 s, and is filled by the tape under one of four rules (queue, queue2, optimistic, strict). Part 2 re-runs the D
replay from research/bd_real_quotes.py with the measured cost per leg.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

TAPE_COLS = ["ts", "price", "size", "bid", "ask", "bid_size", "ask_size"]
RULES = ("queue", "queue2", "optimistic", "strict")
TICK = 0.01


def _r(x: float) -> float:
    return round(x + 1e-9, 2) if x >= 0 else round(x, 2)


def start_price(bid: float, ask: float, side: str) -> float:
    """The price nearest the mid on the passive side: 1c market -> the passive side, 2c -> the mid,
    3c+ -> the mid rounded toward the passive side."""
    mid = (bid + ask) / 2
    cents = mid * 100
    if side == "sell":
        return _r(math.ceil(cents - 1e-6) / 100)
    if side == "buy":
        return _r(math.floor(cents + 1e-6) / 100)
    raise ValueError(side)


@dataclass
class Order:
    price: float | None
    ts: pd.Timestamp | None
    mid0: float
    width_c: int
    passive: bool
    reason: str


def _valid(bid, ask) -> bool:
    return bid is not None and ask is not None and not (pd.isna(bid) or pd.isna(ask)) and bid > 0 and ask > bid


def simulate(tape: pd.DataFrame, t0: pd.Timestamp, side: str, hold_s: int, rule: str, deadline: pd.Timestamp,
             step_s: int = 15) -> Order:
    """One order for one contract, decided at t0, worked until filled or until the deadline (then natural)."""
    if rule not in RULES:
        raise ValueError(rule)
    sgn = 1 if side == "buy" else -1                     # direction the limit walks: buy up, sell down
    tp = tape.sort_values("ts", kind="stable")
    before = tp[tp.ts <= t0]
    q = None
    for r in before.itertuples(index=False):
        if _valid(r.bid, r.ask):
            q = r
    if q is None:
        return Order(None, None, float("nan"), 0, False, "no_quote")
    bid, ask, bsz, asz = float(q.bid), float(q.ask), float(q.bid_size), float(q.ask_size)
    mid0, width_c = (bid + ask) / 2, int(round((ask - bid) * 100))
    start = start_price(bid, ask, side)

    def natural():
        return ask if side == "buy" else bid

    def k_at(t) -> int:
        dt = (t - t0).total_seconds()
        return 0 if dt < hold_s else 1 + int((dt - hold_s) // step_s)

    def queue_for(lim) -> float:
        passive_px, size = (bid, bsz) if side == "buy" else (ask, asz)
        if rule in ("optimistic", "strict"):
            return 0.0
        qa = size if abs(lim - passive_px) < 1e-9 else 0.0
        return qa * (2 if rule == "queue2" else 1)

    k = 0
    limit = start
    queue = queue_for(limit)
    traded_at = 0.0

    def reprice(t):
        """Step the limit to k(t); returns a fill Order when the new limit reaches the natural."""
        nonlocal k, limit, queue, traded_at
        nk = k_at(t)
        if nk == k:
            return None
        k = nk
        new = _r(start + sgn * k * TICK)
        nat = natural()
        if (side == "buy" and new >= nat - 1e-9) or (side == "sell" and new <= nat + 1e-9):
            return Order(_r(nat), t, mid0, width_c, False, "walked_to_natural")
        limit, queue, traded_at = new, queue_for(new), 0.0
        return None

    step_times = []
    t = t0 + pd.Timedelta(seconds=hold_s)
    while t < deadline:
        step_times.append(t)
        t += pd.Timedelta(seconds=step_s)
    rows = list(tp[(tp.ts > t0) & (tp.ts <= deadline)].itertuples(index=False))
    events = sorted([(r.ts, 1, r) for r in rows] + [(s, 0, None) for s in step_times], key=lambda e: (e[0], e[1]))

    for ts, kind, r in events:
        if kind == 0:
            o = reprice(ts)
            if o:
                return o
            continue
        o = reprice(ts)
        if o:
            return o
        px, sz = float(r.price), float(r.size)
        better = px > limit + 1e-9 if side == "sell" else px < limit - 1e-9
        at = abs(px - limit) < 1e-9
        if better:
            return Order(limit, ts, mid0, width_c, True, "trade_through")
        if at and rule != "strict":
            traded_at += sz
            if rule == "optimistic" or traded_at > queue:
                return Order(limit, ts, mid0, width_c, True, "queue")
        if _valid(r.bid, r.ask):
            bid, ask, bsz, asz = float(r.bid), float(r.ask), float(r.bid_size), float(r.ask_size)
            if (side == "sell" and bid >= limit - 1e-9) or (side == "buy" and ask <= limit + 1e-9):
                return Order(limit, ts, mid0, width_c, True, "cross")
    return Order(_r(natural()), deadline, mid0, width_c, False, "deadline")


def cost_cents(o: Order, side: str) -> float | None:
    """Cents worse than the decision-time mid (negative = better than mid)."""
    if o.price is None:
        return None
    c = (o.price - o.mid0) * 100 if side == "buy" else (o.mid0 - o.price) * 100
    return round(c, 6)
