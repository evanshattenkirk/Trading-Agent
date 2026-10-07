# X1: short iron fly through earnings, on real single-name quotes

Rules: `research/x1_earnings_fly_prereg.md`. Bar, all at taker fills: PF >= 1.1, t > 2.50 clustered by entry session, mean > 0 in 2018-2021 and in 2022-2026.

**Verdict: FAIL.**

Events: 1050 kept reports in the sample; EDGAR filings by status: {'kept': 1082, 'dropped': 61}.
Date sanity check: {'events': 1050, 'event_median': 0.029445901812416575, 'other_median': 0.004491468215710892, 'ratio': 6.555963528677892, 'valid': True}.

## X1 (wings 1.5 x straddle, exit 10:00 ET)

- **mid**: 698 trades, win 54%, mean -2.73% of max loss, PF 0.88, t -1.38, P&L per lot $-17,848, worst -109%
  - 2018-2021: 300 trades, win 57%, mean +0.37% of max loss, PF 1.32, t 0.13, P&L per lot $+11,904, worst -103%
  - 2022-2026: 398 trades, win 51%, mean -5.07% of max loss, PF 0.72, t -1.84, P&L per lot $-29,752, worst -109%
- **mid1**: 698 trades, win 52%, mean -4.83% of max loss, PF 0.85, t -2.45, P&L per lot $-22,628, worst -112%
  - 2018-2021: 300 trades, win 54%, mean -2.33% of max loss, PF 1.26, t -0.83, P&L per lot $+9,844, worst -105%
  - 2022-2026: 398 trades, win 50%, mean -6.71% of max loss, PF 0.70, t -2.47, P&L per lot $-32,473, worst -112%
- **mid_frac**: 698 trades, win 51%, mean -5.53% of max loss, PF 0.80, t -2.78, P&L per lot $-30,915, worst -128%
  - 2018-2021: 300 trades, win 54%, mean -2.62% of max loss, PF 1.19, t -0.92, P&L per lot $+7,651, worst -115%
  - 2022-2026: 398 trades, win 49%, mean -7.73% of max loss, PF 0.66, t -2.80, P&L per lot $-38,566, worst -128%
- **taker**: 698 trades, win 47%, mean -10.60% of max loss, PF 0.67, t -5.24, P&L per lot $-55,182, worst -161%
  - 2018-2021: 300 trades, win 48%, mean -8.02% of max loss, PF 0.99, t -2.77, P&L per lot $-248, worst -142%
  - 2022-2026: 398 trades, win 47%, mean -12.54% of max loss, PF 0.55, t -4.48, P&L per lot $-54,934, worst -161%

By year (mid1 / taker):

- 2018: 70 trades, win 53%, mean -5.30% of max loss, PF 0.81, t -0.90, P&L per lot $-1,101, worst -104% / 70 trades, win 47%, mean -13.41% of max loss, PF 0.56, t -2.33, P&L per lot $-2,958, worst -142%
- 2019: 93 trades, win 55%, mean +3.19% of max loss, PF 1.58, t 0.68, P&L per lot $+4,886, worst -100% / 93 trades, win 51%, mean -1.21% of max loss, PF 1.29, t -0.25, P&L per lot $+2,791, worst -108%
- 2020: 54 trades, win 57%, mean -2.32% of max loss, PF 2.05, t -0.31, P&L per lot $+9,576, worst -105% / 54 trades, win 50%, mean -7.25% of max loss, PF 1.71, t -0.96, P&L per lot $+7,244, worst -124%
- 2021: 83 trades, win 51%, mean -6.03% of max loss, PF 0.77, t -1.13, P&L per lot $-3,517, worst -101% / 83 trades, win 43%, mean -11.59% of max loss, PF 0.59, t -2.05, P&L per lot $-7,325, worst -117%
- 2022: 70 trades, win 49%, mean -8.73% of max loss, PF 0.88, t -1.57, P&L per lot $-1,746, worst -100% / 70 trades, win 47%, mean -14.12% of max loss, PF 0.68, t -2.48, P&L per lot $-5,425, worst -109%
- 2023: 96 trades, win 55%, mean -1.56% of max loss, PF 1.00, t -0.28, P&L per lot $-26, worst -100% / 96 trades, win 51%, mean -6.91% of max loss, PF 0.79, t -1.20, P&L per lot $-3,367, worst -123%
- 2024: 97 trades, win 43%, mean -13.31% of max loss, PF 0.59, t -2.36, P&L per lot $-13,822, worst -112% / 97 trades, win 41%, mean -19.49% of max loss, PF 0.48, t -3.37, P&L per lot $-20,023, worst -161%
- 2025: 88 trades, win 59%, mean +3.18% of max loss, PF 0.79, t 0.57, P&L per lot $-5,819, worst -104% / 88 trades, win 56%, mean -2.79% of max loss, PF 0.62, t -0.49, P&L per lot $-12,001, worst -125%
- 2026: 47 trades, win 38%, mean -19.15% of max loss, PF 0.41, t -2.21, P&L per lot $-11,060, worst -97% / 47 trades, win 36%, mean -25.59% of max loss, PF 0.33, t -2.83, P&L per lot $-14,118, worst -118%

