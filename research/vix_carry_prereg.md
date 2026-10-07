# V1: short VIX futures carry (SVXY) while the VIX term structure is in contango — pre-registration

Date: 2026-10-06, thread "Find a profitable strategy for a new book", round 2. Written and committed **before any VX
futures, VIX3M or SVXY data was downloaded or looked at** in this repo. Nothing below changes after a result is
seen; anything added later is a new, separately labelled variant.

## 1. Why this one

Round 1 (`book_h_candidates.md`, `h4_overnight_fly.md`) found the same thing as B, D and Vilkov: the option variance
premium is there at mid, but two or four legs of SPY spreads, paid on every trade, eat it. H4 earned $1-2 a lot at
mid and lost $5 at mid −1¢. The premium has to be collected somewhere the costs are small next to it.

VIX futures carry is the same premium in a cheaper wrapper. VIX futures usually trade above spot VIX (contango) and
drift down to it as they near settlement, so a short position earns the roll. Positions change only when the term
structure flips, so costs are paid a few dozen times a year, not twice a day.

Evidence (none of it from this repo):
- Whaley (2013, *Journal of Portfolio Management*), "Trading volatility: at what cost?": long VIX-futures ETPs lose
  structurally, which is the short side's return.
- Simon & Campasano (2014, *Journal of Derivatives*), "The VIX futures basis: evidence and trading strategies": the
  basis predicts VIX futures returns; shorting in contango earned significant returns.
- Johnson (2017, *JFQA*), "Risk premia and the VIX term structure": the slope of the VIX term structure predicts
  VIX futures and variance swap returns.
- Cheng (2019, *Review of Financial Studies*), "The VIX premium": the premium for being short VIX futures shrinks or
  turns negative when risk rises, which is what a contango filter tries to avoid.

The known risk is the tail. SVXY, then −1x, lost about 90% on 2018-02-05, and ProShares cut it to −0.5x on
2018-02-28. The test uses −0.5x throughout and reports the worst days.

## 2. The rule (V1)

- **Instrument modeled:** −0.5 × the daily excess return of the S&P 500 VIX Short-Term Futures index (SPVXSP),
  rebuilt from CBOE VX monthly futures settlements (section 3), minus SVXY's 0.95% a year expense ratio on every
  session the position is held. No interest is credited on cash, held or flat (conservative).
- **Signal:** c(t) = VIX close(t) / VIX3M close(t), both CBOE closes. Contango when c(t) < 1.00.
- **Position:** p(t) is the position held from the close of session t to the close of session t+1.
  p(t) = 1 if c(t−1) < 1.00, else 0. The one-session lag means the decision at t uses only closes the engine
  already knows that morning. If c(t−1) is missing, p(t) = p(t−1).
- **Daily return:** s(t+1) = p(t) × (−0.5 × R(t+1) − 0.0095/252) − cost × |p(t) − p(t−1)| − 0.3 bp sale fee when
  p(t) = 0 and p(t−1) = 1. R(t+1) is the index return from the close of t to the close of t+1.
- No stop, no take-profit, no other filter, no sizing rule (returns are per dollar of notional).

## 3. The index (SPVXSP excess return, S&P methodology)

- **Contracts:** VX monthly futures only (weeklies ignored). Settlement date S(y, m) of the contract for month m:
  30 calendar days before the third Friday of month m+1; if that Friday is not a CBOE session, 30 days before the
  session before it; if the result is not a session, the session before it.
- **Sessions:** dates on which VX futures have settlements; beyond the data, NYSE weekdays.
- **Roll period k:** sessions from S_k (included) to S_(k+1) (excluded). On a session t in it the index holds the
  contract settling S_(k+1) (first month) with weight w1(t) = dr/dt and the contract settling S_(k+2) with 1 − w1(t),
  where dt = sessions in the roll period and dr = sessions after t and before S_(k+1). Weights are set at the close.
- **Return:** R(t) = [w1(t−1)·F1(t) + w2(t−1)·F2(t)] / [w1(t−1)·F1(t−1) + w2(t−1)·F2(t−1)] − 1, with the two
  contracts held at the close of t−1 priced on both days (daily settlement prices).
- **Missing data:** a contract's missing or zero settlement is carried forward for at most 2 sessions; beyond that
  R(t) is missing and s(t) = 0 for that session. The count is reported.

## 4. Data

- CBOE VX monthly futures daily settlements, one public CSV per contract (CBOE's historical data pages; the fetch
  script `fetch_cboe_vx.py` records the URL used for each).
- CBOE VIX and VIX3M daily closes (CBOE index history CSVs). VIX3M (formerly VXV) starts in December 2007.
- Validation only: SVXY daily closes from Alpaca (split-adjusted), 2018-03-01 onward.

## 5. Periods, costs, statistics, pass bar

- **In-sample:** 2008-01-02 to 2014-12-31 (or from the first session with every input, if later; reported).
  **Out-of-sample:** 2015-01-02 to 2026-09-30. Every session counts, flat sessions as 0.
- **Costs per position change (one side):** mid1 (patient, about 1¢ at SVXY's usual price) 3 bp; taker 10 bp. The
  0.3 bp sale fee is added on exits in both.
- **Statistics:** daily mean and t. The t used for the bar is the smaller of the plain t and the Newey-West t with 5
  lags. Also: annualized return and volatility, Sharpe, maximum drawdown of the compounded series, worst day,
  share of sessions held, switches a year, by-year table, the 10 worst days with dates.
- **Pass bar (all three):**
  1. In-sample mean > 0 at mid1.
  2. Out-of-sample t ≥ 2.33 at mid1.
  3. Out-of-sample mean > 0 at taker.
- **Run validity (not part of the bar):** the rebuilt −0.5 × index return, less the expense ratio, must correlate
  ≥ 0.95 with SVXY's actual daily return from 2018-03-01. If it doesn't, the index or the data has a bug: fix the bug,
  never the rule, and rerun. The correlation and the yearly gap between the two are reported.

## 6. Sensitivities (reported, never selected from)

Threshold 0.95 and 0.90; no filter (always short); −1x leverage; no lag (p(t) from c(t), which uses closes not yet
final at the 15:00 CT decision, so an upper bound); front-month basis filter (first-month settlement > VIX) instead
of VIX/VIX3M; zero costs.

## 7. What follows

- **Pass:** a spec for a paper book trading SVXY shares through the existing equity paper broker, held across days and
  kept at shutdown (like E and F2), with size, notional cap and loss limits left to Evan (the planning case is a −50%
  day). The live signal needs Robinhood's index data to serve VIX3M (it already serves VIX); the build checks that,
  and checks SVXY's tradability in shadow mode.
- **Fail:** dropped, with the result committed.
