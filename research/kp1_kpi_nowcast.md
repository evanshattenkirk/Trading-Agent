# KP1 Stage 1: how well public data pins KPI-binary KPIs — results

Rules: `research/kp1_kpi_prereg.md` (frozen before any data). Inputs: `research/kp1_inputs/` (typed from each earnings release, one source URL per row) and `research/data/kp1/` (mempool.space MARA Pool blocks and heights, Coinbase Exchange daily candles; `python research/kp1_fetch.py`).

**Gate passed: KCHOD, KBHOD, KDHOD, KCMAR, KFMAR, KBMAR, KCCOI.** Gate per KPI: model median |log error| at most half the naive baseline's, and the model's 80% interval covers 60-95% of scored quarters. Stage 1 proves no edge; it only says whether public data knows more than a trend line. COIN transaction revenue is paper-traded in Stage 2 whatever its result.

| Contract | KPI | Scored | Model median abs err | Naive median abs err | Model / naive | Model err in strike intervals | 80% coverage | Brier model | Brier naive | Gate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| KCHOD | HOOD Funded Customers | 12 | 0.4% | 1.0% | 0.42390 | 1.0 | 75.0% | 0.156 | 0.220 | PASS |
| KBHOD | HOOD Revenue | 12 | 9.5% | 19.0% | 0.49996 | 11.3 | 83.3% | 0.249 | 0.491 | PASS |
| KDHOD | HOOD ARPU | 12 | 9.6% | 19.5% | 0.49152 | 18.2 | 83.3% | 0.254 | 0.484 | PASS |
| KCMAR | MARA Total Blocks Won | 7 | 0.1% | 5.2% | 0.01922 | 0.7 | 85.7% | 0.103 | 0.272 | PASS |
| KFMAR | MARA Bitcoins Mined | 16 | 1.3% | 21.8% | 0.05930 | 2.8 | 93.8% | 0.279 | 0.257 | PASS |
| KBMAR | MARA Revenue | 16 | 2.7% | 20.5% | 0.13286 | 5.6 | 93.8% | 0.272 | 0.279 | PASS |
| KCCOI | COIN Transaction Revenue | 16 | 12.7% | 29.0% | 0.43901 | 74.6 | 93.8% | 0.331 | 0.273 | PASS |

