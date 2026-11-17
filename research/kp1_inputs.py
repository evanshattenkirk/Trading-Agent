"""KP1 typed inputs: loaders and validators for the hand-typed KPI history.

`kp1_inputs/actuals.csv` holds one row per reported KPI value (company, quarter, report date, kpi, value in raw
units, the text as printed, and the source URL). `kp1_inputs/hood_monthly.csv` holds Robinhood's monthly operating
data, one row per month, raw units (customers in persons, dollars in dollars, contracts in contracts).
Rules: research/kp1_kpi_prereg.md.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACTUALS = HERE / "kp1_inputs" / "actuals.csv"
HOOD_MONTHLY = HERE / "kp1_inputs" / "hood_monthly.csv"


def _rows(path) -> list[dict[str, str]]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_actuals(path=ACTUALS) -> dict[tuple[str, str], dict[str, float]]:
    """{(company, quarter): {kpi: value}}. A second row for the same KPI-quarter is a typing error."""
    out: dict[tuple[str, str], dict[str, float]] = {}
    for r in _rows(path):
        key = (r["company"], r["quarter"])
        kpis = out.setdefault(key, {})
        if r["kpi"] in kpis:
            raise ValueError(f"duplicate row for {key} {r['kpi']}")
        kpis[r["kpi"]] = float(r["value"])
    return out


def load_report_dates(path=ACTUALS) -> dict[tuple[str, str], date]:
    """{(company, quarter): report date}. Every row of one quarter must carry the same date."""
    out: dict[tuple[str, str], date] = {}
    for r in _rows(path):
        key = (r["company"], r["quarter"])
        d = date.fromisoformat(r["report_date"])
        if out.setdefault(key, d) != d:
            raise ValueError(f"report date differs within {key}: {out[key]} vs {d}")
    return out


@dataclass(frozen=True)
class MonthRow:
    month: str  # "YYYY-MM"
    published: date
    funded_customers: float
    equity_notional: float
    options_contracts: float
    crypto_notional: float
    event_contracts: float
    margin_book: float
    cash_sweep: float
    sec_lending_revenue: float


_NUM = ("funded_customers", "equity_notional", "options_contracts", "crypto_notional", "event_contracts",
        "margin_book", "cash_sweep", "sec_lending_revenue")


def load_hood_monthly(path=HOOD_MONTHLY) -> list[MonthRow]:
    rows = [MonthRow(month=r["month"], published=date.fromisoformat(r["published"]),
                     **{k: float(r[k]) for k in _NUM}) for r in _rows(path)]
    return sorted(rows, key=lambda m: m.month)


if __name__ == "__main__":
    actuals = load_actuals()
    load_report_dates()
    months = load_hood_monthly()
    print(f"actuals.csv: {sum(len(v) for v in actuals.values())} values over {len(actuals)} company-quarters; "
          f"hood_monthly.csv: {len(months)} months ({months[0].month} to {months[-1].month})")
