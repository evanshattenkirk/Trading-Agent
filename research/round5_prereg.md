# Round 5: W, E1, E2, X3 and K1 (pre-registration)

Status: **FROZEN 2026-10-30.** Evan picked "All four plus Kalshi check" on the thread's decision card (2026-10-30
18:12Z). This file is merged to main before any replay script for these candidates exists and before any of their
outcomes is computed; any later change is an amendment with its own date. Research only: no book, rule or engine
change. Thread "Find a profitable new strategy, round 5"; the candidate screen is in the project note
`research/new-strategy-2026-10-30.md` (shared folder). Everything runs in cloud sessions (Evan, 2026-10-30).

What has been looked at before this file was frozen (so a reader can judge the contamination):
- SPY 0DTE quotes 2016-2026: books B and D, the 09:45 straddle held to settlement (by year), book G, H4, N1/N2.
  W's discovery rule and its fresh confirmation sample (section 2) exist because of this.
- Book E's 30 names: X1's EDGAR report times and the X1 fly (15:45 ET the session before the report to 10:00 ET
  after). Nothing from T−10 to T−1 has been pulled or looked at.
- DoltHub daily stock bars (downloaded 2026-10-30): file sizes, column names and date range only.
- Kalshi: the API's field list only.

## 1. Common rules

- **Fill models (options):** **mid**; **patient** = sell at max(bid, mid − $0.01), buy at min(ask, mid + $0.01)
  (`bd_real_quotes.fill`, the paper broker's rule); **taker** = sell at the bid, buy at the ask. Fees $0.04 per
  contract per leg per side on every leg traded.
- **Family:** five tests (W, E1, E2, X3, K1). Every out-of-sample t bar below is **2.58** (Bonferroni, 5% two-sided
  over five tests). Reports also say whether a result would have cleared the single-test 2.33 of earlier rounds;
  that changes no verdict.
- **Sensitivities** listed under each candidate are reported and never selected from. A fail drops the candidate;
  no sensitivity row is promoted in its place.
- **A pass** makes a candidate eligible for a book spec, which needs Evan's approval (sizing and any HANDOFF
  exception included). It changes nothing in the engine.

## 2. W: 0DTE ATM iron fly with wide wings, held to 15:40 ET

**Why.** The 0DTE SPY ATM straddle sold at 09:45 ET and held to settlement made +6.83% of premium at mid and +5.92%
at taker (2016-2026, ours). Book B, entered at the same time, made +0.18% at mid after its ±$5 wings, 50% take-profit
and 15:30 ET exit. W asks whether a Level 3-legal wrapper that pays the spread once on entry, and at exit only on
legs that still have value, keeps the premium. Robinhood closes at-risk SPY expiring positions from 15:45 ET, so
15:40 ET is the latest exit.

**Data.** ThetaData 1-minute NBBO, all strikes, both rights, the expiry equal to the session date (0DTE), 09:30-16:00
ET: SPY from the existing pull (`spy_0dte`, 2016-01-08 to 2026-10-05); QQQ and IWM from a new pull with the same
script (every session from 2016-01-01 with a same-day expiry). Sessions that close at 13:00 ET are dropped.

**Rule for one cell (entry time E, wing multiple w).**
- At minute E: spot S by put-call parity (`bd_real_quotes.spot`). K = the strike listed for both calls and puts
  nearest S (ties to the lower). M = call mid + put mid at K.
- Sell the K call and the K put. Buy the call at the listed strike nearest K + w·M and the put at the listed strike
  nearest K − w·M (ties to the farther strike), each at least one listed strike away from K.
