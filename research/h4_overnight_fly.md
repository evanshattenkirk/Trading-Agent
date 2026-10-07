# H4 overnight 1DTE iron fly: real-quote replay

Rules frozen in `research/book_h_candidates_prereg.md` section 4 (commit 300b931) before this ran.
Bar: in-sample (2016-2021) mean > 0 at patient; out-of-sample (2022 on) t >= 2.33 at patient; out-of-sample mean > 0 at taker.

**Result: FAIL**

Nights skipped: ex_div_eve 42, expiry_not_next_session 794, half_day 21, missing_leg 9, no_0dte_file 1, no_exit_quote 1, wide_leg 21

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| H4 is | mid | 747 | +2.11 | +1.18 | +0.91% | 65% | 1.12 | -242 |
| H4 is | patient | 747 | -3.42 | -1.92 | -1.22% | 59% | 0.82 | -248 |
| H4 is | taker | 747 | -7.28 | -3.92 | -3.36% | 57% | 0.67 | -256 |
| H4 oos | mid | 1068 | +0.77 | +0.49 | +0.76% | 60% | 1.04 | -228 |
| H4 oos | patient | 1068 | -5.08 | -3.23 | -2.54% | 56% | 0.77 | -234 |
| H4 oos | taker | 1068 | -10.71 | -6.43 | -5.92% | 52% | 0.58 | -250 |

## Sensitivities (reported, never selected from)

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| exit 09:35 is | patient | 747 | -3.23 | -1.96 | -1.17% | 58% | 0.82 | -231 |
| exit 09:35 oos | patient | 1068 | -5.56 | -3.79 | -2.71% | 56% | 0.73 | -229 |
| exit 09:35 is | taker | 747 | -7.13 | -4.12 | -3.30% | 54% | 0.65 | -246 |
| exit 09:35 oos | taker | 1068 | -9.98 | -6.53 | -5.42% | 52% | 0.58 | -248 |
| exit 10:00 is | patient | 747 | -3.39 | -1.72 | -1.12% | 60% | 0.84 | -264 |
| exit 10:00 oos | patient | 1068 | -3.76 | -2.13 | -1.67% | 57% | 0.84 | -238 |
| exit 10:00 is | taker | 747 | -8.64 | -4.17 | -3.92% | 56% | 0.65 | -279 |
| exit 10:00 oos | taker | 1068 | -12.82 | -6.66 | -7.18% | 51% | 0.58 | -268 |
| wings +/-3 is | patient | 735 | -5.63 | -4.80 | -4.65% | 54% | 0.61 | -132 |
| wings +/-3 oos | patient | 1066 | -5.75 | -7.58 | -7.45% | 46% | 0.54 | -114 |
| wings +/-3 is | taker | 735 | -9.50 | -7.60 | -8.88% | 48% | 0.46 | -138 |
| wings +/-3 oos | taker | 1066 | -11.59 | -13.79 | -15.19% | 37% | 0.32 | -143 |
| wings +/-10 is | patient | 719 | +0.65 | +0.23 | +0.12% | 63% | 1.03 | -509 |
| wings +/-10 oos | patient | 1041 | -2.71 | -0.88 | -0.45% | 61% | 0.93 | -533 |
| wings +/-10 is | taker | 719 | -2.94 | -1.02 | -0.59% | 62% | 0.89 | -513 |
| wings +/-10 oos | taker | 1041 | -7.97 | -2.49 | -1.46% | 59% | 0.80 | -546 |
| weekday nights only is | patient | 548 | -3.17 | -1.78 | -0.97% | 59% | 0.81 | -225 |
| weekday nights only oos | patient | 824 | -6.33 | -3.71 | -2.98% | 55% | 0.71 | -226 |
| weekday nights only is | taker | 548 | -6.47 | -3.46 | -2.83% | 57% | 0.66 | -231 |
| weekday nights only oos | taker | 824 | -11.49 | -6.38 | -6.06% | 51% | 0.54 | -232 |

