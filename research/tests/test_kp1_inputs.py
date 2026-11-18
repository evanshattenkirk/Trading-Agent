"""KP1 typed inputs: reported KPIs (actuals.csv) and Robinhood monthly data (hood_monthly.csv)."""
import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kp1_inputs as I  # noqa: E402

HEADER_A = "company,quarter,report_date,kpi,value,printed,source_url\n"
HEADER_M = ("month,published,funded_customers,equity_notional,options_contracts,crypto_notional,"
            "event_contracts,margin_book,cash_sweep,sec_lending_revenue,source_url\n")
URL = "https://example.com/x"


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_actuals_rejects_duplicate_kpi_rows(tmp_path):
    p = _write(tmp_path, "a.csv", HEADER_A
               + f"HOOD,2024Q3,2024-10-30,revenue,637000000,$637 million,{URL}\n"
               + f"HOOD,2024Q3,2024-10-30,revenue,638000000,$638 million,{URL}\n")
    with pytest.raises(ValueError, match="duplicate"):
        I.load_actuals(p)


def test_actuals_report_date_consistent_per_quarter(tmp_path):
    p = _write(tmp_path, "a.csv", HEADER_A
               + f"HOOD,2024Q3,2024-10-30,revenue,637000000,$637 million,{URL}\n"
               + f"HOOD,2024Q3,2024-10-31,arpu,105,ARPU $105,{URL}\n")
    with pytest.raises(ValueError, match="report date"):
        I.load_report_dates(p)


def test_actuals_load_values_and_dates(tmp_path):
    p = _write(tmp_path, "a.csv", HEADER_A
               + f"MARA,2025Q3,2025-11-04,blocks_won,633,Number of blocks won 633,{URL}\n"
               + f"MARA,2025Q3,2025-11-04,bitcoins_mined,2144,\"bitcoin produced 2,144\",{URL}\n")
    assert I.load_actuals(p) == {("MARA", "2025Q3"): {"blocks_won": 633.0, "bitcoins_mined": 2144.0}}
    assert I.load_report_dates(p) == {("MARA", "2025Q3"): date(2025, 11, 4)}


def test_monthly_parses_units(tmp_path):
    p = _write(tmp_path, "m.csv", HEADER_M
               + f"2026-08,2026-09-10,28700000,335400000000,292500000,17500000000,4700000000,"
                 f"21500000000,31200000000,36000000,{URL}\n")
    (row,) = I.load_hood_monthly(p)
    assert row.month == "2026-08"
    assert row.published == date(2026, 9, 10)
    assert row.funded_customers == 28_700_000
    assert row.options_contracts == 292_500_000
    assert row.sec_lending_revenue == 36_000_000


def test_hood_revenue_lines_sum_to_total_within_1m():
    actuals = I.load_actuals(I.ACTUALS)
    lines = ("rev_options", "rev_equities", "rev_crypto", "rev_event_contracts", "rev_other_transaction")
    checked = 0
    for (co, q), k in actuals.items():
        if co != "HOOD" or "rev_options" not in k:
            continue
        total = sum(k.get(n, 0.0) for n in lines) + k["net_interest"] + k["other_revenues"]
        assert abs(total - k["revenue"]) <= 1e6, (q, total, k["revenue"])
        checked += 1
    assert checked >= 16


def test_every_row_has_https_source():
    for path in (I.ACTUALS, I.HOOD_MONTHLY):
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        assert rows
        assert all(r["source_url"].startswith("https://") for r in rows), path


def test_real_files_cover_the_samples():
    actuals = I.load_actuals(I.ACTUALS)
    by_co = defaultdict(set)
    for co, q in actuals:
        by_co[co].add(q)
    assert {"2021Q3", "2026Q3"} <= by_co["MARA"]
    assert {"2021Q2", "2026Q3"} <= by_co["COIN"]
    assert {"2022Q4", "2026Q3"} <= by_co["HOOD"]
    months = I.load_hood_monthly(I.HOOD_MONTHLY)
    assert len({m.month for m in months}) == len(months)
