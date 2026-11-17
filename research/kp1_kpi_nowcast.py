"""KP1 Stage 1: how well does public data pin seven KPI-binary KPIs before each report?

Nowcasts and naive baselines per research/kp1_kpi_prereg.md section 4, scored walk-forward per section 5.
Run: `python research/kp1_kpi_nowcast.py` after `python research/kp1_fetch.py`. Writes
research/kp1_kpi_nowcast_results.json and research/kp1_kpi_nowcast.md.
"""
from __future__ import annotations

import calendar
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from statistics import mean

from kp1_fetch import DATA, PRODUCTS, quarter_bounds
from kp1_inputs import ACTUALS, HOOD_MONTHLY, MonthRow, load_actuals, load_hood_monthly, load_report_dates

# NYSE full-day closures 2021-2026 (weekdays only).
NYSE_HOLIDAYS = {date.fromisoformat(d) for d in """
2021-01-01 2021-01-18 2021-02-15 2021-04-02 2021-05-31 2021-07-05 2021-09-06 2021-11-25 2021-12-24
2022-01-17 2022-02-21 2022-04-15 2022-05-30 2022-06-20 2022-07-04 2022-09-05 2022-11-24 2022-12-26
2023-01-02 2023-01-16 2023-02-20 2023-04-07 2023-05-29 2023-06-19 2023-07-04 2023-09-04 2023-11-23 2023-12-25
2024-01-01 2024-01-15 2024-02-19 2024-03-29 2024-05-27 2024-06-19 2024-07-04 2024-09-02 2024-11-28 2024-12-25
2025-01-01 2025-01-09 2025-01-20 2025-02-17 2025-04-18 2025-05-26 2025-06-19 2025-07-04 2025-09-01 2025-11-27
2025-12-25
2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25
""".split()}

KPIS = {"HOOD": ("funded_customers", "revenue", "arpu"),
        "MARA": ("blocks_won", "bitcoins_mined", "revenue"),
        "COIN": ("transaction_revenue",)}
# HOOD transaction line -> (monthly driver, uses NYSE trading days?)
HOOD_LINES = {"rev_options": ("options_contracts", True), "rev_equities": ("equity_notional", True),
              "rev_crypto": ("crypto_notional", False), "rev_event_contracts": ("event_contracts", False)}


@dataclass
class Inputs:
    actuals: dict          # (company, quarter) -> {kpi: value}
    report_dates: dict     # (company, quarter) -> date
    monthly: list          # [MonthRow]
    mara_blocks: list      # [(height, unix_ts, reward_sats)]
    heights: dict          # quarter -> height of the last block before the quarter starts
    coinbase: dict         # product -> {date: (close, volume)}
    _cache: dict = field(default_factory=dict, repr=False)


# ---------- calendar helpers ----------


def prev_q(q: str) -> str:
    y, n = int(q[:4]), int(q[5])
    return f"{y - 1}Q4" if n == 1 else f"{y}Q{n - 1}"


def next_q(q: str) -> str:
    y, n = int(q[:4]), int(q[5])
    return f"{y + 1}Q1" if n == 4 else f"{y}Q{n + 1}"


def months_of(q: str) -> list[str]:
    y, n = int(q[:4]), int(q[5])
    return [f"{y}-{m:02d}" for m in range(3 * n - 2, 3 * n + 1)]


def calendar_days(m: str) -> int:
    return calendar.monthrange(int(m[:4]), int(m[5:]))[1]


def trading_days(m: str) -> int:
    y, mo = int(m[:4]), int(m[5:])
    days = (date(y, mo, d) for d in range(1, calendar_days(m) + 1))
    return sum(1 for d in days if d.weekday() < 5 and d not in NYSE_HOLIDAYS)


def _days(m: str, nyse: bool) -> int:
    return trading_days(m) if nyse else calendar_days(m)


def published_months(monthly, quarter: str, report_date: date) -> list[MonthRow]:
    """Monthly releases for months inside the quarter, published strictly before the report date."""
    inside = set(months_of(quarter))
    return sorted((m for m in monthly if m.month in inside and m.published < report_date), key=lambda m: m.month)


def naive_growth(prior, prior2):
    """prior x (1 + prior growth) = prior^2 / prior2."""
    if prior is None or not prior2:
        return None
    return prior * prior / prior2


