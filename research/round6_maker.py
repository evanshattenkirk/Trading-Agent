"""PM6 (research/round6_maker_prereg.md): a resting YES bid on PM4's modal Bitcoin bracket, filled from Kalshi's
trade tape. Reuses round 6's cached markets, candles and Coinbase minutes (research/round6_fetch.py).

    .venv/bin/python research/round6_maker.py fetch    # trade tape for every order (resumable)
    .venv/bin/python research/round6_maker.py report   # research/round6_maker_results.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import round6 as r6  # noqa: E402
from round5_k1 import Api, Limiter, NotFound, _read_json_gz, _write_json_gz  # noqa: E402
from round6_fetch import DATA, HIST_CUTOFF, ts  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TAPE = DATA / "trades"
log = logging.getLogger("pm6")


# ------------------------------------------------------------------ orders
def orders(off: int = 15, neighbours: bool = False) -> tuple[list[dict], dict]:
    """PM4's picks as resting orders: one per hour (or three with neighbours), bid b at E = close - off min."""
    counts = defaultdict(int)
    out = []
    for ev in r6.btc_events():
        rev = r6.btc_range_event(ev)
        ms = [m for m in r6.markets_of("KXBTC", rev) if r6.settled(m) and (k := r6.kind(m)) and k[0] == "between"]
        if not ms:
            counts["no_markets"] += 1
            continue
        close = ts(ms[0]["close_time"])
        e = close - off * 60
        ss = r6.spot_sigma(e, 60)
        if not ss:
            counts["no_spot"] += 1
            continue
        ms = r6.nearest(ms, ss[0], lambda m: (r6.num(m["floor_strike"]) + r6.num(m["cap_strike"])) / 2)
        quoted = []
        for m in ms:
            c = r6.candles_of("KXBTC", m["ticker"])
            q = r6.quote_at(c, e) if c else None
            if q:
                quoted.append((m, q))
        if not quoted:
            counts["no_quotes"] += 1
            continue
        quoted.sort(key=lambda x: r6.num(x[0]["floor_strike"]))
        mids = [(q[0] + q[1]) / 2 for _, q in quoted]
        best = max(range(len(quoted)), key=lambda i: (mids[i], -i))
        for i in [best] + ([best - 1, best + 1] if neighbours else []):
            if not 0 <= i < len(quoted):
                continue
            m, (bid, ask) = quoted[i]
            if not r6.buyable(bid):
                counts["bid_not_buyable"] += 1
                continue
            out.append({"event": rev, "ticker": m["ticker"], "e": e, "close": close, "bid": bid, "ask": ask,
                        "payout": 1 if m["result"] == "yes" else 0, "modal": i == best})
    return out, dict(counts)


# ------------------------------------------------------------------ tape
def tape_path(ticker: str) -> Path:
    return TAPE / f"{ticker}.json.gz"


def fetch(threads: int = 6):
    TAPE.mkdir(parents=True, exist_ok=True)
    want = {}
    for off in (15, 45):
        for o in orders(off, neighbours=True)[0]:
            a, b = want.get(o["ticker"], (o["e"], o["close"]))
            want[o["ticker"]] = (min(a, o["e"]), b)
    todo = [(t, a, b) for t, (a, b) in want.items() if not tape_path(t).exists()]
    log.info("tape: %d markets to fetch", len(todo))
    api = Api(limiter=Limiter(interval=0.15, lo=0.1), in_flight=threads)

    def one(t, a, b):
        paths = ["/historical/trades", "/markets/trades"]
        if b >= HIST_CUTOFF:
            paths.reverse()
        for path in paths:
            rows, cur = [], None
            while True:
                p = {"ticker": t, "limit": 1000, "min_ts": a, "max_ts": b}
                if cur:
                    p["cursor"] = cur
                try:
                    d = api(path, p)
                except NotFound:
                    break
                rows += d.get("trades", [])
                cur = d.get("cursor")
                if not cur or not d.get("trades"):
                    break
            if rows:
                return rows
        return []

    with ThreadPoolExecutor(threads) as ex:
        futs = {ex.submit(one, *x): x[0] for x in todo}
        for i, f in enumerate(as_completed(futs)):
            _write_json_gz(tape_path(futs[f]), f.result())
            if i % 500 == 0:
                log.info("tape %d/%d (calls %d, 429s %d)", i, len(todo), api.calls, api.limiter.throttles)