Abs err = |ln(actual / nowcast)|, so 1.0% is about a 1% miss. Brier is over 7 synthetic strikes (the model nowcast rounded to Cboe's strike interval, ±3 intervals) and the scored quarters; lower is better. A quarter is scored once both methods have 4 earlier errors.

## KCHOD: HOOD Funded Customers

| Quarter | Actual (scaled by 1e+06) | Model | Naive | Model err | Naive err | Scored | In 80% |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2022Q3 | 22.900 | 22.900 | — | 0.0% | n/a |  |  |
| 2022Q4 | 23.000 | 23.100 | 22.900 | -0.4% | 0.4% |  |  |
| 2023Q1 | 23.100 | 23.200 | 23.100 | -0.4% | 0.0% |  |  |
| 2023Q2 | 23.200 | 23.100 | 23.200 | 0.4% | 0.0% |  |  |
| 2023Q3 | 23.300 | 23.200 | 23.300 | 0.4% | 0.0% |  |  |
| 2023Q4 | 23.400 | 23.300 | 23.400 | 0.4% | 0.0% | yes | yes |
| 2024Q1 | 23.900 | 23.700 | 23.500 | 0.8% | 1.7% | yes | no |
| 2024Q2 | 24.200 | 24.200 | 24.400 | 0.0% | -0.8% | yes | yes |
| 2024Q3 | 24.300 | 24.400 | 24.500 | -0.4% | -0.8% | yes | yes |
| 2024Q4 | 25.200 | 25.200 | 24.400 | 0.0% | 3.2% | yes | yes |
| 2025Q1 | 25.800 | 25.700 | 26.100 | 0.4% | -1.2% | yes | yes |
| 2025Q2 | 26.500 | 25.900 | 26.400 | 2.3% | 0.4% | yes | no |
| 2025Q3 | 26.800 | 26.700 | 27.200 | 0.4% | -1.5% | yes | yes |
| 2025Q4 | 27.000 | 26.700 | 27.100 | 1.1% | -0.4% | yes | yes |
| 2026Q1 | 27.400 | 27.600 | 27.200 | -0.7% | 0.7% | yes | yes |
| 2026Q2 | 28.400 | 27.800 | 27.800 | 2.1% | 2.1% | yes | no |
| 2026Q3 | 28.700 | 28.700 | 29.400 | 0.0% | -2.4% | yes | yes |

## KBHOD: HOOD Revenue

| Quarter | Actual (scaled by 1e+09) | Model | Naive | Model err | Naive err | Scored | In 80% |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2022Q3 | 0.361 | — | — | n/a | n/a |  |  |
| 2022Q4 | 0.380 | 0.338 | 0.410 | 11.8% | -7.6% |  |  |
| 2023Q1 | 0.441 | 0.448 | 0.400 | -1.6% | 9.8% |  |  |
| 2023Q2 | 0.486 | 0.476 | 0.512 | 2.0% | -5.2% |  |  |
| 2023Q3 | 0.467 | 0.571 | 0.536 | -20.1% | -13.7% |  |  |
| 2023Q4 | 0.471 | 0.476 | 0.449 | -1.1% | 4.8% | yes | yes |
| 2024Q1 | 0.618 | 0.587 | 0.475 | 5.1% | 26.3% | yes | yes |
| 2024Q2 | 0.682 | 0.717 | 0.811 | -5.1% | -17.3% | yes | yes |
| 2024Q3 | 0.637 | 0.745 | 0.753 | -15.7% | -16.7% | yes | yes |
| 2024Q4 | 1.014 | 0.883 | 0.595 | 13.9% | 53.3% | yes | no |
| 2025Q1 | 0.927 | 1.012 | 1.614 | -8.8% | -55.5% | yes | yes |
| 2025Q2 | 0.989 | 0.858 | 0.847 | 14.2% | 15.4% | yes | yes |
| 2025Q3 | 1.274 | 1.392 | 1.055 | -8.8% | 18.8% | yes | yes |
| 2025Q4 | 1.283 | 1.429 | 1.641 | -10.8% | -24.6% | yes | yes |
| 2026Q1 | 1.067 | 1.145 | 1.292 | -7.0% | -19.1% | yes | yes |
| 2026Q2 | 1.308 | 1.068 | 0.887 | 20.3% | 38.8% | yes | no |
| 2026Q3 | 1.424 | 1.576 | 1.603 | -10.1% | -11.9% | yes | yes |

## KDHOD: HOOD ARPU

| Quarter | Actual (scaled by 1) | Model | Naive | Model err | Naive err | Scored | In 80% |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2022Q3 | 63.0 | — | — | n/a | n/a |  |  |
| 2022Q4 | 66.0 | 58.8 | 71.6 | 11.6% | -8.1% |  |  |
| 2023Q1 | 77.0 | 77.6 | 69.4 | -0.8% | 10.4% |  |  |
| 2023Q2 | 84.0 | 82.5 | 88.4 | 1.9% | -5.1% |  |  |
| 2023Q3 | 80.0 | 98.5 | 92.1 | -20.8% | -14.1% |  |  |
| 2023Q4 | 81.0 | 81.7 | 76.9 | -0.9% | 5.2% | yes | yes |
| 2024Q1 | 104.0 | 99.7 | 81.0 | 4.2% | 25.0% | yes | yes |
| 2024Q2 | 113.0 | 119.3 | 134.3 | -5.4% | -17.3% | yes | yes |
| 2024Q3 | 105.0 | 122.7 | 123.6 | -15.6% | -16.3% | yes | yes |
| 2024Q4 | 164.0 | 142.7 | 97.7 | 13.9% | 51.8% | yes | no |
| 2025Q1 | 145.0 | 159.0 | 251.7 | -9.2% | -55.2% | yes | yes |
| 2025Q2 | 151.0 | 132.8 | 129.9 | 12.8% | 15.1% | yes | yes |
| 2025Q3 | 191.0 | 209.3 | 157.2 | -9.1% | 19.5% | yes | yes |
| 2025Q4 | 191.0 | 213.7 | 243.6 | -11.2% | -24.3% | yes | yes |
| 2026Q1 | 157.0 | 167.7 | 190.7 | -6.6% | -19.5% | yes | yes |
| 2026Q2 | 187.0 | 154.8 | 128.6 | 18.9% | 37.4% | yes | no |
| 2026Q3 | 200.0 | 220.8 | 221.9 | -9.9% | -10.4% | yes | yes |

## KCMAR: MARA Total Blocks Won

Not printed (dropped): 2021Q3, 2021Q4, 2022Q1, 2022Q2, 2022Q3, 2022Q4, 2023Q1, 2023Q2, 2023Q3.

| Quarter | Actual (scaled by 1) | Model | Naive | Model err | Naive err | Scored | In 80% |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2023Q4 | 562.0 | — | 538.2 | n/a | 4.3% |  |  |
| 2024Q1 | 368.0 | 368.7 | 547.5 | -0.2% | -39.7% |  |  |
| 2024Q2 | 457.0 | 457.4 | 357.5 | -0.1% | 24.6% |  |  |
| 2024Q3 | 604.0 | 604.4 | 471.4 | -0.1% | 24.8% |  |  |
| 2024Q4 | 703.0 | 702.3 | 617.0 | 0.1% | 13.1% |  |  |
| 2025Q1 | 666.0 | 665.4 | 669.9 | 0.1% | -0.6% | yes | yes |
| 2025Q2 | 694.0 | 691.5 | 668.3 | 0.4% | 3.8% | yes | no |
| 2025Q3 | 633.0 | 633.8 | 720.7 | -0.1% | -13.0% | yes | yes |
| 2025Q4 | 595.0 | 595.7 | 609.8 | -0.1% | -2.5% | yes | yes |
| 2026Q1 | 653.0 | 653.7 | 576.8 | -0.1% | 12.4% | yes | yes |
| 2026Q2 | 700.0 | 700.6 | 664.3 | -0.1% | 5.2% | yes | yes |
| 2026Q3 | 644.0 | 644.5 | 711.7 | -0.1% | -10.0% | yes | yes |

## KFMAR: MARA Bitcoins Mined

| Quarter | Actual (scaled by 1e+03) | Model | Naive | Model err | Naive err | Scored | In 80% |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2021Q3 | 1.252 | — | 0.486 | n/a | 94.6% |  |  |
| 2021Q4 | 1.098 | 1.223 | 1.254 | -10.8% | -13.3% |  |  |
| 2022Q1 | 1.259 | 1.172 | 1.196 | 7.1% | 5.2% |  |  |
| 2022Q2 | 0.707 | 0.672 | 1.239 | 5.0% | -56.1% |  |  |
| 2022Q3 | 0.616 | 0.482 | 0.706 | 24.4% | -13.6% |  |  |
| 2022Q4 | 1.562 | 1.549 | 0.496 | 0.8% | 114.7% | yes | yes |
| 2023Q1 | 2.195 | 1.460 | 1.542 | 40.8% | 35.3% | yes | no |
| 2023Q2 | 2.926 | 3.116 | 1.462 | -6.3% | 69.4% | yes | yes |
| 2023Q3 | 3.490 | 3.716 | 2.697 | -6.3% | 25.8% | yes | yes |
| 2023Q4 | 4.242 | 4.423 | 3.892 | -4.2% | 8.6% | yes | yes |
| 2024Q1 | 2.811 | 2.697 | 3.696 | 4.1% | -27.4% | yes | yes |
| 2024Q2 | 2.058 | 2.046 | 1.468 | 0.6% | 33.8% | yes | yes |
| 2024Q3 | 2.070 | 2.109 | 1.510 | -1.9% | 31.6% | yes | yes |
| 2024Q4 | 2.492 | 2.475 | 1.998 | 0.7% | 22.1% | yes | yes |
| 2025Q1 | 2.286 | 2.296 | 2.123 | -0.4% | 7.4% | yes | yes |
| 2025Q2 | 2.358 | 2.385 | 2.118 | -1.1% | 10.7% | yes | yes |
| 2025Q3 | 2.144 | 2.173 | 2.273 | -1.3% | -5.8% | yes | yes |
| 2025Q4 | 2.011 | 2.036 | 1.919 | -1.2% | 4.7% | yes | yes |
| 2026Q1 | 2.247 | 2.232 | 1.814 | 0.7% | 21.4% | yes | yes |
| 2026Q2 | 2.422 | 2.394 | 2.090 | 1.2% | 14.7% | yes | yes |
| 2026Q3 | 2.247 | 2.204 | 2.239 | 2.0% | 0.4% | yes | yes |

## KBMAR: MARA Revenue

| Quarter | Actual (scaled by 1e+06) | Model | Naive | Model err | Naive err | Scored | In 80% |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2021Q3 | 51.707 | — | — | n/a | n/a |  |  |
| 2021Q4 | 60.282 | 66.854 | 68.599 | -10.3% | -12.9% |  |  |
| 2022Q1 | 51.718 | 47.331 | 48.284 | 8.9% | 6.9% |  |  |
| 2022Q2 | 24.922 | 20.488 | 37.531 | 19.6% | -40.9% |  |  |
| 2022Q3 | 12.690 | 13.815 | 18.516 | -8.5% | -37.8% |  |  |
| 2022Q4 | 28.417 | 27.620 | 8.652 | 2.8% | 118.9% | yes | yes |
| 2023Q1 | 51.132 | 33.459 | 35.311 | 42.4% | 37.0% | yes | no |
| 2023Q2 | 81.759 | 89.007 | 42.443 | -8.5% | 65.6% | yes | yes |
| 2023Q3 | 97.849 | 103.658 | 75.068 | -5.8% | 26.5% | yes | yes |
| 2023Q4 | 156.768 | 160.414 | 141.143 | -2.3% | 10.5% | yes | yes |
| 2024Q1 | 165.198 | 152.583 | 208.029 | 7.9% | -23.1% | yes | yes |
| 2024Q2 | 145.139 | 143.226 | 105.360 | 1.3% | 32.0% | yes | yes |
| 2024Q3 | 131.647 | 137.864 | 101.585 | -4.6% | 25.9% | yes | yes |
| 2024Q4 | 214.394 | 219.856 | 178.686 | -2.5% | 18.2% | yes | yes |
| 2025Q1 | 213.884 | 214.640 | 198.413 | -0.4% | 7.5% | yes | yes |
| 2025Q2 | 238.485 | 229.896 | 204.099 | 3.7% | 15.6% | yes | yes |
| 2025Q3 | 252.410 | 260.827 | 272.340 | -3.3% | -7.6% | yes | yes |
| 2025Q4 | 202.314 | 207.626 | 195.997 | -2.6% | 3.2% | yes | yes |
| 2026Q1 | 174.614 | 170.531 | 139.170 | 2.4% | 22.7% | yes | yes |
| 2026Q2 | 174.881 | 174.714 | 153.275 | 0.1% | 13.2% | yes | yes |
| 2026Q3 | 164.233 | 162.394 | 164.920 | 1.1% | -0.4% | yes | yes |

## KCCOI: COIN Transaction Revenue

| Quarter | Actual (scaled by 1e+06) | Model | Naive | Model err | Naive err | Scored | In 80% |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2021Q2 | 1,930.400 | — | — | n/a | n/a |  |  |
| 2021Q3 | 1,089.700 | 1,403.226 | — | -25.3% | n/a |  |  |
| 2021Q4 | 2,276.600 | 1,706.073 | 615.130 | 28.8% | 130.9% |  |  |
| 2022Q1 | 1,013.000 | 1,381.712 | 4,756.270 | -31.0% | -154.7% |  |  |
| 2022Q2 | 655.200 | 1,050.252 | 450.746 | -47.2% | 37.4% |  |  |
| 2022Q3 | 365.900 | 791.249 | 423.778 | -77.1% | -14.7% |  |  |
| 2022Q4 | 322.100 | 679.175 | 204.339 | -74.6% | 45.5% | yes | yes |
| 2023Q1 | 374.700 | 427.868 | 283.543 | -13.3% | 27.9% | yes | yes |
| 2023Q2 | 327.100 | 246.547 | 435.890 | 28.3% | -28.7% | yes | no |
| 2023Q3 | 288.600 | 206.697 | 285.547 | 33.4% | 1.1% | yes | yes |
| 2023Q4 | 529.300 | 452.995 | 254.631 | 15.6% | 73.2% | yes | yes |
| 2024Q1 | 1,076.700 | 925.513 | 970.750 | 15.1% | 10.4% | yes | yes |
| 2024Q2 | 780.900 | 743.453 | 2,190.219 | 4.9% | -103.1% | yes | yes |
| 2024Q3 | 572.500 | 656.098 | 566.365 | -13.6% | 1.1% | yes | yes |
| 2024Q4 | 1,556.000 | 1,447.436 | 419.716 | 7.2% | 131.0% | yes | yes |
| 2025Q1 | 1,262.200 | 1,382.084 | 4,229.059 | -9.1% | -120.9% | yes | yes |
| 2025Q2 | 764.300 | 758.915 | 1,023.875 | 0.7% | -29.2% | yes | yes |
| 2025Q3 | 1,046.300 | 961.056 | 462.807 | 8.5% | 81.6% | yes | yes |
| 2025Q4 | 982.700 | 902.517 | 1,432.348 | 8.5% | -37.7% | yes | yes |
| 2026Q1 | 755.800 | 751.192 | 922.966 | 0.6% | -20.0% | yes | yes |
| 2026Q2 | 599.200 | 530.068 | 581.290 | 12.3% | 3.0% | yes | yes |
| 2026Q3 | 590.200 | 517.309 | 475.047 | 13.2% | 21.7% | yes | yes |

## Interpretations of the prereg

Where the frozen text left a choice, these readings were used. Each was re-run under the literal alternative in review; none changes a gate result.

- HOOD sample start: 2022Q3's report had only the August 2022 release, which also printed July; July is taken from it, so 2022Q3 counts as having two months. Literal reading: start at 2022Q4 (gate-neutral).
- Scored quarters: a quarter is scored once both model and naive have 4 earlier errors, so both are compared on the same quarters. Literal reading: each method on its own 4-error start (gate-neutral).
- HOOD net interest: 2022 releases printed no cash sweep, so the 2022Q4 base ratio uses margin only for both quarters. Literal reading: margin + sweep with a zero sweep in 2022Q3 (that quarter is unscored; KBHOD/KDHOD coverage 75% instead of 83%, still passing).
- Synthetic strikes centre on the model nowcast, and the naive baseline is scored on the same strikes.

## Caveats

- Small samples: 7 to 16 scored quarters per KPI, so a gate result can flip on one or two quarters. Model/naive ratio close to the 0.50 bar: KBHOD (0.49996), KDHOD (0.49152).
- Coverage at the edge of the 60-95% band for KCMAR, KFMAR, KBMAR, KCCOI: one more covered quarter (or one fewer) would fail the gate.
- Brier is worse for the model than the naive baseline on KFMAR, KCCOI although its errors are smaller. Both are scored on the same strikes; the model's t-distribution is shifted by the mean of its earlier errors, which a few large early misses (MARA 2023Q1, COIN 2022) pull away from zero. Brier here grades calibration on near-the-money strikes, not accuracy.
- HOOD revenue: the prior quarter's rates use only its months 1-2 (frozen rule), so a big month 3 skews the next quarter's rates too. The earnings release prints the prior quarter's full volumes; using them would be a separately labelled variant.
- HOOD's quarter-end month is never public before the report; the model sees months 1-2 only.
- MARA printed blocks won only from Q4 2023, so KCMAR has the shortest history.
- KPI values are as first printed in each quarter's release (no restatements), which is what Cboe settles on. HOOD's revenue split by line for 2022Q3-2023Q3 comes from the 10-Q/10-K (model inputs only).
- Information rule: a nowcast uses only releases published before its report date; parameters for a quarter come only from earlier quarters.