- **Skip** if any leg has no quote at E, a short leg has a zero bid, or any leg is wider than 25% of its mid and more
  than $0.02 (H4's rule).
- **Exit at 15:40 ET:** buy back both short legs; sell each long wing whose bid is at least $0.01; a long wing with a
  zero bid is left to expire (value 0, no fee). If a needed leg has no quote at 15:40, use the latest minute from 15:39
  back to 15:30 where every needed leg has one; otherwise the session is dropped and counted.
- No take-profit, no stop: the wings cap the loss.
- **Per lot:** P&L = 100 × (credit − cost to close) − fees. Max loss = 100 × (the wider wing's width − credit) + entry
  fees + exit fees on four legs. **Return = P&L / max loss**, each under the fill model being scored.

**Grid (9 cells).** E ∈ {09:45, 11:00, 13:00} ET × w ∈ {1.0, 1.5, 2.0}; exit 15:40 in every cell.

**Discovery (SPY, sessions 2016-01-01 to 2021-12-31).** For each cell, t of the per-trade patient returns. The cell
with the highest t is chosen (ties: smaller w, then earlier E). Nothing else is chosen from discovery.

**Confirmation (the chosen cell only).**
- Pooled sample: SPY, QQQ and IWM sessions from 2022-01-01 to the last day on disk. Daily return = the mean of the
  returns of the underlyings that traded that day; t over days.
- Fresh-only sample: QQQ and IWM, every session on disk (2016 onward where it exists), same daily averaging.

**W passes only if all four hold:**
1. the chosen cell's discovery mean > 0 at patient;
2. pooled confirmation t ≥ **2.58** at patient;
3. pooled confirmation mean > 0 at taker;
4. fresh-only mean > 0 at patient with t ≥ 1.65.

If QQQ and IWM quotes can't be pulled, W is reported on SPY alone (bars 1-3 with SPY 2022 onward as the pooled
sample) and labelled provisional; it is not eligible for a book until bar 4 is met on fresh data.

**Reported, never selected:** all 9 cells on every underlying and period; exit 15:30; value at settlement (legs at
intrinsic against the 16:00 ET parity spot, no exit cost; not tradeable on SPY, it measures the last 20 minutes and
decides whether a cash-settled XSP version is worth a pull); B's ±$5 wings with W's exit (SPY); by year; prior VIX
close < 15 / 15-20 / 20-30 / > 30; daily-expiry era (from 2022-11-14) vs before; worst day; max loss per lot
(median, 90th percentile); share of sessions skipped and why.

## 3. E1 and E2: book E as built, on real quotes

**Why.** Book E is in paper with no measured edge. E1 rests on the pre-earnings IV run-up (Gao, Xing & Zhang, JFQA
2018: ATM straddles bought three days before a report and sold just before it earn positive returns). E2 is E's
calendar. The rules are the book's (`docs/superpowers/specs/2026-09-29-book-e-design.md`, `config.yaml`
`books.E_earnings_iv`); this section only restates them for a replay.

