# N3: weekly SPY put credit spread, sold at the week's last close, closed 15:40 ET on next week's expiry (real quotes)

## Verdict (2026-10-13): FAIL

- **It loses out of sample at every fill.** 2022 to 2026-10 (235 weeks): −$6.99 a lot at mid, −$9.94 at patient
  (t −0.73), −$15.62 at taker. The bar needed t >= 2.33 at patient and a positive taker mean.
- **The in-sample gain was SPY's drift, not a premium.** 2016-2021 made +$7.23 a lot at patient (t +1.23), but net of
  SPY's own move over the week the intercept is −$0.82 (t −0.19). Out of sample the intercept is −$21.54 a lot
  (t −2.21): after the market's move the weekly spread loses money. Up weeks average +$114, down weeks −$149.
- **No variant comes close.** The short strike at 0.25× or 1.0× the implied move, a 2× width, and settlement value with
  no exit cost all stay under t +0.8 out of sample (best: short at 1.0× EM, +$5.02 at patient, t +0.50). None is
  selected (prereg section 7).
- **Same lesson as N2.** A short-dated put credit spread on SPY tracks SPY. The weekly premium is larger than the 1DTE
  one, but it is still smaller than what the down weeks take back.

Data: ThetaData 1-minute NBBO on the Mac. 561 weekly entry chains (`spy_weekly`, pulled with
`fetch_thetadata_spy_weekly.py`, no failures) plus the existing `spy_0dte` pull for entry spot and exit. 534 trades
2016-01 to 2026-10 (299 in-sample, 235 out of sample). Prereg merged first (PR #57), code and tests next (PR #58).
The per-trade CSV and results JSON stay on the Mac in `data/thetadata/n3_results/`.

## Generated report

Rules frozen in `research/n3_weekly_put_spread_prereg.md` before the pull or this replay ran.
Bar: in-sample (2016-2021) mean > 0 at patient; out-of-sample (2022 on) t >= 2.33 at patient; out-of-sample mean > 0 at taker.

**N3: FAIL**

Weeks skipped: half_day 24, no_0dte_file 1, wide_leg 2

Mean credit $0.83 a share on a mean width of $3.98.

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| N3 is | mid | 299 | +9.87 | +1.68 | +5.21% | 80% | 1.29 | -386 |
| N3 is | patient | 299 | +7.23 | +1.23 | +3.97% | 79% | 1.21 | -390 |
| N3 is | taker | 299 | +5.07 | +0.84 | +3.02% | 79% | 1.14 | -395 |
| N3 oos | mid | 235 | -6.99 | -0.51 | -1.82% | 72% | 0.92 | -644 |
| N3 oos | patient | 235 | -9.94 | -0.73 | -2.56% | 71% | 0.89 | -648 |
| N3 oos | taker | 235 | -15.62 | -1.13 | -3.83% | 71% | 0.83 | -653 |

## Sensitivities (reported, never selected from)

Settlement value prices the legs at intrinsic against e's last parity spot (15:59) with no exit cost; not tradeable on SPY.

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| short 0.25 x EM is | mid | 299 | +8.97 | +1.39 | +5.07% | 74% | 1.22 | -400 |
| short 0.25 x EM oos | mid | 232 | -8.98 | -0.60 | -2.33% | 68% | 0.91 | -615 |
| short 0.25 x EM is | patient | 299 | +6.26 | +0.97 | +3.70% | 74% | 1.15 | -403 |
| short 0.25 x EM oos | patient | 232 | -11.98 | -0.80 | -3.13% | 68% | 0.88 | -619 |
| short 0.25 x EM is | taker | 299 | +4.00 | +0.61 | +2.57% | 73% | 1.09 | -410 |
| short 0.25 x EM oos | taker | 232 | -18.28 | -1.21 | -4.55% | 67% | 0.83 | -627 |
| short 1.0 x EM is | mid | 298 | +8.03 | +1.71 | +3.86% | 86% | 1.35 | -322 |
| short 1.0 x EM oos | mid | 232 | +7.79 | +0.78 | +1.70% | 81% | 1.15 | -650 |
| short 1.0 x EM is | patient | 298 | +5.54 | +1.18 | +2.80% | 86% | 1.23 | -325 |
| short 1.0 x EM oos | patient | 232 | +5.02 | +0.50 | +1.06% | 81% | 1.10 | -654 |
| short 1.0 x EM is | taker | 298 | +4.45 | +0.93 | +2.40% | 86% | 1.18 | -343 |
| short 1.0 x EM oos | taker | 232 | +2.08 | +0.21 | +0.43% | 81% | 1.04 | -664 |
| width 2W is | mid | 299 | +19.40 | +2.05 | +4.65% | 80% | 1.38 | -634 |
| width 2W oos | mid | 234 | +1.53 | +0.07 | -0.20% | 74% | 1.01 | -1169 |
| width 2W is | patient | 299 | +16.83 | +1.78 | +4.07% | 80% | 1.33 | -638 |
| width 2W oos | patient | 234 | -1.35 | -0.06 | -0.55% | 74% | 0.99 | -1173 |
| width 2W is | taker | 299 | +14.53 | +1.51 | +3.60% | 80% | 1.27 | -656 |
| width 2W oos | taker | 234 | -6.89 | -0.32 | -1.19% | 74% | 0.95 | -1183 |
| settlement value is | mid | 299 | +8.28 | +1.31 | +4.67% | 79% | 1.22 | -435 |
| settlement value oos | mid | 235 | -3.18 | -0.23 | -1.15% | 74% | 0.96 | -649 |
| settlement value is | patient | 299 | +6.76 | +1.07 | +3.95% | 79% | 1.18 | -437 |
| settlement value oos | patient | 235 | -5.01 | -0.36 | -1.60% | 74% | 0.94 | -651 |
| settlement value is | taker | 299 | +5.71 | +0.91 | +3.44% | 79% | 1.15 | -437 |
| settlement value oos | taker | 235 | -9.24 | -0.67 | -2.51% | 74% | 0.90 | -653 |

## Net of SPY's move (patient fills, descriptive)

OLS of P&L per lot on SPY's return from 15:50 on d to 15:40 on e.

| Period | n | alpha $/lot | alpha t | beta ($ per 100% move) | up weeks (n, mean $) | down weeks (n, mean $) |
|---|---|---|---|---|---|---|
| is | 299 | -0.82 | -0.19 | +2769 | 181, +56.65 | 118, -68.56 |
| oos | 235 | -21.54 | -2.21 | +6492 | 124, +114.29 | 111, -148.72 |

## By year

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| 2016 | patient | 50 | +6.78 | +0.73 | +4.46% | 76% | 1.29 | -172 |
| 2017 | patient | 50 | +22.40 | +3.86 | +12.12% | 92% | 3.70 | -147 |
| 2018 | patient | 50 | -13.33 | -0.87 | -5.18% | 68% | 0.73 | -246 |
| 2019 | patient | 50 | +20.93 | +1.72 | +8.92% | 84% | 1.85 | -253 |
| 2020 | patient | 49 | -0.15 | -0.01 | +0.67% | 76% | 1.00 | -239 |
| 2021 | patient | 50 | +6.63 | +0.32 | +2.74% | 78% | 1.12 | -390 |
| 2022 | patient | 48 | -46.17 | -1.82 | -15.23% | 54% | 0.54 | -397 |
| 2023 | patient | 50 | -1.40 | -0.06 | +0.82% | 76% | 0.98 | -410 |
| 2024 | patient | 50 | +3.68 | +0.13 | +1.75% | 76% | 1.05 | -493 |
| 2025 | patient | 48 | -18.85 | -0.52 | -3.09% | 71% | 0.83 | -582 |
| 2026 | patient | 39 | +17.19 | +0.43 | +3.82% | 79% | 1.19 | -648 |
| 2016 | taker | 50 | +5.96 | +0.63 | +3.94% | 74% | 1.25 | -172 |
| 2017 | taker | 50 | +22.22 | +3.80 | +12.01% | 92% | 3.65 | -148 |
| 2018 | taker | 50 | -17.10 | -1.08 | -6.79% | 68% | 0.67 | -254 |
| 2019 | taker | 50 | +20.68 | +1.70 | +8.82% | 84% | 1.84 | -254 |
| 2020 | taker | 49 | -6.38 | -0.35 | -2.12% | 76% | 0.88 | -316 |
| 2021 | taker | 50 | +4.80 | +0.23 | +2.17% | 78% | 1.09 | -395 |
| 2022 | taker | 48 | -53.74 | -2.08 | -17.12% | 54% | 0.49 | -398 |
| 2023 | taker | 50 | -2.18 | -0.09 | +0.62% | 76% | 0.97 | -411 |
| 2024 | taker | 50 | +0.84 | +0.03 | +1.09% | 76% | 1.01 | -500 |
| 2025 | taker | 48 | -29.60 | -0.81 | -5.46% | 71% | 0.74 | -591 |
| 2026 | taker | 39 | +10.15 | +0.25 | +2.55% | 79% | 1.11 | -653 |
