# Book D fill-quality study: results

Spec and pass rule: `docs/superpowers/specs/2026-11-09-d-fill-quality-design.md` (fixed before the run). Script: `research/d_fill_quality.py`. Returns are % of max risk per trade.

## Verdict: **FAIL**

Primary (queue rule, hold 60 s, stops at taker): n 1788, mean -1.72%, t -5.91 (bar 2.33); 2016-2021 -2.33%, 2022-2026 -1.32%; queue doubled -1.72%. Checks: [False, False, False].

**What it means (written by hand after the run; the pass rule was not changed).** Working a limit order does not rescue book D. Resting at the price nearest mid and walking toward natural every 15 s fills each 1c-wide leg at about +0.5c worse than the decision-time mid (the full half-spread), at every hold time tried: the order only fills passively 6-52% of the time, and when it does, the market has usually moved against it, so the "cost vs mid at fill" column is the same +0.4-0.5c. Doubling the queue changes nothing because most passive fills are trade-throughs, not queue fills. D needs a fill no worse than +0.11c a leg to break even, and even a perfect mid fill (with stops paid at taker) is only +0.41%, t 1.38. Only the optimistic rule (any print at the limit fills us, ignoring the queue) with a 5-minute hold comes out positive (+0.21%, t 0.71), and that rule is not achievable. Every year from 2016 to 2026 is negative except 2022 (+0.15%, t 0.10). Recommendation: turn book D off in paper, as the spec says for a FAIL; Evan decides. Limits (spec section 8): legs are simulated one at a time, not as a combo order; Robinhood routes for payment, which may fill at or inside the NBBO more often than the public tape shows; the queue ahead is inferred from displayed size.

## D under each fill model

| Model | n | Mean % | t | PF | Win % |
|---|---|---|---|---|---|
| mid | 1864 | +0.81 | +2.86 | +1.22 | +70.9 |
| patient | 1783 | -1.45 | -5.05 | +0.70 | +43.8 |
| taker | 1783 | -1.98 | -6.99 | +0.61 | +42.4 |
| measured queue H15 | 1787 | -1.73 | -5.97 | +0.65 | +43.0 |
| measured queue H60 | 1788 | -1.72 | -5.91 | +0.66 | +43.5 |
| measured queue H300 | 1789 | -1.53 | -5.29 | +0.69 | +44.0 |
| measured queue2 H15 | 1787 | -1.73 | -5.97 | +0.65 | +43.0 |
| measured queue2 H60 | 1788 | -1.72 | -5.91 | +0.66 | +43.5 |
| measured queue2 H300 | 1789 | -1.54 | -5.36 | +0.68 | +43.9 |
| measured optimistic H15 | 1862 | -0.79 | -2.73 | +0.83 | +57.4 |
| measured optimistic H60 | 1863 | -0.30 | -1.04 | +0.93 | +64.5 |
| measured optimistic H300 | 1864 | +0.21 | +0.71 | +1.05 | +70.9 |
| measured strict H15 | 1783 | -1.79 | -6.19 | +0.64 | +42.8 |
| measured strict H60 | 1783 | -1.79 | -6.19 | +0.64 | +42.8 |
| measured strict H300 | 1787 | -1.76 | -6.11 | +0.65 | +42.9 |

## Uniform cost per leg (stops at taker)

| c (cents vs mid) | n | Mean % | t |
|---|---|---|---|
| -0.50 | 1864 | +2.86 | +9.67 |
| -0.25 | 1864 | +1.78 | +6.00 |
| +0.00 | 1864 | +0.41 | +1.38 |
| +0.25 | 1845 | -0.55 | -1.86 |
| +0.50 | 1789 | -1.27 | -4.34 |
| +0.75 | 1789 | -1.42 | -4.84 |
| +1.00 | 1783 | -1.49 | -5.12 |

Break-even uniform cost: +0.11 c a leg.

## Measured cost per leg (cents worse than the decision-time mid)