**Universe and dates.** The 30 names in `crew.earnings.universe` (FB before 2022-06-09 for META). Report times from
X1's EDGAR file (`research/data/x1/earnings.csv`, 8-K Item 2.02 acceptance times, rows with status `kept`). For one
event: D = `report_session` and timing = am for `bmo`; D = `entry_session` and timing = pm for `amc`; D =
`entry_session` (the filing day) and timing = unknown for `dmh` (filed during market hours, so the release may have
come before that day's open; this keeps every exit before it). Sessions are NYSE sessions
(`research/nyse_calendar.py`); T−n is the n-th session before D.

**Expiries.** Post-announcement expiry: am: the first listed expiry ≥ D; pm or unknown: the first listed expiry > D.
Pre-announcement expiry (E2): am or unknown: the last listed expiry < D; pm: the last listed expiry ≤ D.

**Clock.** Entries and normal exits at 15:45 ET (14:45 CT); on 13:00-close sessions 12:20 ET (11:20 CT). The exit
day is T−1 for am and unknown timing, T−0 for pm.

**Spot and strike.** Spot by put-call parity on the entry expiry at the entry minute (the five strikes with the
smallest |call mid − put mid|, median of K + C − P). Strike: the listed strike nearest the spot that exists in every
leg's expiry (ties to the lower).

**Filters (both).** Skip if the prior session's VIX close > 30 (CBOE history) or if any leg at entry has
(ask − bid) > 5% of its mid. IV percentile: the post-announcement expiry's ATM implied volatility at entry
(Black-Scholes on the call and put mids, averaged; rate = the prior day's 3-month T-bill, FRED DTB3; no dividends;
time to the expiry's 16:00 ET in calendar days / 365) ranked against the same symbol's values at the same T in
earlier events; skip above the 80th percentile once 4 earlier events exist, no filter before that.

**E1, straddle.** At T−3: buy the ATM call and put in the post-announcement expiry, which must be 4-10 calendar days
after the entry day (else skip). Take profit when the combo mid ≥ 1.20 × the entry debit; stop when it is ≤ 0.70 ×
the entry debit; otherwise exit on the exit day.

**E2, calendar.** On T−10, then T−9, then T−8 (the first day whose entry passes every check): sell the ATM call in the
pre-announcement expiry, buy the same-strike call in the post-announcement expiry; the short leg must expire after
the entry day, and the debit must be > 0. Take profit at combo mid ≥ 1.15 × entry, stop at ≤ 0.70 × entry. Exit at
the earlier of the exit day's 15:45 ET and the short leg's expiry day at 15:15 ET (14:15 CT).

**Path checks.** Take profit and stop are checked on every 1-minute bar after entry up to the exit (09:30-16:00 ET on
held sessions), only when every leg has a fresh, uncrossed quote; on a trigger the position closes at that minute
under the fill model. The engine polls every 15 s, so 1-minute bars are coarser. A leg with no quote at the exit
minute uses the latest earlier minute that session; none at all drops the event (counted).

**Size and return.** One lot. Return = P&L / entry debit under the fill model. Every eligible event is traded; book
E's portfolio limits (3 open, one per sector, one per symbol, $500 / $250 max debit) are applied only in a reported
row.

**Data.** ThetaData 1-minute NBBO for each event: all strikes at the entry minutes (T−3 for E1, T−10..T−8 for E2) of
the needed expiries; then the chosen contracts from entry to exit. Same subscription and checks as X1.

**Periods.** In-sample: events with D from 2018-01-01 to 2021-12-31. Out-of-sample: D from 2022-01-01.

**Each of E1 and E2 passes only if all three hold:** in-sample mean > 0 at patient; out-of-sample t ≥ **2.58** at
patient, clustered by entry date (events entered the same day are averaged first); out-of-sample mean > 0 at taker.

**Reported, never selected:** the as-built portfolio row; no take-profit or stop (hold to the exit); exit at 15:00
ET; by year, by sector, by timing; the share of take-profits, stops and time exits; the IV-percentile filter off.

## 4. X3: high-volume up-day reversal on stocks the S&P study never saw (book F2-P's premise)

**Why.** F2-P (in paper) buys a put spread on a name up ≥ 3% on heavy volume, betting on next-day weakness. That
rests on one cell of our daily study on S&P names (RVOL ≥ 2, up ≥ 3%, next day −19.5 bp, t −3.89). X3 asks whether
it holds on names that study never saw. The rule is the 2026-09-29 draft in `research/strategies_equity_prereg.md`,
frozen here with the data source filled in.

**Data.** DoltHub `post-no-preference/stocks` (free): `ohlcv` (daily, unadjusted, 2011 onward), `split`, `dividend`,
`symbol` (Nasdaq Trader fields, incl. `is_etf`, `is_test_issue`, `last_seen`; it keeps symbols no longer listed).
Survivorship is whatever the source keeps; the report counts names with `last_seen` before 2026.

**Universe (each day t, known at t's close).** `is_etf` = 0, `is_test_issue` = 0, security name not a warrant,
right, unit, preferred, note or depositary share; **minus** today's S&P 500 list (datasets/s-and-p-500-companies),
the 505 names in `research/strategy_f_daily_results_2013_2018.json`, F2's 34 names and book E's 30. Then prior close
≥ $10, 14-day average volume ≥ 1,000,000, ATR14 ≥ $0.50, 20-day average dollar volume ≥ $25M (all through t−1).
Any split with ex-date in [t−20, t+1] drops the name for that t.

**Signal and trade.** close_t ≥ 1.03 × close_(t−1) and volume_t ≥ 2 × the mean volume of t−20..t−1. Short at close_t,
cover at close_(t+1); a dividend with ex-date t+1 is paid by the short. Excess return = short return minus the short
return of the equal-weight universe over the same night (all universe names that day). Costs 2 bp per side.

**Statistic.** Day-clustered: each signal day's mean excess short return; t over signal days. Period 2016-01-01 to
2026-10-29; halves 2016-2020 and 2021-2026.

**X3 passes only if all four hold:** profit factor ≥ 1.1 over trades; mean > 0 in both halves; day-clustered t ≥
**2.58**; mean > 0 at 10 bp per side.

**Reported, never selected:** the RVOL < 1.5 control (up ≥ 3%, normal volume); F2-P's own 1.8× volume threshold;
raw (not excess) returns; by year; names no longer listed vs listed.

## 5. K1: Kalshi favourite-longshot bias, out of sample (information only)

**Why.** Bürgi, Deng & Whelan ("Makers and Takers: The Economics of the Kalshi Prediction Market", 2025; Kalshi data
from 2021 to April 2025) find that contracts above 50c earn a small positive return after fees (significant above
70c) while contracts under 10c lose over 60%. K1 checks the favourite side on markets that closed after their sample.
The agent can't trade event contracts through Robinhood's MCP, so a pass is information for Evan, not a book.

**Data.** Kalshi's public API (no account): series (`/series`), events with nested markets (`/events`, and
`/historical/markets` for archived ones), daily candlesticks (`/series/{s}/markets/{t}/candlesticks` and
`/historical/markets/{t}/candlesticks`, `period_interval` 1440), the historical cutoff (`/historical/cutoff`).

**Markets.** Binary, non-multivariate markets that settled `yes` or `no` (voided and scalar markets dropped), with a
scheduled `close_time` from 2021-07-01 to 2026-10-29, open at least 3 days. If more than 60,000 qualify, keep the
events whose SHA-1 of `event_ticker` mod k = 0, with the smallest k that keeps at most 60,000 markets (no outcome
used).

**Entry.** The last daily candle that ends at least 24 hours before `close_time`. Skip if the market settled before
that candle's end, if its yes bid/ask spread at that candle's close is wider than $0.05, or if fewer than 100
contracts traded in the 7 daily candles up to it. Buy YES if the yes ask is in [0.70, 0.97]; buy NO if the no ask
(1 − yes bid) is in [0.70, 0.97]; otherwise no trade. Hold to settlement.

**Fees and return.** Taker fee per contract = `fee_multiplier` × 0.07 × P × (1 − P) at the entry price P, from the
series' current `fee_type` (`quadratic` or `quadratic_with_maker_fees`; series with any other fee type are dropped and
counted). Fee changes over time aren't modelled; the report says so. Return per contract = (payout − P − fee) / (P + fee), payout 1 or 0.

**Periods.** In-sample: close_time before 2025-05-01 (the paper's window). Out-of-sample: 2025-05-01 to 2026-10-29.

**K1 passes only if all three hold:** in-sample mean > 0; out-of-sample t ≥ **2.58**, clustered by event (markets in
one event are averaged first); out-of-sample mean > 0 with the fee rounded up to a whole cent per contract.

**Reported, never selected:** price buckets 0.50-0.70, 0.70-0.85, 0.85-0.97 and the longshot side (< 0.30) as the
mirror; by category (from the series); maker version (buy at the bid, no fee; fills not knowable, so information
only); entry 72 hours before close.

## 6. Order of work

1. This file merged to main.
2. Scripts written test-first (`research/round5_w.py`, `round5_e.py`, `round5_x3.py`, `round5_k1.py`, and the
   ThetaData fetchers); tests use synthetic data only.
3. Pulls: QQQ and IWM 0DTE and book E's event quotes from a cloud session holding the ThetaData key; Kalshi and
   DoltHub from any cloud session.
4. Runs and one results report, `research/round5.md`, with every verdict, pass or fail.

## Amendments

**Amendment 1 (2026-10-30, before any book E quote was pulled or any E1/E2 outcome computed).** Section 3 says
"D = `entry_session` (the filing day) and timing = unknown for `dmh`". In X1's file the filing day of a `dmh` row
is `report_session`; its `entry_session` is the session before the filing. The parenthesis and the stated reason
("this keeps every exit before it") describe the filing day, so for `dmh` rows D = `report_session`. The exit for
those events is the session before the filing day. Implemented in `research/round5_e.py`.

**Amendment 2 (2026-10-30, before any XSP quote was pulled).** Evan asked for a sixth test after W's settlement
row: WX, W's chosen cell on XSP held to cash settlement. Its rules and bar (t 2.64, Bonferroni over six) are frozen in
`research/round5_wx_prereg.md`. The five tests above keep their rules and their 2.58 bar.