def norm_trades(raw: list) -> list[dict]:
    out = []
    for t in raw or []:
        p = t.get("yes_price_dollars", t.get("yes_price"))
        if p is None:
            continue
        p = float(p)
        if p > 1.5:            # very old rows may be in cents
            p /= 100
        out.append({"ts": ts(t["created_time"]) if isinstance(t.get("created_time"), str) else int(t["created_time"]),
                    "t_exact": t.get("created_time"), "p": p, "taker": t.get("taker_side")})
    return sorted(out, key=lambda x: x["ts"])


def fill(order: dict, trades: list[dict], rule: str = "through") -> dict | None:
    """First trade after E and at or before close that fills the bid: 'through' = yes price < b; 'touch' = yes
    price <= b with a YES seller as taker. Returns that trade or None."""
    b = order["bid"]
    for t in trades:
        if not (order["e"] < t["ts"] <= order["close"]):
            continue
        if rule == "through" and t["p"] < b - 1e-9:
            return t
        if rule == "touch" and t["p"] <= b + 1e-9 and t["taker"] == "no":
            return t
    return None


def replay(off=15, neighbours=False, rule="through", maker_fee=0.0) -> tuple[list[dict], dict]:
    os_, counts = orders(off, neighbours)
    rows = []
    for o in os_:
        f = tape_path(o["ticker"])
        if not f.exists():
            counts["no_tape_file"] = counts.get("no_tape_file", 0) + 1
            continue
        hit = fill(o, norm_trades(_read_json_gz(f)), rule)
        b = o["bid"]
        fee = r6.fee(b, maker_fee) if maker_fee else 0.0
        r = (o["payout"] - b - fee) / (b + fee) if hit else None
        rows.append(o | {"filled": bool(hit), "ret": r, "pnl_cents": 100 * (o["payout"] - b - fee) if hit else 0.0,
                         "fill_lag": (hit["ts"] - o["e"]) if hit else None, "p": b})
    return rows, counts


def summary(rows: list[dict]) -> dict:
    filled = [x for x in rows if x["filled"]]
    s = r6.summarize([{"event": x["event"] + x["ticker"], "close": x["close"], "ret": x["ret"], "p": x["p"]}
                      for x in filled])
    s["orders"] = len(rows)
    s["fill_rate"] = len(filled) / len(rows) if rows else None
    s["pnl_per_order_cents"] = sum(x["pnl_cents"] for x in rows) / len(rows) if rows else None
    s["pnl_per_filled_cents"] = (sum(x["pnl_cents"] for x in filled) / len(filled)) if filled else None
    return s


def report() -> dict:
    rows, counts = replay()
    s = summary(rows)
    ok = (s.get("events", 0) > 0 and s["t"] >= r6.T_BAR and (s["half1_mean"] or -1) > 0
          and (s["half2_mean"] or -1) > 0 and (s["pnl_per_order_cents"] or -1) > 0)
    early = [x for x in rows if x["filled"] and x["fill_lag"] <= 300]
    late = [x for x in rows if x["filled"] and x["e"] + x["fill_lag"] >= x["close"] - 300]
    out = {"PM6": {"verdict": "PASS" if ok else "FAIL", "primary": s, "skips": counts,
                   "sens": {"touch": summary(replay(rule="touch")[0]),
                            "E-45": summary(replay(off=45)[0]),
                            "neighbours": summary([x for x in replay(neighbours=True)[0] if not x["modal"]]),
                            "maker_fee_0.0175": summary(replay(maker_fee=0.0175)[0]),
                            "filled_first_5min": summary(early), "filled_last_5min": summary(late)}}}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["fetch", "report"])
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if a.step == "fetch":
        fetch()
    else:
        out = report()
        (ROOT / "research" / "round6_maker_results.json").write_text(json.dumps(out, indent=1, default=str))
        p = out["PM6"]["primary"]
        print(out["PM6"]["verdict"], {k: p.get(k) for k in ("orders", "fill_rate", "events", "mean", "t", "half1_mean",
                                                             "half2_mean", "pnl_per_order_cents")})
        for k, v in out["PM6"]["sens"].items():
            print(" ", k, {x: v.get(x) for x in ("orders", "fill_rate", "events", "mean", "t", "pnl_per_order_cents")})


if __name__ == "__main__":
    main()
