# N3: weekly SPY put credit spread, sold at the week's last close and held to 15:40 ET on next week's expiry (pre-registration)

Status: **FROZEN 2026-10-12** (Evan picked "Run N3" on the thread's decision card after N1/N2 failed, PR #56).
Merged to main before the quote pull or the replay runs; any later change is an amendment with its own date. Research
only: no book, rule or engine change. Background: `research/n1_n2_1dte_close.md` and the project note
`research/new-strategy-2026-10-12.md` (shared folder).

## 1. Why

The variance premium is best documented at weekly and monthly horizons, where a week of premium is large next to
two legs of spread. Cboe's weekly PutWrite index (WPUT) had a gross Sharpe of 0.50 in 2006-2015 against 0.46 for the
S&P 500 (Bondarenko 2016). N2 showed that a short-dated put spread here mostly tracked SPY's drift, so this test also
reports how much of N3's P&L is left after SPY's own weekly move (section 6). That figure is descriptive, not part
of the bar.

## 2. Data

- **Entry (new, small pull):** `research/fetch_thetadata_spy_weekly.py` writes
  `data/thetadata/spy_weekly/YYYY-MM-DD.parquet`, named by entry day d: ThetaData 1-minute NBBO, 15:30-16:00 ET on d,
  all strikes, calls and puts, of the expiry e defined in section 3.
- **Entry spot:** d's own same-day expiry in the existing `data/thetadata/spy_0dte/` (put-call parity at 15:50 ET,
  where a same-day expiry has no carry; the 7-day chain would overstate spot by up to ~$0.60 at 5% rates).
- **Exit:** e's file in `data/thetadata/spy_0dte/` (e is then a same-day expiry).

## 3. Weeks traded

- **d** = the last trading session of a calendar week (Friday, or Thursday when Friday is a holiday).
- **e** = the last trading session of the following calendar week, if SPY lists an expiry that day.
- Skip the week if d or e is a 13:00 close, if e has no listed expiry or no spy_0dte file, or if d has no spy_0dte
  file (no entry spot). Positions never overlap: each closes at 15:40 ET on e, before the next entry at 15:50 ET on e.

## 4. Rules

At 15:50 ET on d:
- S = parity spot from d's same-day chain (`bd_real_quotes.spot`).
- EM = mid of the e-expiry ATM straddle at K0 = round(S) (the week's implied move).
- **Short put** K_s = round(S − 0.5 × EM) (about 0.25-0.30 delta). **Width** W = max(1, round(S / 100)) dollars
  (about 1% of spot: $2 in 2016, $6-7 in 2026). **Long put** K_s − W. Both expiry e.
- **Entry skip:** either leg without a quote, or wider than 25% of its mid and more than 2 cents (H4's rule), or a
  credit of zero or less.
- **Exit at 15:40 ET on e** from e's spy_0dte file: buy back the short, sell the long (latest fully quoted minute back
  to 15:30; otherwise the week is dropped and counted). Closing value capped at W.
- No take-profit, no stop.
- **Fills per leg:** mid; **patient** = mid ∓ 1 cent, never worse than natural; **taker** = bid/ask. Fees $0.04 per
  contract per leg per side.
- **Per trade:** P&L in $ per 1 lot; return on max risk, max risk = 100 × (W − credit) + fees.

## 5. Periods and pass bar

In-sample 2016-01-01 to 2021-12-31; out-of-sample 2022-01-01 to the last day on disk, by d. One candidate, so the
bar is H4's:
1. in-sample mean P&L per trade > 0 at **patient**;
2. out-of-sample t (over trades) >= **2.33** at **patient**;
3. out-of-sample mean > 0 at **taker**.

## 6. Reported, never selected from

- Short strike at S − 0.25 × EM and at S − 1.0 × EM; width 2W.
- Value at settlement (legs at intrinsic against e's last parity spot, no exit cost; not tradeable on SPY).
- **Net of SPY's move:** the OLS of per-trade P&L (patient) on SPY's return from 15:50 on d to 15:40 on e; the
  intercept and its t, and mean P&L in up weeks and in down weeks.
- By year; worst week; win rate; profit factor.

## 7. What a pass does and doesn't mean

A pass makes N3 eligible for a book spec, which needs Evan's approval, including holding a short put through the
week (HANDOFF section 2 exception) and an early-assignment rule for an in-the-money short put. A fail drops it, and
no section 6 variant is promoted in its place.