Skipped events: prior VIX close above 30 81, call wing: spread 11% of mid 17, call wing: spread 9% of mid 16, call wing: spread 12% of mid 12, call wing: spread 10% of mid 12, ATM put: spread 8% of mid 10, ATM call: spread 11% of mid 10, call wing: spread 14% of mid 10, put wing: spread 9% of mid 9, ATM call: spread 9% of mid 9, call wing: spread 13% of mid 8, ATM put: spread 11% of mid 8, call wing: spread 8% of mid 7, call wing: spread 22% of mid 6, put wing: spread 10% of mid 6, no quotes on disk 6, call wing: spread 15% of mid 6, call wing: spread 16% of mid 5, call wing: spread 18% of mid 5, call wing: spread 17% of mid 5, ATM call: spread 13% of mid 5, put wing: spread 12% of mid 5, put wing: spread 8% of mid 4, put wing: spread 11% of mid 4, ATM call: spread 10% of mid 4, put wing: spread 14% of mid 4, ATM put: spread 9% of mid 4, put wing: spread 17% of mid 4, put wing: spread 13% of mid 4, call wing: spread 19% of mid 3, call wing: spread 23% of mid 3, call wing: spread 20% of mid 3, call wing: spread 29% of mid 3, put wing: spread 19% of mid 3, ATM call: spread 14% of mid 3, ATM call: spread 8% of mid 3, ATM call: spread 16% of mid 3, put wing: spread 22% of mid 3, ATM put: spread 15% of mid 2, ATM put: spread 13% of mid 2, call wing: spread 21% of mid 2, ATM put: spread 10% of mid 2, ATM call: spread 17% of mid 1, call wing: spread 47% of mid 1, call wing: spread 36% of mid 1, call wing: spread 32% of mid 1, call wing: spread 40% of mid 1, ATM put: spread 16% of mid 1, call wing: spread 62% of mid 1, put wing: spread 24% of mid 1, ATM call: spread 68% of mid 1, call wing: spread 124% of mid 1, ATM put: spread 12% of mid 1, ATM call: spread 28% of mid 1, ATM put: spread 21% of mid 1, ATM call: spread 15% of mid 1, ATM call: spread 47% of mid 1, call wing: spread 28% of mid 1, ATM call: spread 26% of mid 1, put wing: spread 83% of mid 1, call wing: spread 52% of mid 1, ATM put: spread 20% of mid 1, call wing: spread 50% of mid 1, put wing: spread 25% of mid 1, put wing: spread 40% of mid 1, ATM put: spread 14% of mid 1, ATM call: spread 12% of mid 1, call wing: spread 33% of mid 1, put wing: spread 15% of mid 1, put wing: spread 139% of mid 1, call wing: spread 31% of mid 1, ATM call: spread 21% of mid 1, call wing: spread 46% of mid 1

## Sensitivities (reported, never selected from)

- exit_0945: mid1 698 trades, win 50%, mean -4.20% of max loss, PF 0.90, t -2.24, P&L per lot $-14,318, worst -103%; taker 698 trades, win 47%, mean -9.74% of max loss, PF 0.71, t -5.08, P&L per lot $-46,550, worst -117%
- exit_1545: mid1 698 trades, win 50%, mean -5.02% of max loss, PF 0.92, t -2.06, P&L per lot $-12,895, worst -110%; taker 698 trades, win 49%, mean -8.95% of max loss, PF 0.80, t -3.62, P&L per lot $-36,521, worst -140%
- wings_1.0x: mid1 757 trades, win 47%, mean -5.43% of max loss, PF 0.90, t -2.21, P&L per lot $-10,176, worst -177%; taker 757 trades, win 40%, mean -19.00% of max loss, PF 0.57, t -7.36, P&L per lot $-59,028, worst -277%
- wings_2.0x: mid1 676 trades, win 58%, mean -2.53% of max loss, PF 0.90, t -1.62, P&L per lot $-14,395, worst -102%; taker 676 trades, win 54%, mean -5.70% of max loss, PF 0.77, t -3.61, P&L per lot $-37,080, worst -120%
- no_vix_filter: mid1 745 trades, win 53%, mean -3.50% of max loss, PF 0.97, t -1.85, P&L per lot $-5,143, worst -112%; taker 745 trades, win 49%, mean -9.19% of max loss, PF 0.76, t -4.74, P&L per lot $-40,512, worst -161%
