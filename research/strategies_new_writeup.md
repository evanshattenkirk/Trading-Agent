# New strategy candidates: ranked write-up (2026-09-28)

**Bottom line.** Of seven pre-registered candidates, one passes the frozen decision rule: **F3, a 10:00 ET SPY
0DTE/1DTE ATM call calendar**. It is the only one recommended as a new paper book. It is not a new source of
edge: its daily P&L correlates 0.84 with book D, because it sells the same intraday variance premium. What it
changes is the wrapper: 2 legs instead of 4, a worst case capped at the debit, lower drawdown, and positive
results at natural fills in every period where D is flat or negative. Everything here is Black-Scholes modeled from
VIX, like the B/C/D tables in HANDOFF section 7, so F3 needs a real-quote replay before paper promotion.

Files:
- `research/strategies_new_prereg.md`: rules and decision rule, committed before any result (plus addendum 1).
- `research/strategies_new.py`: the modeled backtest. `python research/strategies_new.py` writes
  `strategies_new_report.md` (full tables, by period and by year) and `strategies_new_results.json`.
- `research/strategies_new_quotes.py`: replay of F1, F2, F2-trend and F3 on the ThetaData quotes (see the end).
- Tests: `research/tests/test_strategies_new.py`, `research/tests/test_strategies_new_quotes.py`.

## Method in one paragraph

Same data and option model as `research/strategies_bcd.py`: S&P 500 1-minute CFD 2005-14 (in-sample) and 2015-20
(out-of-sample) as 5-minute bars, SPY 5-minute 2025-04..2026-03, sessions re-based to SPY 765, Black-Scholes with
IV from the prior VIX close (regular session at 0.80x the VIX-implied sd; overnight at 0.60x, so a whole next-day
option is priced near the VIX-implied day). The new code reproduces `condor_like` for book D trade for trade
(tested). Fills per leg per side: **mid - 1c** (the approved paper rule) and **natural** (2c from mid for
same-day legs, 3c for legs with a session or more left), plus $0.04 fees. Every filter uses only data known at
entry. Stress: premium scaled by s = 0.75 ("little variance premium"). Break-even s is the premium scale at which
the average trade is zero on the fixed set of trades. Capacity: `floor($300 / that trade's max loss)` lots,
HANDOFF section 11's $10k row. Bar for significance: |t| > 2.86 (the HANDOFF's multiple-test bar).

## Ranking

| Rank | Candidate | Rule | 2005-14 mid-1c | 2015-20 natural | 2025-26 natural | Break-even s | Corr with D |
|---|---|---|---|---|---|---|---|
| 1 | **F3** 0DTE/1DTE call calendar | **PASS** | +7.7%, PF 1.76, t +10.6 | +3.6%, PF 1.30, t +2.8 | +10.8%, PF 2.56, t +6.0 | 0.80 / 0.85 / 0.71 | 0.84 |
| 2 | G1 turn-of-month call spread | fail (t) | +8.2%, t +0.8 (n=113) | +33.5%, t +2.4 (n=55) | +16.2%, t +0.5 (n=11) | n/a (long premium) | 0.08 |
| 3 | F1 afternoon iron condor | fail | +2.9%, PF 1.42, t +6.7 | -2.0%, PF 0.75 | +4.5%, PF 2.09 | 0.90 / 0.93 / 0.75 | 0.23 |
| 4 | G2 VIX-stretch call spread | fail | +13.2%, t +0.9 (n=48) | -8.4%, t -0.5 (n=33) | +49.7%, t +1.3 (n=7) | n/a (long premium) | 0.09 |
| 5 | F4 overnight put spread | fail | +0.3%, t +0.6 | -5.4%, PF 0.52 | -1.5%, PF 0.78 | 0.96 / 1.23 / 0.78 | 0.02 |
| 6 | F2 10:00 put spread | fail | -0.9%, t -3.5 | -3.4%, PF 0.51 | -1.1%, PF 0.80 | 1.08 / 1.15 / 0.90 | 0.29 |
| 7 | F2-trend (50-day gate) | fail | -0.7%, t -2.2 | -3.3%, PF 0.49 | -2.0%, PF 0.66 | 1.07 / 1.16 / 0.95 | 0.34 |
| ref | D unfiltered (existing book) | n/a | +4.7%, PF 1.57, t +9.2 | -1.0%, PF 0.90 | +5.7%, PF 1.96 | 0.88 / 0.93 / 0.79 | 1.00 |

Returns are per trade, % of max risk (credit structures) or % of the debit (F3, G1, G2). A lower break-even s is
better for a premium seller: it is how cheap real premium can be, relative to the model, before the book stops
making money.

