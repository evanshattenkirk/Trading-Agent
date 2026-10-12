# N1 and N2: 1DTE SPY credit structures sold at the close, held to 15:40 ET on expiry day (pre-registration)

Status: **FROZEN 2026-10-12** (Evan picked N1 + N2 on the thread's decision card). Merged to main before the replay
script exists or any data is read; any later change is an amendment with its own date. Research only: no book, rule
or engine change. Thread "deep research for a new strategy, maybe 1DTE"; candidates and evidence in the
project note `research/new-strategy-2026-10-12.md` (shared folder).

## 1. Why these two

Every real-quote replay here that held a short SPY premium position to (or near) expiry kept a positive mean at real
fills, and every one that closed it early gave the edge back to the spread (D condor held to settlement +0.69% of
risk at taker, the same condor closed at 15:25 ET −2.20%; H4 bought back at 09:45 ET −$10.71 a lot at taker). N1
and N2 test whether selling the **next** expiry at the close, and holding it until 20 minutes before Robinhood's
15:45 ET at-risk closeout on expiry day, collects the overnight premium (Jones & Shemesh 2018; Muravyev & Ni 2020)
and the expiry day's 0DTE premium on one set of entry costs. N2 is the two-leg version: half the legs, put skew only.

## 2. Data

Already on the Mac, no new pull:
- Entry: `data/thetadata/spy_next/YYYY-MM-DD.parquet` (ThetaData 1-minute NBBO, 09:30-16:00 ET, of SPY's first listed
  expiry after trade date d).
- Exit: `data/thetadata/spy_0dte/YYYY-MM-DD.parquet` for the expiry date e.

## 3. Nights traded (both candidates)

Exactly H4's rule (`book_h_candidates_prereg.md` section 4), plus one addition:
- d is not a 13:00 close; the first expiry after d is the next trading session e; d is not the session before an SPY
  ex-dividend date (third Friday of March, June, September, December).
- **Added:** e is not a 13:00 close (the 15:40 exit does not exist on a half day).
- Positions never overlap: each one closes at 15:40 ET on e, before the next entry at 15:50 ET on e.

## 4. Rules

Spot S at 15:50 ET on d by put-call parity on the e chain (`bd_real_quotes.spot`); K = round(S).

- **N1, iron fly:** sell the K call and K put, buy the K+5 call and K−5 put, expiry e. Max risk = 100 × (5 − credit)
  + fees.
- **N2, bull put spread:** sell the K put, buy the K−5 put, expiry e. Max risk = 100 × (5 − credit) + fees.
- **Entry skip:** any leg without a quote, or any leg wider than 25% of its mid and more than 2 cents (H4's rule).
- **Exit at 15:40 ET on e** from e's spy_0dte file: close every leg (shorts bought back, longs sold, including legs
  quoted 0 bid). If a leg has no quote at 15:40, use the latest minute from 15:39 back to 15:30 where all legs have
  one; otherwise the night is dropped and counted.
- No take-profit, no stop: overnight nothing trades, and the $5 wing caps the loss. The closing value is capped at
  the width.
- **Fills per leg:** mid; **patient** = mid ∓ 1 cent, never worse than natural (the paper fill rule); **taker** =
  bid/ask. Fees $0.04 per contract per leg per side, charged on every leg at entry and exit.

## 5. Periods and pass bar

In-sample 2016-01-01 to 2021-12-31; out-of-sample 2022-01-01 to the last day on disk, by d.

Each candidate passes only if **all three** hold:
1. in-sample mean P&L per trade > 0 at **patient**;
2. out-of-sample t (over trades) >= **2.58** at **patient** (one-sided 1%, Bonferroni over the two candidates; H4's
   bar was 2.33 for one);
3. out-of-sample mean > 0 at **taker**.

## 6. Reported, never selected from

- Exit at 15:25 ET instead of 15:40 (the earlier closeout Robinhood applies to non-late-close products).
- Value at settlement: legs at intrinsic against the 16:00 ET parity spot, no exit cost. Not tradeable on SPY
  (Robinhood closes at-risk positions from 15:45 ET); it measures how much of any edge sits in the last 20 minutes,
  which decides whether a cash-settled XSP version (N4) is worth a quote pull.
- Wings $3 and $10.
- Weekday nights vs weekend/holiday nights; by year; worst night; win rate and profit factor.
- The short ATM straddle with no wings, same timing, as % of entry premium (not tradeable at Level 3), and SPY's own
  return from 15:50 on d to 15:40 on e (context for N2's directional exposure).

## 7. What a pass does and doesn't mean

A pass makes the candidate eligible for a book spec, which needs Evan's approval, including the HANDOFF section 2
exception for short legs held overnight (books E and F2 already hold overnight) and an early-assignment rule for
in-the-money short legs. A fail drops it; no variant from section 6 is promoted in its place.
