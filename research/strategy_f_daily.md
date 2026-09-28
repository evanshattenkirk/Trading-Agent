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

## 2016–2026 rerun (Evan's Mac, 2026-09-28)

505 symbols, 2016-01-04 → 2026-09-25, Alpaca SIP daily bars written by `strategy_f_intraday.py`. Bonferroni bar |t| > 2.96. Net of the 10 bp round trip, in excess of the equal-weight universe. Halves are 2016–2020 / 2021–2026.

| Test | n | bp/trade | bp/day | win | t | halves (bp / t) |
|---|---|---|---|---|---|---|
| momentum rvol≥2 up≥3% hold 1d | 5,418 | −19.5 | −19.1 | 46% | **−3.89** | −24/−3.2, −16/−2.2 |
| momentum rvol≥2 up≥3% hold 5d | 5,412 | −37.1 | −27.5 | 46% | −2.64 | −60/−3.1, −19/−0.8 |
| momentum rvol≥2 up≥3% hold 10d | 5,397 | −26.1 | −16.3 | 47% | −1.08 | −59/−1.6, −0/+0.1 |
| momentum rvol≥2 up≥3% hold 20d | 5,385 | +4.1 | −6.8 | 47% | −0.29 | −39/−1.7, +37/+1.0 |
| momentum rvol≥2 up≥5% hold 1d | 3,246 | −20.2 | −18.9 | 46% | −2.92 | −20/−2.5, −20/−1.6 |
| momentum rvol≥2 up≥5% hold 5d | 3,241 | −47.0 | −25.3 | 46% | −1.84 | −71/−2.4, −29/−0.3 |
| momentum rvol≥2 up≥5% hold 10d | 3,234 | −25.8 | −12.2 | 47% | −0.57 | −71/−1.6, +8/+0.7 |
| momentum rvol≥2 up≥5% hold 20d | 3,225 | +15.0 | +15.0 | 47% | +0.45 | −49/−1.4, +62/+1.8 |
| momentum rvol≥3 up≥3% hold 1d | 1,502 | −19.2 | −21.7 | 47% | −2.49 | −31/−3.3, −8/−0.0 |
| momentum rvol≥3 up≥3% hold 5d | 1,500 | −25.1 | −33.5 | 49% | −2.10 | −67/−3.4, +13/+0.1 |
| momentum rvol≥3 up≥3% hold 10d | 1,498 | +2.7 | −12.8 | 48% | −0.54 | −69/−2.4, +67/+1.4 |
| momentum rvol≥3 up≥3% hold 20d | 1,494 | +19.5 | −5.9 | 48% | −0.17 | −68/−2.4, +98/+1.4 |
| momentum rvol≥3 up≥5% hold 1d | 1,136 | −16.3 | −19.1 | 48% | −1.88 | −26/−2.6, −7/+0.1 |
| momentum rvol≥3 up≥5% hold 5d | 1,134 | −21.1 | −19.2 | 49% | −1.03 | −59/−2.3, +13/+0.7 |
| momentum rvol≥3 up≥5% hold 10d | 1,133 | +10.2 | +7.8 | 49% | +0.29 | −56/−1.6, +69/+1.8 |
| momentum rvol≥3 up≥5% hold 20d | 1,130 | +34.9 | +19.9 | 49% | +0.49 | −54/−1.6, +114/+1.9 |
| down, 16 cells (rvol≥2/3, down≥3/5%, hold 1/5/10/20) | 1,667–6,972 | −32 to −3 | −27 to −2 | 46–50% | −1.86 to −0.24 | none past ±2 |
| control up≥3% (rvol < 1.5) | 45,255 | −9.9 | −14.5 | 47% | −4.99 | −17/−4.0, −5/−3.0 |
| control up≥5% | 10,853 | −0.8 | −21.4 | 49% | −3.24 | −8/−3.1, +11/−1.4 |
| gap up≥2%, open → close | 36,119 | −15.0 | −20.7 | 46% | −5.64 | −14/−4.5, −16/−3.5 |
| gap up≥4% | 9,585 | −21.3 | −24.1 | 46% | −3.49 | −18/−2.3, −25/−2.6 |
| gap down≤−2% (not pre-registered) | 35,701 | +4.1 | −5.1 | 51% | −1.39 | +8/−0.6, +1/−1.4 |

**Read:**
- **Still nothing to buy after a high-volume up day.** No momentum cell is positive and significant (largest positive t +0.49). The one cell past the Bonferroni bar goes the other way: RVOL ≥ 2, up ≥ 3%, next day −19.5 bp, t −3.89, negative in both halves. In 2013–2018 that cell was flat; here the high-volume move reverses too. It is a short signal at best, and F never shorts.
- **The gap chase still loses:** −15 / −21 bp, t −5.6 / −3.5, negative in both halves.
- **The normal-volume control still reverses** (t −5.0 / −3.2). The down side and the gap-down row show nothing (the gap-down t 2.4 from 2013–2018 is gone).
- The 20-day holds are positive in 2021–2026 (+37 to +114 bp, t up to 1.9) and negative in 2016–2020. None pass, and this is an observation, not a signal to select.
- Event counts per year are about 2.5× the 2013–2018 run's. Inferred, not checked: 2016–2026 is more volatile (2020, 2022), and today's list includes names that were smaller and more volatile back then (survivorship).
- The AI/memory 2023–2026 slice and the three sub-periods are in `strategy_f_daily_results_2016_2026.json` on Evan's Mac, not in this table.

To rerun, after `strategy_f_intraday.py` has written `research/data/f_intraday/daily/`:

```
.venv/bin/python research/strategy_f_daily.py research/data/f_intraday/daily research/strategy_f_daily_results_2016_2026.json
```
