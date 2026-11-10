"""Book D fill-quality study: what does a resting-then-walking limit order pay per leg, and does D make money at it?

Spec and pre-registered pass rule: docs/superpowers/specs/2026-11-09-d-fill-quality-design.md (approved 2026-11-10).
Plan: docs/superpowers/plans/2026-11-10-d-fill-quality.md. Research only; nothing here places or reviews orders.

Part 1 replays each D leg's order on ThetaData tick trades (each trade carries the NBBO at that moment): the limit
starts at the price nearest the mid on the passive side, holds H seconds, then moves 1c toward the far side every
15 s, and is filled by the tape under one of four rules (queue, queue2, optimistic, strict). Part 2 re-runs the D
replay from research/bd_real_quotes.py with the measured cost per leg.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
log = logging.getLogger("d_fill_quality")

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


# ---------------------------------------------------------------- Part 2: fill models for the D replay

Costs = dict  # (window "entry"/"close", side "buy"/"sell", width bucket 1/2/3) -> cents worse than mid


def width_bucket(width_c: int) -> int:
    return 1 if width_c <= 1 else (2 if width_c == 2 else 3)


def _clamped(bid: float, ask: float, side: str, c_cents: float) -> float:
    mid = (bid + ask) / 2
    if side == "buy":
        return min(ask, max(bid, mid + c_cents / 100))
    return max(bid, min(ask, mid - c_cents / 100))


def measured_model(costs: Costs):
    """Leg price = mid -/+ the measured cost for (window, side, width bucket), between the passive side and natural.
    A missing bucket falls back to the next wider one for that window and side, then to taker."""
    def f(bid: float, ask: float, side: str, opening: bool) -> float:
        window = "entry" if opening else "close"
        wb = width_bucket(int(round((ask - bid) * 100)))
        for b in range(wb, 4):
            c = costs.get((window, side, b))
            if c is not None and not (isinstance(c, float) and math.isnan(c)):
                return _clamped(bid, ask, side, c)
        return ask if side == "buy" else bid
    return f


def uniform_model(c_cents: float):
    def f(bid: float, ask: float, side: str, opening: bool) -> float:
        return _clamped(bid, ask, side, c_cents)
    return f


def replay_d(p, legs, model) -> dict | None:
    """bd_real_quotes.replay for D with any fill model; a stop exit is re-priced at taker (a stop is urgent)."""
    import bd_real_quotes as bd
    r = bd.replay(p, legs, bd.D_ENTRY, bd.D_CLOSE, model, width=2)
    if r is None or r["why"] != "stop":
        return r
    d = bd._value(p, legs, r["exit_minute"], "taker", opening=False)
    if d is None:
        return r
    v = min(d, 2.0) + len(legs) * bd.FEE
    pnl = r["credit"] - v
    return {**r, "pnl": pnl, "ret": pnl / r["risk"]}


# ---------------------------------------------------------------- pooling and the pre-registered verdict

T_BAR = 2.33


def pool_costs(events: pd.DataFrame, rule: str, hold_s: int) -> Costs:
    ev = events[(events.rule == rule) & (events.hold_s == hold_s)].dropna(subset=["cost_c"])
    if ev.empty:
        return {}
    ev = ev.assign(wb=ev.width_c.astype(int).map(width_bucket))
    return {(w, s, int(b)): float(c) for (w, s, b), c in ev.groupby(["window", "side", "wb"]).cost_c.mean().items()}


def verdict(primary: pd.DataFrame, queue2: pd.DataFrame) -> dict:
    """Spec section 7: t >= 2.33, mean > 0 in 2016-2021 and in 2022-2026, mean > 0 with the queue doubled.
    Returns are fractions per trade; the reported averages are in percent."""
    from bd_real_quotes import summ
    s = summ(primary.ret)
    h1, h2 = summ(primary[primary.year <= 2021].ret), summ(primary[primary.year >= 2022].ret)
    s2 = summ(queue2.ret)
    avg, t = s.get("avg", float("nan")), s.get("t", float("nan"))
    checks = [bool(avg > 0 and t >= T_BAR), bool(h1.get("avg", -1) > 0 and h2.get("avg", -1) > 0),
              bool(s2.get("avg", -1) > 0)]
    return {"pass": all(checks), "checks": checks, "n": s.get("n", 0), "avg": avg, "t": t,
            "avg_2016_2021": h1.get("avg"), "avg_2022_2026": h2.get("avg"), "avg_queue2": s2.get("avg"),
            "t_queue2": s2.get("t")}


# ---------------------------------------------------------------- data: D's legs and the tick pull

ET = "America/New_York"
WINDOWS = {"entry": ("09:59:00", "10:10:00"), "close": ("15:19:00", "15:28:00")}


def normalize_ticks(raw: pd.DataFrame) -> pd.DataFrame:
    """ThetaData option_history_trade_quote rows -> TAPE_COLS, timestamps in ET, sorted by time."""
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=TAPE_COLS)
    ts = pd.to_datetime(raw["trade_timestamp"])
    ts = ts.dt.tz_localize(ET) if ts.dt.tz is None else ts.dt.tz_convert(ET)
    out = pd.DataFrame({"ts": ts, "price": raw["price"].astype(float), "size": raw["size"].astype(float),
                        "bid": raw["bid"].astype(float), "ask": raw["ask"].astype(float),
                        "bid_size": raw["bid_size"].astype(float), "ask_size": raw["ask_size"].astype(float)})
    return out.sort_values("ts", kind="stable").reset_index(drop=True)[TAPE_COLS]


def d_legs_for_day(p, d: dt.date, vix: pd.Series):
    """D's four legs as the verdict replay picks them: parity spot at 10:00 ET, prior VIX close."""
    import bd_real_quotes as bd
    s = bd.spot(p, bd.D_ENTRY)
    vx = bd.prev_close(vix, d)
    if s is None or math.isnan(vx):
        return None
    return bd.legs_D(s, vx)


