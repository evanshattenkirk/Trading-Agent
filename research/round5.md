# Round 5 results: W, WX, E1, E2, X3 and K1

Rules frozen before any data in `round5_prereg.md` (PR #61, amendments 1 and 2) and `round5_wx_prereg.md` (PR #62).
Research only: no book, rule or engine change. Everything ran in cloud sessions on 2026-10-30/31. A pass would only
have made a candidate eligible for a book spec, which needs Evan's approval.

| Test | What | Bar | Result | Verdict |
|---|---|---|---|---|
| W | 0DTE ATM iron fly, wings 1-2 straddles, closed 15:40 ET, SPY/QQQ/IWM | pooled t ≥ 2.58 | pooled patient −0.77% of max loss, t −0.74 | **FAIL** |
| WX | W's cell on XSP, held to cash settlement, $0.54 a leg | t ≥ 2.64 | +2.91% of max loss, t 1.34; first half negative | **FAIL** |
| E1 | book E straddle, T−3 to T−1, real quotes | OOS t ≥ 2.58 | OOS patient +0.64% of debit, t 0.64; taker −1.36% | **FAIL** |
| E2 | book E calendar, T−10..T−8 to T−1, real quotes | OOS t ≥ 2.58 | in-sample −4.62%; OOS +0.26%, t 0.15; taker −24.5% | **FAIL** |
| X3 | short the high-volume up day on non-S&P stocks (F2-P's premise) | t ≥ 2.58, PF ≥ 1.1 | +19.5 bp a signal day, t 2.30, PF 1.03 | **FAIL** |
| K1 | Kalshi favourites 70-97c, a day before the close (information only) | OOS t ≥ 2.58 | OOS −0.68% a contract, t −0.77 | **FAIL** |

Returns are per trade. "Patient" sells at max(bid, mid − $0.01) and buys at min(ask, mid + $0.01), the paper
broker's rule; "taker" crosses the spread. Result files: `round5_w_results.json`, `round5_wx_results.json`,
`round5_x3_results.json`, `round5_k1_results.json`, `round5_e_results.json`.

## W: 0DTE iron fly with wide wings, closed at 15:40 ET (FAIL)

Real ThetaData 1-minute NBBO: SPY 1,908 sessions, QQQ 1,523, IWM 1,343 (2016-01-08 to 2026-10-29). Discovery on SPY
2016-2021 chose 13:00 ET with wings 2.0 straddles out (the best patient t of nine cells, and still negative).

| Bar | Needed | Got | |
|---|---|---|---|
| 1. discovery mean at patient (SPY 2016-2021) | > 0 | −0.34% (t −0.30, 777 trades) | no |
| 2. pooled SPY+QQQ+IWM from 2022, t at patient | ≥ 2.58 | −0.77%, t −0.74 (1,121 days) | no |
| 3. pooled mean at taker | > 0 | −2.16% | no |
| 4. fresh QQQ+IWM, every session, patient | > 0, t ≥ 1.65 | −0.36%, t −0.41 (1,479 days) | no |

The same trades at mid make money: pooled +2.13% (t 2.03), QQQ +4.16% (t 4.16), IWM +3.51% (t 3.14). The spread
paid on four legs twice turns that into a loss. Every one of the nine cells loses at patient on SPY 2016-2021
(−0.3% to −11.7%). Closing at 15:30 is worse (pooled from 2022, −1.90%, t −1.94). Max loss a lot: median $170 SPY, $195 QQQ, $116 IWM.

**The premium is real at settlement value.** Valued at intrinsic against the 15:59 ET spot (no closing trade, not
tradeable on these ETFs), the same cell makes +6.70% on SPY (t 5.49), and on the fresh paths QQQ +8.73% (t 6.36),
IWM +6.60% (t 4.36), QQQ and IWM pooled by day +7.31% (t 6.01). The last 20 minutes and the closing spread take
it. That is what WX tested.

## WX: W's fly on XSP, held to cash settlement (FAIL)

XSP is cash-settled on the SPX close, and Robinhood's XSP sellout time is 16:00 ET (read-only check, 2026-10-30), so
it can be held to settlement. XSP quotes: 1,809 sessions with a same-day expiry, 2016-01-08 to 2026-10-29 (one day,
2023-04-06, is a bad ThetaData record). Settlement value: FRED SP500 close / 10 on every traded session (median gap
to the 15:59 parity spot $0.20). Fees: $0.50 Robinhood index fee + $0.04 a leg at the open.

| Bar | Needed | Got | |
|---|---|---|---|
| 1. XSP at patient | mean > 0, t ≥ 2.64 | +2.91%, t 1.34 (620 trades, 2018-05 to 2026-10) | no |
| 2. XSP at taker | > 0 | +1.31% | yes |
| 3. both halves at patient (split 2025-03-05) | > 0 each | −1.49% / +7.30% | no |
| 4. max loss ≤ $300 at entry | > 0 | +2.66% (522 trades) | yes |
| 5. fresh QQQ+IWM held to settlement value | > 0, t ≥ 1.65 | +7.31%, t 6.01 | yes |

Why it falls short: XSP's legs at 13:00 are $0.035 wide (median) against SPY's $0.01, so 1,108 of 1,809 sessions
fail W's wide-leg rule (any leg wider than 25% of its mid and $0.02) and are skipped, and the trades that remain pay
more. On the 596 days both traded, XSP returns 5.36 points less than SPY's settlement row (t −5.11, correlation
0.89). Same path, higher cost.

Reported only, never selected: XSP at mid +5.01% (t 2.27); $0.75 a leg +2.43%; closed at 15:40 with the index fee
−2.40% (taker −17.7%). By year: 2022 −10.6% (65 trades), 2023 +0.8%, 2024 +5.8%, 2025 +7.6% (t 1.89), 2026 +4.8%.
From 2022-11-14 (daily expiries) +4.90%, t 2.12 (535 trades); before it −9.65% (85). The other eight cells all lose
on XSP at patient. Max loss a lot: median $187, 90th percentile $344, 16% of trades above $300. Worst day −$606
(2022-05-04). XSP liquidity has improved since 2022, and the recent years are positive, but the pre-registered
sample doesn't clear the bar, and a filter by period chosen after seeing it would be a new test on new data.

## E1 and E2: book E on real quotes

## E1 and E2: book E on real quotes (both FAIL)

ThetaData quotes for 1,070 of book E's 1,082 events (its 30 names). Four events came back empty and eight are
corrupt records at ThetaData. Report times are from X1's EDGAR file, the rules are book E's as built, and the
clock is 15:45 ET. Returns are on the entry debit, and t is clustered by entry date.

| Bar | E1 straddle (T−3 to T−1) | E2 calendar (T−10..T−8 to T−1) |
|---|---|---|
| 1. in-sample (2018-2021) mean at patient > 0 | +3.19%, t 2.68 (292 trades): yes | −4.62%, t −1.92 (304): no |
| 2. out-of-sample (2022-2026) t at patient ≥ 2.58 | +0.64%, t 0.64 (291): no | +0.26%, t 0.15 (315): no |
| 3. out-of-sample mean at taker > 0 | −1.36%: no | −24.5%: no |

**E1.** The pre-earnings IV run-up shows up in 2018-2019 (+6.3% and +3.4% a trade, t 2.2 each), then fades:
2023 +1.1%, 2024 +1.2%, 2025 −4.3% (t −2.72). Out of sample at mid the trade makes +1.08% (t 1.08), so even free
fills wouldn't pass. Exits: 468 at the time exit, 131 take-profits, 3 stops. Skips: 205 IV percentile, 157 wide
leg, 66 VIX, 38 expiry out of 4-10 days. Reported only: the IV filter off gives +0.61% out of sample, and holding
with no take-profit or stop +0.88%. With book E's limits (3 open, one per sector and name, $500 debit), 133 trades
since 2018 make +2.05% (t 1.26), +$903 in total.

**E2.** The calendar wins 70% of the time at patient fills, but its stops are bigger than its take-profits (431
take-profits, 200 stops). It loses in sample and is flat out of sample. Two single-name legs crossed twice cost
about a quarter of the debit at taker. With book E's limits, 262 trades since 2018 make −3.24% (t −1.41),
−$1,304. By year it swings from +6.9% (2023, t 2.64) to −10.0% (2025, t −2.40), with no steady sign.

Book E stays a paper experiment, as Evan set it up. Neither leg has an edge on real quotes, and E2 loses in both
periods. Any change to the book is Evan's call.

## X3: shorting the high-volume up day on stocks outside the S&P study (FAIL)

DoltHub daily bars, 9,592 symbols in the panel, 2,931 names entered the universe (858 no longer listed), about 368
a day. 14,435 trades on 2,494 signal days, 2016-2026.

| Bar | Needed | Got | |
|---|---|---|---|
| profit factor over trades | ≥ 1.1 | 1.03 | no |
| mean > 0 in both halves | yes | +22.4 bp (2016-2020) / +17.2 bp (2021-2026) a day | yes |
| day-clustered t | ≥ 2.58 | 2.30 | no |
| mean > 0 at 10 bp per side | yes | +3.5 bp a day | yes |

The per-trade mean is +6.0 bp against a day mean of +19.5 bp: a few heavy days carry it. The tails are short
squeezes (APT 2020-02-26 and INDO 2022-03-03 doubled overnight) and moves that look like unrecorded splits (IZM
2024-04-16, −88% overnight, which favours the short). Names later delisted make +31 bp a day (t 1.91), names still
listed +5 bp (t 0.57). Borrow costs are not modelled and would cut further. The normal-volume control earns +1.9 bp
(t 0.53); F2-P's own 1.8× threshold +14.2 bp (t 2.09). F2-P's premise doesn't carry over to names our daily study
never saw with enough strength to pass.

## K1: Kalshi favourite side a day before the close (FAIL, information only)

Kalshi's public API, 19.4 million markets listed, 874,183 eligible, a hash sample of 56,069 markets in 8,085 events.
Buy the side priced 70-97c at the ask 24 hours before the close, pay the quadratic taker fee, hold to settlement.

| | trades / events | mean | mean, fee rounded up | t (by event) |
|---|---|---|---|---|
| in-sample (to 2025-04) | 283 / 126 | +2.23% | +1.61% | 2.10 |
| out-of-sample (2025-05 to 2026-10) | 4,504 / 1,693 | −0.68% | −1.33% | −0.77 |

The longshot side still loses heavily (out-of-sample −56.5% a contract, t −13.0), so the longshot half of the
favourite-longshot bias holds; buying favourites at the ask doesn't earn after fees. Reported only: buying at the
bid as a maker (no fee) shows +2.80% out-of-sample (t 3.14), but whether those orders would fill can't be known from
candles. Entry 72 hours before the close −1.22%. No category has |t| above 1.5.

## What this round says

Six pre-registered tests, six fails. Across five rounds, 17 pre-registered tests have now failed on real data.

Every round has found the same thing. Each premium is measurable at mid or at settlement value, and realistic
fills remove it:

- **The 0DTE variance premium.** It held on fresh underlyings. QQQ and IWM, which nothing before had touched, earn
  +7.3% of max loss a trade (t 6.0) from 13:00 ET to the close at settlement value. That is the strongest evidence
  so far that the premium is real. It isn't reachable on Robinhood, though:
  - SPY, QQQ and IWM have to be closed by 15:40, and the closing spread takes the premium.
  - XSP can be held to settlement, but its spreads are about 3.5 times wider.
- **The pre-earnings IV run-up (E1).** It was there in 2018-2019 and has faded since.
- **Kalshi.** The favourite side doesn't earn at the ask after fees. The longshot side still loses more than half
  its price.
- **F2-P's premise.** The high-volume up-day reversal is weaker on unseen stocks than on the S&P study's names,
  and it rests on a few squeezes.

Reported only, and not a pass: XSP since daily expiries (2022-11-14 onward) is +4.9% a trade (t 2.12, 535 trades),
and every year from 2023 is positive. XSP liquidity is improving. A sample period chosen after seeing the results
can't be the test. The clean way to use this lead would be a new pre-registration on data no one has seen: a
forward-only paper log of WX from today, at one lot. That needs Evan's go-ahead and an exception to HANDOFF
section 12 for holding a short leg to the close.