## 1. F3: 0DTE/1DTE ATM call calendar (recommended as paper book F)

- **Thesis.** Sell today's ATM call, where all of the day's remaining time value decays by the close, and buy
  tomorrow's call at the same strike, which keeps its overnight and next-day value. It harvests the same
  intraday premium as D, but the long call hedges the tail, so the most it can lose is the debit.
- **Rules (frozen).** Monday-Thursday, only when the next session is the next calendar day. At 10:00 ET sell the
  0DTE call and buy the next-day call at `round(SPY)`, one 2-leg debit order. Take profit at +25% of the debit,
  stop at -35%, otherwise close both legs at 15:25 ET (14:25 CT, inside the 15:30 ET short-leg rule).
- **Results.**

  | Period | Fills | n | Avg | Win | PF | t | Avg $/lot | Worst $/lot | Max DD at cap | $/yr at cap |
  |---|---|---|---|---|---|---|---|---|---|---|
  | 2005-14 | mid-1c | 1,535 | +7.7% | 69% | 1.76 | +10.6 | +$15.5 | -$270 | -$810 | +$2,362 |
  | 2005-14 | natural | 1,535 | +5.1% | 66% | 1.45 | +6.9 | +$11.5 | -$276 | | +$1,484 |
  | 2015-20 | mid-1c | 547 | +6.2% | 68% | 1.57 | +5.0 | +$9.7 | -$307 | -$546 | +$1,512 |
  | 2015-20 | natural | 547 | +3.6% | 65% | 1.30 | +2.8 | +$5.7 | -$313 | | +$913 |
  | 2025-26 | mid-1c | 181 | +12.0% | 76% | 2.86 | +6.9 | +$19.1 | -$124 | -$357 | +$4,705 |
  | 2025-26 | natural | 181 | +10.8% | 76% | 2.56 | +6.0 | +$17.3 | -$130 | | +$4,182 |

  At s = 0.75 it loses in 2005-14 (-2.3%) and 2015-20 (-5.5%) and stays positive in 2025-26 (+3.0%).
  By year at mid-1c it is positive in 17 of 18 years (2017: -5.1%, t -0.5).
- **IV check.** The model holds IV at the prior VIX close all day. Marking the hold-to-close version at that day's
  own VIX close instead moves the average from +10.6% to +11.1% (2005-14), +8.6% to +10.9% (2015-20) and +19.5%
  to +19.8% (2025-26). The calendar is long vega, so ignoring IV changes slightly understates it.
- **Capacity in $10k.** Median debit is about $1.70, so the median trade fits 1 lot under the $300 cap; 88% /
  92% / 99% of sessions fit at least 1 lot (skip the day when the debit is above $3.00). One lot uses at most
  $300 of the $1,500 account open-risk cap. Expect roughly +$1k to +$4k a year at 1-2 lots if the model holds.
- **How it differs from A to E.** A is directional long calls. B and D are short 4-leg flies/condors whose losses
  run to the wing. C is a directional bull put. E2 is a multi-day single-stock calendar around earnings. F3 is an
  intraday SPY calendar: short same-day gamma, hedged by next-day gamma, closed the same afternoon.
- **What would kill it.**
  1. The real-quote replay (below) shows no positive average at mid - 1c. The model's key assumption is how the
     market prices the 0DTE call relative to the next-day call; if 0DTE premium is priced cheaper than 0.80-0.85x
     of this model, F3 loses.
  2. Paper: profit factor under 1.1 at taker fills after 100 trades, or results outside the backtest range
     (HANDOFF section 10 step 8).
  3. It adds little if D is also promoted: with a 0.84 correlation they are one bet. Treat F3 and D as
     alternatives for the same premium, and compare them side by side in paper.
- **Data caveat.** SPY has had an expiry every weekday only since November 2022, when Tuesday and Thursday
  expiries were added; earlier there were fewer (Monday/Wednesday/Friday for some years, Fridays only before
  that). The 2005-20 rows assume a daily expiry that did not exist then; the 2025-26 rows match today's listings.
  The real-quote replay records each trade's actual days to the next expiry, so the two cases can be split.

## 2. G1: turn-of-the-month $5 call debit spread (not recommended; closest miss)

- **Thesis.** Equity returns cluster from the last trading day of a month to the third day of the next
  (Lakonishok & Smidt 1988; McConnell & Xu 2008).
- **Rules.** Buy a $5 call debit spread (long `round(S)`, short +$5) at the 15:55 ET close of the month's
  second-to-last session, expiring on the third session of the next month; close it at 15:25 ET that day.