| Rule | Hold s | Window | Side | Width | n | Cost c | Passive fill % | Median s to fill | Cost vs mid at fill c |
|---|---|---|---|---|---|---|---|---|---|
| optimistic | 15 | close | buy | 1c | 3517 | +0.26 | 34 | 15 | -0.02 |
| optimistic | 15 | close | buy | 2c | 67 | +0.69 | 84 | 4 | +1.12 |
| optimistic | 15 | close | buy | 3+c | 165 | +7.75 | 85 | 17 | +5.32 |
| optimistic | 15 | close | sell | 1c | 3658 | +0.43 | 14 | 15 | +0.30 |
| optimistic | 15 | close | sell | 2c | 32 | +0.91 | 59 | 15 | +0.50 |
| optimistic | 15 | close | sell | 3+c | 56 | +9.14 | 91 | 19 | +5.15 |
| optimistic | 15 | entry | buy | 1c | 3569 | +0.14 | 51 | 15 | -0.09 |
| optimistic | 15 | entry | buy | 2c | 111 | +0.76 | 68 | 5 | +0.19 |
| optimistic | 15 | entry | buy | 3+c | 65 | +1.68 | 97 | 3 | +0.35 |
| optimistic | 15 | entry | sell | 1c | 3353 | +0.05 | 72 | 5 | -0.14 |
| optimistic | 15 | entry | sell | 2c | 228 | +0.84 | 78 | 3 | +0.30 |
| optimistic | 15 | entry | sell | 3+c | 166 | +2.04 | 92 | 2 | +0.46 |
| optimistic | 60 | close | buy | 1c | 3517 | +0.10 | 50 | 47 | -0.26 |
| optimistic | 60 | close | buy | 2c | 67 | +0.69 | 99 | 4 | +1.11 |
| optimistic | 60 | close | buy | 3+c | 165 | +7.68 | 90 | 17 | +5.14 |
| optimistic | 60 | close | sell | 1c | 3658 | +0.29 | 29 | 60 | +0.13 |
| optimistic | 60 | close | sell | 2c | 32 | +1.09 | 84 | 28 | +0.37 |
| optimistic | 60 | close | sell | 3+c | 56 | +8.68 | 91 | 23 | +4.99 |
| optimistic | 60 | entry | buy | 1c | 3569 | +0.00 | 67 | 15 | -0.28 |
| optimistic | 60 | entry | buy | 2c | 111 | +0.68 | 75 | 5 | +0.22 |
| optimistic | 60 | entry | buy | 3+c | 65 | +1.48 | 95 | 3 | +0.09 |
| optimistic | 60 | entry | sell | 1c | 3353 | -0.04 | 84 | 5 | -0.25 |
| optimistic | 60 | entry | sell | 2c | 228 | +0.71 | 88 | 3 | +0.18 |
| optimistic | 60 | entry | sell | 3+c | 166 | +1.96 | 94 | 2 | +0.49 |
| optimistic | 300 | close | buy | 1c | 3517 | +0.05 | 58 | 47 | -0.36 |
| optimistic | 300 | close | buy | 2c | 67 | +0.48 | 99 | 4 | +1.05 |
| optimistic | 300 | close | buy | 3+c | 165 | +7.83 | 89 | 17 | +4.98 |
| optimistic | 300 | close | sell | 1c | 3658 | +0.00 | 59 | 102 | -0.12 |
| optimistic | 300 | close | sell | 2c | 32 | +0.97 | 84 | 28 | +0.47 |
| optimistic | 300 | close | sell | 3+c | 56 | +8.30 | 91 | 23 | +4.70 |
| optimistic | 300 | entry | buy | 1c | 3569 | -0.09 | 76 | 15 | -0.35 |
| optimistic | 300 | entry | buy | 2c | 111 | +0.32 | 89 | 5 | +0.19 |
| optimistic | 300 | entry | buy | 3+c | 65 | +1.60 | 97 | 3 | +0.22 |
| optimistic | 300 | entry | sell | 1c | 3353 | -0.13 | 93 | 5 | -0.31 |
| optimistic | 300 | entry | sell | 2c | 228 | +0.55 | 97 | 3 | +0.13 |
| optimistic | 300 | entry | sell | 3+c | 166 | +2.36 | 92 | 2 | +0.41 |
| queue | 15 | close | buy | 1c | 3517 | +0.54 | 9 | 15 | +0.52 |
| queue | 15 | close | buy | 2c | 67 | +0.69 | 84 | 4 | +1.12 |
| queue | 15 | close | buy | 3+c | 165 | +7.75 | 85 | 17 | +5.32 |
| queue | 15 | close | sell | 1c | 3658 | +0.55 | 4 | 15 | +0.54 |
| queue | 15 | close | sell | 2c | 32 | +0.91 | 59 | 15 | +0.50 |
| queue | 15 | close | sell | 3+c | 56 | +9.14 | 91 | 19 | +5.15 |
| queue | 15 | entry | buy | 1c | 3569 | +0.51 | 22 | 15 | +0.43 |
| queue | 15 | entry | buy | 2c | 111 | +0.77 | 68 | 5 | +0.22 |
| queue | 15 | entry | buy | 3+c | 65 | +1.68 | 97 | 3 | +0.35 |
| queue | 15 | entry | sell | 1c | 3353 | +0.56 | 41 | 15 | +0.41 |
| queue | 15 | entry | sell | 2c | 228 | +0.86 | 77 | 3 | +0.31 |
| queue | 15 | entry | sell | 3+c | 166 | +2.04 | 92 | 2 | +0.48 |
| queue | 60 | close | buy | 1c | 3517 | +0.50 | 14 | 60 | +0.50 |
| queue | 60 | close | buy | 2c | 67 | +0.75 | 97 | 4 | +1.13 |
| queue | 60 | close | buy | 3+c | 165 | +7.68 | 90 | 17 | +5.14 |
| queue | 60 | close | sell | 1c | 3658 | +0.54 | 6 | 60 | +0.54 |
| queue | 60 | close | sell | 2c | 32 | +1.09 | 84 | 28 | +0.37 |
| queue | 60 | close | sell | 3+c | 56 | +8.68 | 91 | 23 | +4.99 |
| queue | 60 | entry | buy | 1c | 3569 | +0.51 | 31 | 60 | +0.40 |
| queue | 60 | entry | buy | 2c | 111 | +0.68 | 75 | 5 | +0.22 |
| queue | 60 | entry | buy | 3+c | 65 | +1.48 | 95 | 3 | +0.09 |
| queue | 60 | entry | sell | 1c | 3353 | +0.57 | 52 | 60 | +0.37 |
| queue | 60 | entry | sell | 2c | 228 | +0.71 | 87 | 3 | +0.18 |
| queue | 60 | entry | sell | 3+c | 166 | +2.01 | 93 | 2 | +0.51 |
| queue | 300 | close | buy | 1c | 3517 | +0.50 | 22 | 300 | +0.43 |
| queue | 300 | close | buy | 2c | 67 | +0.57 | 97 | 4 | +1.07 |
| queue | 300 | close | buy | 3+c | 165 | +7.83 | 89 | 17 | +4.98 |
| queue | 300 | close | sell | 1c | 3658 | +0.55 | 10 | 300 | +0.56 |
| queue | 300 | close | sell | 2c | 32 | +0.97 | 84 | 28 | +0.47 |
| queue | 300 | close | sell | 3+c | 56 | +8.30 | 91 | 23 | +4.70 |
| queue | 300 | entry | buy | 1c | 3569 | +0.44 | 44 | 300 | +0.36 |
| queue | 300 | entry | buy | 2c | 111 | +0.32 | 89 | 5 | +0.19 |
| queue | 300 | entry | buy | 3+c | 65 | +1.60 | 97 | 3 | +0.22 |
| queue | 300 | entry | sell | 1c | 3353 | +0.55 | 63 | 121 | +0.35 |
| queue | 300 | entry | sell | 2c | 228 | +0.55 | 96 | 3 | +0.14 |
| queue | 300 | entry | sell | 3+c | 166 | +2.37 | 91 | 2 | +0.43 |
| queue2 | 15 | close | buy | 1c | 3517 | +0.54 | 9 | 15 | +0.53 |
| queue2 | 15 | close | buy | 2c | 67 | +0.69 | 84 | 4 | +1.12 |
| queue2 | 15 | close | buy | 3+c | 165 | +7.75 | 85 | 17 | +5.32 |
| queue2 | 15 | close | sell | 1c | 3658 | +0.55 | 4 | 15 | +0.54 |
| queue2 | 15 | close | sell | 2c | 32 | +0.91 | 59 | 15 | +0.50 |
| queue2 | 15 | close | sell | 3+c | 56 | +9.14 | 91 | 19 | +5.15 |
| queue2 | 15 | entry | buy | 1c | 3569 | +0.52 | 22 | 15 | +0.44 |
| queue2 | 15 | entry | buy | 2c | 111 | +0.77 | 68 | 5 | +0.22 |
| queue2 | 15 | entry | buy | 3+c | 65 | +1.68 | 97 | 3 | +0.35 |
| queue2 | 15 | entry | sell | 1c | 3353 | +0.57 | 41 | 15 | +0.42 |
| queue2 | 15 | entry | sell | 2c | 228 | +0.86 | 77 | 3 | +0.31 |
| queue2 | 15 | entry | sell | 3+c | 166 | +2.04 | 92 | 2 | +0.48 |
| queue2 | 60 | close | buy | 1c | 3517 | +0.51 | 14 | 60 | +0.51 |
| queue2 | 60 | close | buy | 2c | 67 | +0.75 | 97 | 4 | +1.20 |
| queue2 | 60 | close | buy | 3+c | 165 | +7.68 | 90 | 17 | +5.14 |
| queue2 | 60 | close | sell | 1c | 3658 | +0.54 | 6 | 60 | +0.54 |
| queue2 | 60 | close | sell | 2c | 32 | +1.09 | 84 | 28 | +0.37 |
| queue2 | 60 | close | sell | 3+c | 56 | +8.68 | 91 | 23 | +4.99 |
| queue2 | 60 | entry | buy | 1c | 3569 | +0.51 | 31 | 60 | +0.42 |
| queue2 | 60 | entry | buy | 2c | 111 | +0.68 | 75 | 5 | +0.22 |
| queue2 | 60 | entry | buy | 3+c | 65 | +1.48 | 95 | 3 | +0.09 |
| queue2 | 60 | entry | sell | 1c | 3353 | +0.57 | 51 | 60 | +0.39 |
| queue2 | 60 | entry | sell | 2c | 228 | +0.71 | 87 | 3 | +0.18 |
| queue2 | 60 | entry | sell | 3+c | 166 | +2.01 | 93 | 2 | +0.51 |
| queue2 | 300 | close | buy | 1c | 3517 | +0.50 | 22 | 300 | +0.45 |
| queue2 | 300 | close | buy | 2c | 67 | +0.57 | 97 | 4 | +1.07 |
| queue2 | 300 | close | buy | 3+c | 165 | +7.83 | 89 | 17 | +4.98 |
| queue2 | 300 | close | sell | 1c | 3658 | +0.55 | 10 | 300 | +0.57 |
| queue2 | 300 | close | sell | 2c | 32 | +0.97 | 84 | 28 | +0.47 |
| queue2 | 300 | close | sell | 3+c | 56 | +8.30 | 91 | 23 | +4.70 |
| queue2 | 300 | entry | buy | 1c | 3569 | +0.45 | 43 | 300 | +0.39 |
| queue2 | 300 | entry | buy | 2c | 111 | +0.32 | 89 | 5 | +0.19 |
| queue2 | 300 | entry | buy | 3+c | 65 | +1.60 | 97 | 3 | +0.22 |
| queue2 | 300 | entry | sell | 1c | 3353 | +0.55 | 62 | 127 | +0.37 |
| queue2 | 300 | entry | sell | 2c | 228 | +0.55 | 96 | 3 | +0.14 |
| queue2 | 300 | entry | sell | 3+c | 166 | +2.37 | 91 | 2 | +0.43 |
| strict | 15 | close | buy | 1c | 3517 | +0.55 | 8 | 15 | +0.55 |
| strict | 15 | close | buy | 2c | 67 | +0.91 | 81 | 5 | +1.54 |
| strict | 15 | close | buy | 3+c | 165 | +8.12 | 85 | 20 | +5.82 |
| strict | 15 | close | sell | 1c | 3658 | +0.55 | 4 | 15 | +0.56 |
| strict | 15 | close | sell | 2c | 32 | +1.38 | 59 | 15 | +0.93 |
| strict | 15 | close | sell | 3+c | 56 | +13.52 | 84 | 24 | +5.88 |
| strict | 15 | entry | buy | 1c | 3569 | +0.54 | 21 | 15 | +0.49 |
| strict | 15 | entry | buy | 2c | 111 | +1.05 | 64 | 11 | +0.68 |
| strict | 15 | entry | buy | 3+c | 65 | +2.12 | 95 | 4 | +0.70 |
| strict | 15 | entry | sell | 1c | 3353 | +0.64 | 38 | 15 | +0.49 |
| strict | 15 | entry | sell | 2c | 228 | +1.23 | 66 | 6 | +0.58 |
| strict | 15 | entry | sell | 3+c | 166 | +2.65 | 87 | 2 | +0.92 |
| strict | 60 | close | buy | 1c | 3517 | +0.52 | 13 | 60 | +0.54 |
| strict | 60 | close | buy | 2c | 67 | +2.51 | 88 | 5 | +1.53 |
| strict | 60 | close | buy | 3+c | 165 | +8.38 | 87 | 21 | +5.73 |
| strict | 60 | close | sell | 1c | 3658 | +0.55 | 6 | 60 | +0.56 |
| strict | 60 | close | sell | 2c | 32 | +1.25 | 78 | 31 | +0.95 |
| strict | 60 | close | sell | 3+c | 56 | +12.86 | 89 | 25 | +5.78 |
| strict | 60 | entry | buy | 1c | 3569 | +0.54 | 29 | 60 | +0.47 |
| strict | 60 | entry | buy | 2c | 111 | +0.95 | 72 | 11 | +0.68 |
| strict | 60 | entry | buy | 3+c | 65 | +2.00 | 95 | 4 | +0.58 |
| strict | 60 | entry | sell | 1c | 3353 | +0.62 | 48 | 60 | +0.46 |
| strict | 60 | entry | sell | 2c | 228 | +1.12 | 76 | 6 | +0.57 |
| strict | 60 | entry | sell | 3+c | 166 | +2.69 | 88 | 2 | +0.91 |
| strict | 300 | close | buy | 1c | 3517 | +0.52 | 21 | 300 | +0.48 |
| strict | 300 | close | buy | 2c | 67 | +2.24 | 91 | 5 | +1.43 |
| strict | 300 | close | buy | 3+c | 165 | +7.97 | 89 | 21 | +5.19 |
| strict | 300 | close | sell | 1c | 3658 | +0.56 | 10 | 300 | +0.59 |
| strict | 300 | close | sell | 2c | 32 | +1.19 | 78 | 31 | +1.05 |
| strict | 300 | close | sell | 3+c | 56 | +12.48 | 89 | 25 | +5.46 |
| strict | 300 | entry | buy | 1c | 3569 | +0.49 | 41 | 300 | +0.44 |
| strict | 300 | entry | buy | 2c | 111 | +0.56 | 86 | 11 | +0.69 |
| strict | 300 | entry | buy | 3+c | 65 | +1.78 | 97 | 4 | +0.50 |
| strict | 300 | entry | sell | 1c | 3353 | +0.60 | 60 | 136 | +0.43 |
| strict | 300 | entry | sell | 2c | 228 | +1.15 | 85 | 6 | +0.55 |
| strict | 300 | entry | sell | 3+c | 166 | +2.93 | 88 | 2 | +0.85 |

