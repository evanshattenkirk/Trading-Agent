# N1 / N2: 1DTE SPY credit structures sold at 15:50 ET, closed 15:40 ET on expiry day (real quotes)

## Verdict (2026-10-12): both FAIL

- **N1, the $5 iron fly, loses at every realistic fill.** Out of sample (2022 to 2026-10) it makes +$2.46 a lot at mid
  but −$3.16 at patient (t −0.71) and −$8.72 at taker. Paying the spread once instead of twice (H4) shrank the loss
  but did not create an edge: the 1DTE premium is small. The short ATM straddle with the same timing earns only
  +3.3% of its premium at mid out of sample and +1.0% at taker, against +6.8% at mid for the 09:45 0DTE straddle.
- **N2, the bull put spread, is SPY's drift, not a premium.** It passed in-sample (+$11.60 a lot at patient, t +2.42;
  2016-2021 was a strong bull market), then went flat out of sample: −$0.31 at patient (t −0.06), −$3.37 at taker.
  Its by-year results follow SPY: 2022 −$28, 2026 −$15.
- **The last 20 minutes don't hold the edge either.** Valuing N1 at settlement with no exit cost (not tradeable on
  SPY) gives +$1.09 a lot out of sample at patient, t +0.21. So a cash-settled XSP version (N4) has no premium to
  pay its higher index fees with; it is dropped without a pull.
- **Weekend and holiday nights look better but are not selected.** N1 on those nights: +$13.96 a lot at mid (t 2.07),
  +$8.41 at patient (t 1.25), +$2.49 at taker (433 nights). It matches Jones & Shemesh and H4's weekend subgroup, but
  it is a subgroup of a failed test with no data left to confirm it out of sample.