def tick_path(out: Path, d: dt.date, right: str, strike: float, window: str) -> Path:
    return out / f"{d.isoformat()}_{right}{strike:g}_{window}.parquet"


def _client():
    from thetadata import ThetaClient
    return ThetaClient(dataframe_type="pandas", api_key=os.environ["THETADATA_API_KEY"])


def _fetch_ticks(client, d: dt.date, right: str, strike: float, window: str, retries: int = 6) -> pd.DataFrame:
    import grpc
    from thetadata.errors import NoDataFoundError
    from fetch_thetadata_spy0dte import TRANSIENT
    start, end = WINDOWS[window]
    delay = 5.0
    for attempt in range(1, retries + 1):
        try:
            return normalize_ticks(client.option_history_trade_quote(
                symbol="SPY", expiration=d, date=d, strike=f"{strike:g}", right="call" if right == "C" else "put",
                start_time=start, end_time=end))
        except NoDataFoundError:
            return normalize_ticks(None)
        except grpc.RpcError as e:
            if e.code() not in TRANSIENT or attempt == retries:
                raise
        except (ConnectionError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(delay)
        delay = min(delay * 2, 120)
    return normalize_ticks(None)


def pull_ticks(quotes: Path, vix_path: Path, out: Path, workers: int = 4) -> None:
    import bd_real_quotes as bd
    vix = bd.load_vix(vix_path)
    out.mkdir(parents=True, exist_ok=True)
    jobs = []
    for f in sorted(quotes.glob("*.parquet")):
        d = dt.date.fromisoformat(f.stem)
        try:
            p = bd.QuotePanel(bd.normalize(pd.read_parquet(f)))
        except Exception as e:  # noqa: BLE001
            log.warning("%s unreadable: %s", f.name, e)
            continue
        if p.last_minute < bd.B_CLOSE:
            continue                                          # half day: the verdict drops it too
        legs = d_legs_for_day(p, d, vix)
        if legs is None:
            continue
        for r, k, _ in legs:
            for w in WINDOWS:
                if not tick_path(out, d, r, k, w).exists():
                    jobs.append((d, r, k, w))
    log.info("%d tick requests to make", len(jobs))
    client = _client()
    fails = out.parent / "d_ticks_failures.csv"

    def one(job):
        d, r, k, w = job
        df = _fetch_ticks(client, d, r, k, w)
        df.to_parquet(tick_path(out, d, r, k, w))
        return len(df)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, j): j for j in jobs}
        for fut in as_completed(futs):
            j = futs[fut]
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                with fails.open("a", newline="") as fh:
                    csv.writer(fh).writerow([j[0], j[1], j[2], j[3], repr(e)[:200]])
            done += 1
            if done % 500 == 0:
                log.info("%d/%d", done, len(jobs))


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("pull-ticks")
    a.add_argument("--quotes", type=Path, required=True)
    a.add_argument("--vix", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    if args.cmd == "pull-ticks":
        pull_ticks(args.quotes, args.vix, args.out, args.workers)


if __name__ == "__main__":
    main()