## Weekday vs weekend/holiday nights

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| weekday | mid | 1372 | +0.61 | +0.49 | +0.58% | 62% | 1.04 | -221 |
| weekend/holiday | mid | 443 | +3.49 | +1.21 | +1.55% | 62% | 1.16 | -242 |
| weekday | patient | 1372 | -5.07 | -4.06 | -2.18% | 57% | 0.74 | -226 |
| weekend/holiday | patient | 443 | -2.32 | -0.80 | -1.42% | 60% | 0.91 | -248 |
| weekday | taker | 1372 | -9.49 | -7.21 | -4.77% | 53% | 0.58 | -232 |
| weekend/holiday | taker | 443 | -8.71 | -2.84 | -5.17% | 56% | 0.70 | -256 |

## By year

| Row | Fills | trades | mean $/lot | t | mean % of max risk | win | PF | worst $ |
|---|---|---|---|---|---|---|---|---|
| 2016 | patient | 65 | -0.62 | -0.11 | +0.36% | 58% | 0.96 | -148 |
| 2017 | patient | 99 | -2.12 | -0.72 | -0.44% | 61% | 0.79 | -141 |
| 2018 | patient | 129 | -3.53 | -1.01 | -0.44% | 59% | 0.79 | -133 |
| 2019 | patient | 150 | -6.92 | -1.67 | -2.02% | 55% | 0.69 | -192 |
| 2020 | patient | 152 | -4.80 | -0.97 | -3.47% | 57% | 0.82 | -225 |
| 2021 | patient | 152 | -0.51 | -0.12 | -0.02% | 65% | 0.97 | -248 |
| 2022 | patient | 160 | -7.27 | -2.32 | -4.18% | 52% | 0.62 | -116 |
| 2023 | patient | 242 | -1.65 | -0.52 | +0.57% | 60% | 0.91 | -226 |
| 2024 | patient | 238 | -5.21 | -1.55 | -1.95% | 56% | 0.76 | -192 |
| 2025 | patient | 241 | -7.51 | -2.16 | -5.45% | 57% | 0.69 | -234 |
| 2026 | patient | 187 | -4.36 | -1.02 | -2.14% | 54% | 0.83 | -167 |
| 2016 | taker | 65 | -2.47 | -0.44 | -0.23% | 57% | 0.86 | -150 |
| 2017 | taker | 99 | -2.80 | -0.93 | -0.61% | 60% | 0.74 | -153 |
| 2018 | taker | 129 | -7.85 | -2.19 | -2.08% | 55% | 0.59 | -139 |
| 2019 | taker | 150 | -8.35 | -1.96 | -2.54% | 55% | 0.65 | -198 |
| 2020 | taker | 152 | -15.02 | -2.82 | -10.60% | 51% | 0.56 | -231 |
| 2021 | taker | 152 | -2.98 | -0.69 | -1.17% | 64% | 0.85 | -256 |
| 2022 | taker | 160 | -15.95 | -4.55 | -9.76% | 44% | 0.37 | -157 |
| 2023 | taker | 242 | -3.65 | -1.13 | -0.35% | 58% | 0.82 | -232 |
| 2024 | taker | 238 | -8.91 | -2.52 | -3.95% | 54% | 0.64 | -204 |
| 2025 | taker | 241 | -16.26 | -4.34 | -10.72% | 49% | 0.46 | -250 |
| 2026 | taker | 187 | -10.48 | -2.34 | -6.18% | 51% | 0.64 | -174 |

## Short ATM straddle overnight, % of entry premium (descriptive)

| Fills | IS n | IS mean | OOS n | OOS mean |
|---|---|---|---|---|
| mid | 747 | +3.38% | 1068 | +1.83% |
| patient | 747 | +1.54% | 1068 | +0.92% |
| taker | 747 | +0.26% | 1068 | -0.19% |