Data: ThetaData 1-minute NBBO already on the Mac (`spy_next` for entry, `spy_0dte` for exit), 2,704 trade dates
2016-01 to 2026-10, 1,798 N1 and 1,811 N2 trades after the calendar rules. Run on the Mac's sandbox VM from
`n1_n2_1dte_close.py` (merged prereg first, PR #55); results `n1_n2_1dte_close_results.json`.

## Generated report

Rules frozen in `research/n1_n2_1dte_close_prereg.md` before this script existed.
Bar per candidate: in-sample (2016-2021) mean > 0 at patient; out-of-sample (2022 on) t >= 2.58 at patient; out-of-sample mean > 0 at taker.

**N1: FAIL**
**N2: FAIL**

Nights skipped: N1 missing_leg 9, N1 wide_leg 21, N2 missing_leg 5, N2 wide_leg 12, ex_div_eve 42, exit_half_day 20, expiry_not_next_session 792, half_day 21, no_0dte_file 1

## N1 iron fly ($5 wings)

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| N1 is | mid | 737 | +1.74 | +0.40 | -0.51% | 59% | 1.04 | -401 |
| N1 is | patient | 737 | -3.62 | -0.84 | -2.51% | 58% | 0.93 | -406 |
| N1 is | taker | 737 | -8.32 | -1.89 | -4.60% | 57% | 0.84 | -413 |
| N1 oos | mid | 1061 | +2.46 | +0.55 | +1.78% | 51% | 1.04 | -300 |
| N1 oos | patient | 1061 | -3.16 | -0.71 | -1.36% | 51% | 0.95 | -303 |
| N1 oos | taker | 1061 | -8.72 | -1.94 | -4.63% | 50% | 0.87 | -303 |

### N1 sensitivities (reported, never selected from)

Settlement value prices the legs at intrinsic against the last parity spot on e (15:59), with no exit cost. It is not tradeable on SPY; it shows how much sits in the last 20 minutes.

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| exit 15:25 is | mid | 737 | +2.18 | +0.53 | -0.46% | 61% | 1.05 | -386 |
| exit 15:25 oos | mid | 1061 | +2.72 | +0.63 | +2.00% | 53% | 1.05 | -296 |
| exit 15:25 is | patient | 737 | -3.20 | -0.77 | -2.47% | 59% | 0.93 | -391 |
| exit 15:25 oos | patient | 1061 | -2.96 | -0.69 | -1.19% | 51% | 0.95 | -302 |
| exit 15:25 is | taker | 737 | -7.52 | -1.78 | -4.42% | 58% | 0.85 | -397 |
| exit 15:25 oos | taker | 1061 | -8.31 | -1.92 | -4.37% | 50% | 0.88 | -303 |
| wings 3 is | mid | 726 | +2.78 | +0.98 | +0.91% | 54% | 1.09 | -231 |
| wings 3 oos | mid | 1059 | +3.57 | +1.47 | +5.27% | 44% | 1.11 | -135 |
| wings 3 is | patient | 726 | -2.60 | -0.92 | -3.94% | 53% | 0.92 | -236 |
| wings 3 oos | patient | 1059 | -2.00 | -0.82 | -2.85% | 43% | 0.94 | -138 |
| wings 3 is | taker | 726 | -6.56 | -2.27 | -7.77% | 51% | 0.82 | -238 |
| wings 3 oos | taker | 1059 | -7.04 | -2.87 | -9.90% | 41% | 0.82 | -143 |
| wings 10 is | mid | 711 | +3.88 | +0.63 | +0.11% | 64% | 1.07 | -678 |
| wings 10 oos | mid | 1034 | +3.59 | +0.48 | +0.63% | 59% | 1.04 | -712 |
| wings 10 is | patient | 711 | -1.41 | -0.23 | -0.61% | 62% | 0.98 | -684 |
| wings 10 oos | patient | 1034 | -1.86 | -0.25 | -0.30% | 58% | 0.98 | -719 |
| wings 10 is | taker | 711 | -6.78 | -1.07 | -1.51% | 62% | 0.89 | -695 |
| wings 10 oos | taker | 1034 | -7.92 | -1.04 | -1.43% | 57% | 0.92 | -719 |
| settlement value is | mid | 737 | +1.37 | +0.29 | -1.52% | 57% | 1.03 | -430 |
| settlement value oos | mid | 1061 | +4.35 | +0.85 | +2.84% | 50% | 1.06 | -319 |
| settlement value is | patient | 737 | -1.52 | -0.32 | -2.56% | 56% | 0.97 | -432 |
| settlement value oos | patient | 1061 | +1.09 | +0.21 | +0.93% | 50% | 1.01 | -321 |
| settlement value is | taker | 737 | -3.34 | -0.69 | -3.18% | 56% | 0.94 | -432 |
| settlement value oos | taker | 1061 | -1.87 | -0.36 | -0.93% | 49% | 0.97 | -321 |

### N1 weekday vs weekend/holiday nights

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| weekday | mid | 1365 | -1.58 | -0.44 | -0.94% | 54% | 0.97 | -401 |
| weekend/holiday | mid | 433 | +13.96 | +2.07 | +6.47% | 57% | 1.26 | -364 |
| weekday | patient | 1365 | -7.08 | -1.97 | -3.54% | 53% | 0.88 | -406 |
| weekend/holiday | patient | 433 | +8.41 | +1.25 | +3.54% | 56% | 1.15 | -370 |
| weekday | taker | 1365 | -12.06 | -3.32 | -6.18% | 52% | 0.81 | -413 |
| weekend/holiday | taker | 433 | +2.49 | +0.36 | +0.31% | 55% | 1.04 | -380 |

### N1 by year

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| 2016 | patient | 64 | +3.87 | +0.31 | +0.66% | 70% | 1.11 | -406 |
| 2017 | patient | 98 | +8.32 | +1.40 | +2.20% | 64% | 1.45 | -275 |
| 2018 | patient | 126 | -13.37 | -1.18 | -3.94% | 57% | 0.77 | -370 |
| 2019 | patient | 147 | +1.83 | +0.20 | +0.38% | 61% | 1.04 | -321 |
| 2020 | patient | 151 | -15.12 | -1.33 | -8.78% | 47% | 0.78 | -304 |
| 2021 | patient | 151 | -0.23 | -0.02 | -2.28% | 60% | 1.00 | -295 |
| 2022 | patient | 159 | -9.14 | -0.79 | -3.14% | 45% | 0.87 | -289 |
| 2023 | patient | 241 | -1.01 | -0.11 | +1.60% | 56% | 0.98 | -303 |
| 2024 | patient | 235 | +2.94 | +0.31 | -0.11% | 56% | 1.05 | -289 |
| 2025 | patient | 239 | +3.50 | +0.38 | -0.16% | 50% | 1.06 | -259 |
| 2026 | patient | 187 | -17.03 | -1.60 | -6.76% | 42% | 0.77 | -239 |
| 2016 | taker | 64 | +0.77 | +0.06 | -0.27% | 69% | 1.02 | -413 |
| 2017 | taker | 98 | +7.32 | +1.21 | +1.95% | 64% | 1.38 | -279 |
| 2018 | taker | 126 | -21.92 | -1.87 | -7.31% | 56% | 0.66 | -380 |
| 2019 | taker | 147 | -0.11 | -0.01 | -0.27% | 61% | 1.00 | -341 |
| 2020 | taker | 151 | -23.35 | -2.03 | -13.41% | 46% | 0.68 | -308 |
| 2021 | taker | 151 | -3.94 | -0.39 | -3.81% | 58% | 0.93 | -298 |
| 2022 | taker | 159 | -17.43 | -1.50 | -8.12% | 45% | 0.77 | -289 |
| 2023 | taker | 241 | -3.38 | -0.35 | +0.52% | 55% | 0.95 | -303 |
| 2024 | taker | 235 | -1.70 | -0.18 | -2.53% | 54% | 0.97 | -295 |
| 2025 | taker | 239 | -4.35 | -0.46 | -4.83% | 49% | 0.93 | -266 |
| 2026 | taker | 187 | -22.57 | -2.12 | -10.71% | 41% | 0.71 | -245 |

## N2 bull put spread ($5 wide)

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| N2 is | mid | 744 | +14.28 | +2.99 | +3.52% | 75% | 1.34 | -422 |
| N2 is | patient | 744 | +11.60 | +2.42 | +2.84% | 74% | 1.27 | -426 |
| N2 is | taker | 744 | +9.15 | +1.87 | +2.19% | 74% | 1.21 | -437 |
| N2 oos | mid | 1067 | +2.58 | +0.48 | +0.90% | 65% | 1.04 | -413 |
| N2 oos | patient | 1067 | -0.31 | -0.06 | +0.09% | 65% | 1.00 | -416 |
| N2 oos | taker | 1067 | -3.37 | -0.62 | -0.77% | 65% | 0.96 | -423 |

### N2 sensitivities (reported, never selected from)

Settlement value prices the legs at intrinsic against the last parity spot on e (15:59), with no exit cost. It is not tradeable on SPY; it shows how much sits in the last 20 minutes.

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| exit 15:25 is | mid | 744 | +14.72 | +3.15 | +3.64% | 75% | 1.36 | -413 |
| exit 15:25 oos | mid | 1067 | +5.52 | +1.06 | +1.75% | 65% | 1.08 | -415 |
| exit 15:25 is | patient | 744 | +12.03 | +2.57 | +2.96% | 74% | 1.29 | -416 |
| exit 15:25 oos | patient | 1067 | +2.62 | +0.50 | +0.93% | 65% | 1.04 | -418 |
| exit 15:25 is | taker | 744 | +9.75 | +2.05 | +2.35% | 74% | 1.23 | -432 |
| exit 15:25 oos | taker | 1067 | -0.36 | -0.07 | +0.08% | 65% | 1.00 | -423 |
| wings 3 is | mid | 738 | +13.09 | +3.93 | +5.87% | 73% | 1.42 | -269 |
| wings 3 oos | mid | 1066 | +0.98 | +0.27 | +0.71% | 62% | 1.02 | -228 |
| wings 3 is | patient | 738 | +10.37 | +3.10 | +4.61% | 72% | 1.33 | -272 |
| wings 3 oos | patient | 1066 | -1.94 | -0.54 | -0.77% | 62% | 0.96 | -231 |
| wings 3 is | taker | 738 | +8.33 | +2.45 | +3.65% | 71% | 1.25 | -275 |
| wings 3 oos | taker | 1066 | -4.76 | -1.31 | -2.17% | 62% | 0.91 | -267 |
| wings 10 is | mid | 735 | +13.28 | +1.98 | +1.45% | 76% | 1.24 | -818 |
| wings 10 oos | mid | 1059 | +7.29 | +0.93 | +1.00% | 68% | 1.08 | -879 |
| wings 10 is | patient | 735 | +10.66 | +1.59 | +1.15% | 75% | 1.19 | -821 |
| wings 10 oos | patient | 1059 | +4.52 | +0.58 | +0.66% | 68% | 1.05 | -883 |
| wings 10 is | taker | 735 | +7.70 | +1.12 | +0.79% | 75% | 1.13 | -831 |
| wings 10 oos | taker | 1059 | +1.28 | +0.16 | +0.25% | 67% | 1.01 | -884 |
| settlement value is | mid | 744 | +11.61 | +2.25 | +2.81% | 73% | 1.24 | -448 |
| settlement value oos | mid | 1067 | +5.25 | +0.92 | +1.61% | 66% | 1.07 | -418 |
| settlement value is | patient | 744 | +10.13 | +1.96 | +2.43% | 73% | 1.21 | -449 |
| settlement value oos | patient | 1067 | +3.57 | +0.63 | +1.14% | 65% | 1.05 | -419 |
| settlement value is | taker | 744 | +9.17 | +1.78 | +2.17% | 73% | 1.19 | -449 |
| settlement value oos | taker | 1067 | +1.90 | +0.33 | +0.66% | 65% | 1.02 | -419 |

### N2 weekday vs weekend/holiday nights

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| weekday | mid | 1376 | +5.51 | +1.29 | +1.44% | 69% | 1.09 | -419 |
| weekend/holiday | mid | 435 | +13.34 | +1.75 | +3.69% | 70% | 1.23 | -422 |
| weekday | patient | 1376 | +2.71 | +0.64 | +0.69% | 68% | 1.04 | -422 |
| weekend/holiday | patient | 435 | +10.52 | +1.38 | +2.91% | 70% | 1.18 | -426 |
| weekday | taker | 1376 | -0.01 | -0.00 | -0.05% | 68% | 1.00 | -429 |
| weekend/holiday | taker | 435 | +7.38 | +0.96 | +2.04% | 69% | 1.12 | -437 |

### N2 by year

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| 2016 | patient | 65 | +2.20 | +0.16 | +0.52% | 75% | 1.06 | -422 |
| 2017 | patient | 98 | +17.59 | +3.73 | +3.95% | 79% | 2.87 | -311 |
| 2018 | patient | 132 | -6.82 | -0.55 | -1.73% | 70% | 0.87 | -426 |
| 2019 | patient | 147 | +16.10 | +1.69 | +3.88% | 75% | 1.46 | -408 |
| 2020 | patient | 151 | +11.70 | +0.83 | +3.06% | 73% | 1.18 | -409 |
| 2021 | patient | 151 | +23.38 | +2.21 | +5.89% | 74% | 1.55 | -394 |
| 2022 | patient | 159 | -28.35 | -1.85 | -7.87% | 55% | 0.71 | -383 |
| 2023 | patient | 241 | +10.20 | +1.01 | +3.02% | 68% | 1.17 | -390 |
| 2024 | patient | 241 | +12.91 | +1.24 | +3.53% | 71% | 1.21 | -416 |
| 2025 | patient | 239 | +6.26 | +0.55 | +2.04% | 67% | 1.09 | -406 |
| 2026 | patient | 187 | -15.43 | -1.09 | -3.84% | 60% | 0.83 | -385 |
| 2016 | taker | 65 | +0.55 | +0.04 | +0.13% | 75% | 1.02 | -429 |
| 2017 | taker | 98 | +17.14 | +3.58 | +3.84% | 78% | 2.76 | -315 |
| 2018 | taker | 132 | -12.05 | -0.93 | -3.07% | 69% | 0.79 | -437 |
| 2019 | taker | 147 | +15.24 | +1.58 | +3.67% | 75% | 1.43 | -410 |
| 2020 | taker | 151 | +7.72 | +0.54 | +1.94% | 73% | 1.11 | -413 |
| 2021 | taker | 151 | +21.68 | +2.02 | +5.42% | 74% | 1.50 | -395 |
| 2022 | taker | 159 | -33.32 | -2.14 | -9.30% | 55% | 0.68 | -383 |
| 2023 | taker | 241 | +8.98 | +0.88 | +2.68% | 68% | 1.15 | -390 |
| 2024 | taker | 241 | +10.81 | +1.03 | +2.95% | 70% | 1.17 | -423 |
| 2025 | taker | 239 | +1.70 | +0.15 | +0.80% | 67% | 1.02 | -406 |
| 2026 | taker | 187 | -18.61 | -1.31 | -4.76% | 60% | 0.80 | -385 |

## Descriptive

Short ATM straddle with the same timing, % of entry premium (not tradeable at Level 3), and SPY's own move from 15:50 on d to 15:40 on e.

| Row | IS n | IS mean | OOS n | OOS mean |
|---|---|---|---|---|
| straddle mid | 754 | +4.761% | 1071 | +3.342% |
| straddle patient | 754 | +2.990% | 1071 | +2.447% |
| straddle taker | 754 | +0.927% | 1071 | +1.019% |
| spy move | 754 | +0.095% | 1074 | +0.046% |