## Primary, by year

| Year | n | Mean % | t |
|---|---|---|---|
| 2016 | 61 | -2.91 | -3.54 |
| 2017 | 65 | -1.87 | -8.51 |
| 2018 | 140 | -3.22 | -3.98 |
| 2019 | 140 | -2.60 | -3.52 |
| 2020 | 150 | -2.75 | -2.14 |
| 2021 | 147 | -0.76 | -1.34 |
| 2022 | 163 | +0.15 | +0.10 |
| 2023 | 248 | -0.27 | -0.40 |
| 2024 | 244 | -1.73 | -2.38 |
| 2025 | 247 | -2.27 | -2.31 |
| 2026 | 183 | -2.21 | -3.09 |

Exits (primary): {'stop': 995, 'take 50%': 741, 'time': 52}

Order outcomes (primary): {'close': {'cross': 191, 'deadline': 17, 'no_quote': 21, 'no_trades_natural': 1219, 'queue': 207, 'trade_through': 619, 'walked_to_natural': 5242}, 'entry': {'cross': 842, 'deadline': 8, 'no_quote': 24, 'no_trades_natural': 302, 'queue': 666, 'trade_through': 1839, 'walked_to_natural': 3835}}

Quiet filter: not run: no SPY 1-minute bars in the cloud session (information only, spec section 5)