def arpu(revenue, start_customers, end_customers):
    return revenue * 4 / ((start_customers + end_customers) / 2)


def _get(data: Inputs, co: str, q: str, kpi: str):
    return data.actuals.get((co, q), {}).get(kpi)


# ---------- HOOD ----------


def _hood_months(data: Inputs, q: str) -> list[MonthRow]:
    rd = data.report_dates.get(("HOOD", q))
    return published_months(data.monthly, q, rd) if rd else []


def _driver_total(months: list[MonthRow], q: str, driver: str, nyse: bool):
    if not months:
        return None
    days = sum(_days(m.month, nyse) for m in months)
    return sum(getattr(m, driver) for m in months) / days * sum(_days(m, nyse) for m in months_of(q))


def hood_revenue_lines(q: str, data: Inputs) -> dict[str, float] | None:
    """Each revenue line's nowcast for quarter q (prereg section 4), or None without prior data or releases."""
    p = prev_q(q)
    prior = data.actuals.get(("HOOD", p))
    cur, old = _hood_months(data, q), _hood_months(data, p)
    if not prior or not cur or not old:
        return None
    lines = {}
    for line, (driver, nyse) in HOOD_LINES.items():
        prior_line = prior.get(line) or 0.0
        prior_total = _driver_total(old, p, driver, nyse)
        if not prior_line or not prior_total:
            lines[line] = 0.0
            continue
        lines[line] = _driver_total(cur, q, driver, nyse) * prior_line / prior_total
    lines["rev_other_transaction"] = prior.get("rev_other_transaction") or 0.0
    lines["other_revenues"] = prior.get("other_revenues") or 0.0
    # net interest scales with the interest-earning base; a release that printed no cash sweep (2022) makes the
    # base margin-only for both quarters, so the ratio compares like with like.
    with_sweep = all(m.cash_sweep > 0 for m in cur + old)

    def base(ms):
        return mean(m.margin_book + (m.cash_sweep if with_sweep else 0.0) for m in ms)

    lines["net_interest"] = (prior.get("net_interest") or 0.0) * base(cur) / base(old)
    return lines


def _hood(q: str, data: Inputs) -> dict:
    p, pp = prev_q(q), prev_q(prev_q(q))
    prior_fc, prior2_fc = _get(data, "HOOD", p, "funded_customers"), _get(data, "HOOD", pp, "funded_customers")
    cur = _hood_months(data, q)
    fc_model = None
    if cur and prior_fc is not None:
        last = cur[-1]
        before = next((m.funded_customers for m in cur if m.month < last.month), None) if len(cur) > 1 else None
        if before is None:
            before = prior_fc if last.month == months_of(q)[0] else None
        if before is not None:
            fc_model = last.funded_customers + (last.funded_customers - before)
    fc_naive = None if prior_fc is None or prior2_fc is None else prior_fc + (prior_fc - prior2_fc)
    lines = hood_revenue_lines(q, data)
    rev_model = sum(lines.values()) if lines else None
    rev_naive = naive_growth(_get(data, "HOOD", p, "revenue"), _get(data, "HOOD", pp, "revenue"))

    def a(rev, fc):
        return None if rev is None or fc is None or prior_fc is None else arpu(rev, prior_fc, fc)

    return {"funded_customers": (fc_model, fc_naive), "revenue": (rev_model, rev_naive),
            "arpu": (a(rev_model, fc_model), a(rev_naive, fc_naive))}


# ---------- MARA ----------


def _onchain(data: Inputs, q: str) -> tuple[int, float]:
    key = ("onchain", q)
    if key not in data._cache:
        start, end = quarter_bounds(q)
        bs = [b for b in data.mara_blocks if start <= b[1] < end]
        data._cache[key] = (len(bs), sum(b[2] for b in bs) / 1e8)
    return data._cache[key]


def _network_blocks(data: Inputs, q: str):
    a, b = data.heights.get(q), data.heights.get(next_q(q))
    return None if a is None or b is None else b - a


def vwap(days: dict, q: str):
    start, end = quarter_bounds(q)
    s = datetime.fromtimestamp(start, tz=timezone.utc).date()
    e = datetime.fromtimestamp(end, tz=timezone.utc).date()
    rows = [cv for d, cv in days.items() if s <= d < e]
    vol = sum(v for _, v in rows)
    return sum(c * v for c, v in rows) / vol if vol else None


