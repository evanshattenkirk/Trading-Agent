# Book F daily study, rebuilt (2013–2018 check)

The original `research/strategy_f.py` exists only in the claude.ai session that designed book F. `research/strategy_f_daily.py` is rebuilt from the definitions in HANDOFF v3.1 section 7F. It was run on the same data (505 S&P 500 stocks, 2013-02-08 → 2018-02-07, [CNuge/kaggle-code](https://github.com/CNuge/kaggle-code) `stock_data/individual_stocks_5yr.zip`) to check that it reproduces 7F's table before anyone relies on its 2016–2026 rerun. Full output: `strategy_f_daily_results_2013_2018.json`.

| Test (7F) | 7F events | Rebuilt events | 7F net | Rebuilt net | 7F t | Rebuilt t |
|---|---|---|---|---|---|---|
| Gap up ≥ 2%, buy the open, sell the close | 7,160 | 7,167 | −29 bp, 43% win | −29 bp, 43% win | −8.2 | −8.6 |
| Gap up ≥ 4% | 1,789 | 1,791 | −44 bp, 42% win | −44 bp, 41% win | −4.6 | −5.0 |
| Control: up ≥ 3% on RVOL < 1.5, next day | 8,359 | 8,361 | −18 bp | −19 bp (per day) | −4.5 | −4.5 |
| Control: up ≥ 5% on RVOL < 1.5 | 1,017 | 1,019 | −31 bp | −32 bp (per day) | −2.2 | −2.3 |
| Down side, RVOL ≥ 2 / 3, down ≥ 3%, next day | 3,029 / 1,135 | 3,030 / 1,134 | small declines | −16 / −12 bp | −2.2 / −0.6 | −2.3 / −0.6 |
| Gap down ≤ −2% (not pre-registered) | — | 8,176 | +11 bp | +11 bp | 2.3 | 2.4 |
| Momentum, 16 cells | 548–2,296 | 546–2,293 | −27 to +25 bp | −21 to +24 bp | best 1.31 | largest \|t\| 1.36 |

**Read:** the rebuild reproduces the gap chase, control and down-side rows within a few events and about 0.4 of t. The momentum cells match on event counts but not exactly on the per-cell means; no cell comes near the Bonferroni bar of 2.96 in either version, so the conclusion (no multi-day drift worth buying) is the same.

Two definitions were settled by the check, not guessed:
- **Control rows** match 7F only without the close-near-high condition (with it, 7,554 / 910 events).
- **Means:** 7F's control rows are the mean of daily means (trades grouped by entry day); its gap rows are per-trade means. The JSON carries both (`mean_bp`, `day_mean_bp`).

## 2016–2026 rerun (Evan's Mac)

`research/strategy_f_intraday.py` writes the per-symbol daily CSVs to `research/data/f_intraday/daily/` as part of its pull. After that, from `~/Trading-Agent`:

```
.venv/bin/python research/strategy_f_daily.py research/data/f_intraday/daily research/strategy_f_daily_results_2016_2026.json
```

It prints the same table, with results for 2016–2020, 2021–2026 and the AI/memory list for 2023–2026 in the JSON.