- **Results.** The underlying itself earned +20 / +42 / +44 bp per window after 2 bp (t +0.95 / +1.23 / +1.49),
  positive in all three periods but never significant. The spread returned +8.2% / +33.5% (natural) / +16.2%
  (natural) of the debit with t +0.8 / +2.4 / +0.5. It trades about 12 times a year, so a 1-lot book makes
  roughly +$140 to +$840 a year, with drawdowns of $440 to $1,530.
- **Why not.** It fails the in-sample t bar by a wide margin and has too few trades to confirm in paper within a
  year. It is the most distinct idea here (correlation 0.08 with D). If Evan wants a low-effort experiment, it can
  be re-run once more data accrues, but it should not become a book on this evidence.
- **Kill.** Any further period with a negative underlying average over the window.

## 3. F1: afternoon iron condor, 13:30 to 15:25 ET (not recommended)

- **Thesis.** 0DTE decay is fastest late in the day, and entering at 13:30 avoids D's morning risk.
- **Results.** Positive at mid - 1c in every period (+2.9% / +2.0% / +7.7%), but negative at natural fills in
  2005-14 (-1.3%) and 2015-20 (-2.0%). The credits are small (the remaining expected move is only about $3.5), so
  4 legs of friction eat most of the edge. Break-even s is 0.90 / 0.93 / 0.75: it needs the market to price
  nearly all of the modeled premium. Pricing with a U-shaped intraday variance profile (known at entry) makes it
  worse in 2015-20 (+0.6% at mid - 1c).
- **Kill.** Already killed by the natural-fill rule.

## 4. G2: VIX-stretch rebound $5 call debit spread (not recommended)

- Buy after the prior VIX close is 20% above its 10-day average, hold 5 sessions. Only 88 trades in 16 years;
  2015-20 lost (-8.4% natural, underlying -6 bp). Too rare and too inconsistent.

## 5. F4: overnight put credit spread (not recommended)

- **Thesis.** Overnight drift plus the overnight option premium (Cliff, Cooper & Gulen 2008; Muravyev & Ni 2020).
- **Results.** Roughly zero at mid - 1c in 2005-14 (+0.3%, t +0.6), negative in 2015-20 (-2.1%), and clearly
  negative at natural fills everywhere (-3.6% / -5.4% / -1.5%). It would also have needed an exception to the
  "no short legs into 15:30 ET" rule. Its one merit, near-zero correlation with D (0.02), doesn't matter when
  the average is negative.

## 6-7. F2 and F2-trend: 10:00 ET put credit spread (not recommended)

- D's put side on its own loses in the model in 2005-14 and 2015-20 (-0.9% and -1.3% at mid - 1c, t -3.5 and
  -3.2), with or without the 50-day trend gate. In this model, D's profit comes mostly from the call side and
  from pairing the two sides.

## Findings that matter for the existing books (information only; no rule changes)

- **D at natural fills is roughly flat outside 2025-26:** +0.4% (2005-14) and -1.0% (2015-20) per trade, and its
  break-even premium scale is 0.88 / 0.93 / 0.79. The real-quote replay (PR #3) is the deciding test; this just
  says D has little room for quotes that are worse than the model.
- **Holding to 15:25 without the take-profit and stop did better in the model for D (+7.4% vs +4.7%) and F3.**
  That was seen after the fact, so it is not a recommendation; it would have to be a separately pre-registered
  variant.

## Recommendation

1. **Add F3 as paper book F** (1 lot, skip days when the debit is above $3.00, Monday-Thursday, 10:00 to 15:25
   ET). It is paper-only and defined-risk, fits Level 3 (E2 already uses calendars), and never holds a short leg
   past 15:25 ET. Nothing else is recommended; G1 is the only other idea worth revisiting later.
2. **Before paper promotion, replay it on real quotes.** That needs one extra ThetaData pull: for each 0DTE
   session, the quotes of the next expiration after it (calls near the money are enough), saved as
   `data/thetadata/spy_next/YYYY-MM-DD.parquet` named by the trade date. Then, on the Mac:

       .venv/bin/python research/strategies_new_quotes.py --quotes data/thetadata/spy_0dte \
           --next-quotes data/thetadata/spy_next --spy data/spy_1m --out data/thetadata/new_results

   The same command also replays F1, F2 and F2-trend on the existing 0DTE pull (without `--next-quotes` it runs
   just those three).
3. **If Evan approves F3 as a book,** the build (a later thread, TDD) needs: a `books/F` strategy module, the
   recorder extended to the next expiry's near-the-money calls, and the paper broker's 2-leg debit fills. Nothing
   in this PR touches the engine, the existing books or `config.yaml`.