def _prior_ratio(data: Inputs, q: str, kpi: str, onchain_index: int):
    ratios = []
    for (co, pq), k in data.actuals.items():
        if co != "MARA" or pq >= q or kpi not in k:
            continue
        oc = _onchain(data, pq)[onchain_index]
        if oc:
            ratios.append(k[kpi] / oc)
    return mean(ratios) if ratios else None


def _mara(q: str, data: Inputs) -> dict:
    p = prev_q(q)
    blocks, btc = _onchain(data, q)
    r_blocks, r_btc = _prior_ratio(data, q, "blocks_won", 0), _prior_ratio(data, q, "bitcoins_mined", 1)
    blk_model = blocks * r_blocks if r_blocks is not None and blocks else None
    btc_model = btc * r_btc if r_btc is not None and blocks else None
    prior_blocks, net_prior, net_now = _onchain(data, p)[0], _network_blocks(data, p), _network_blocks(data, q)
    blk_naive = prior_blocks / net_prior * net_now if net_prior and net_now and prior_blocks else None
    btc_naive = blk_naive * (btc / blocks) if blk_naive is not None and blocks else None
    days = data.coinbase.get("BTC-USD", {})
    v_now, v_prior = vwap(days, q), vwap(days, p)
    prior_rev, prior_btc = _get(data, "MARA", p, "revenue"), _get(data, "MARA", p, "bitcoins_mined")
    other = None
    if None not in (prior_rev, prior_btc, v_prior):
        other = prior_rev - prior_btc * v_prior

    def rev(b):
        return None if b is None or v_now is None or other is None else b * v_now + other

    return {"blocks_won": (blk_model, blk_naive), "bitcoins_mined": (btc_model, btc_naive),
            "revenue": (rev(btc_model), rev(btc_naive))}


# ---------- COIN ----------


def coin_volume(data: Inputs, q: str) -> float:
    key = ("coinvol", q)
    if key not in data._cache:
        start, end = quarter_bounds(q)
        s = datetime.fromtimestamp(start, tz=timezone.utc).date()
        e = datetime.fromtimestamp(end, tz=timezone.utc).date()
        data._cache[key] = sum(c * v for days in data.coinbase.values() for d, (c, v) in days.items() if s <= d < e)
    return data._cache[key]


def _coin(q: str, data: Inputs) -> dict:
    prior_qs = sorted(pq for (co, pq), k in data.actuals.items()
                      if co == "COIN" and pq < q and "transaction_revenue" in k and coin_volume(data, pq) > 0)
    ratios = [data.actuals[("COIN", pq)]["transaction_revenue"] / coin_volume(data, pq) for pq in prior_qs[-4:]]
    vol = coin_volume(data, q)
    model = vol * mean(ratios) if ratios and vol > 0 else None
    p = prev_q(q)
    naive = naive_growth(_get(data, "COIN", p, "transaction_revenue"),
                         _get(data, "COIN", prev_q(p), "transaction_revenue"))
    return {"transaction_revenue": (model, naive)}


def nowcasts(company: str, quarter: str, data: Inputs) -> dict[str, tuple]:
    """kpi -> (model, naive); either is None when its inputs don't exist yet."""
    return {"HOOD": _hood, "MARA": _mara, "COIN": _coin}[company](quarter, data)


# ---------- loading ----------


def load_inputs() -> Inputs:
    with open(DATA / "mara_blocks.csv", newline="") as f:
        blocks = [(int(r["height"]), int(r["timestamp"]), int(r["reward_sats"])) for r in csv.DictReader(f)]
    with open(DATA / "network_heights.csv", newline="") as f:
        heights = {r["quarter"]: int(r["height_at_start"]) for r in csv.DictReader(f)}
    coinbase = {p: {} for p in PRODUCTS}
    with open(DATA / "coinbase_daily.csv", newline="") as f:
        for r in csv.DictReader(f):
            coinbase[r["product"]][date.fromisoformat(r["day"])] = (float(r["close"]), float(r["volume"]))
    return Inputs(actuals=load_actuals(ACTUALS), report_dates=load_report_dates(ACTUALS),
                  monthly=load_hood_monthly(HOOD_MONTHLY), mara_blocks=blocks, heights=heights, coinbase=coinbase)
