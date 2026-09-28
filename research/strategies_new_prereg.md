# New strategy candidates: pre-registration (2026-09-28)

Written and committed **before** any candidate below was backtested. Rules, parameters and the decision rule are
frozen here; anything changed after seeing results is a new, separately labelled variant (HANDOFF v3 section 12).

These are proposals for *additional* paper books (F, G, ...). Nothing here changes books A to E, the engine or config.

## Shared method (same as `research/strategies_bcd.py` unless stated)

- Data: S&P 500 1m CFD 2005-01..2020-05 (Oanda) as 5m bars, split 2005-14 (in-sample) and 2015-20
  (out-of-sample); SPY 5m 2025-04..2026-03 (recent). VIX daily close.
- Prices re-based so the entry session opens at SPY 765; multi-day trades keep the entry session's factor.
- Options: Black-Scholes, flat IV from the **prior** VIX close. Priced variance, in units of one VIX-implied day
  `v = (VIX/100)^2 / 252`:
  - regular session remaining after bar k closes: `m_rth^2 * v * ((77 - k)/78 + 15/390)` (as `sd_left`)
  - overnight (close to next open): `m_on^2 * v`
  - base case `m_rth = 0.80`, `m_on = 0.60` (so a whole 1DTE day is priced at about the VIX-implied variance).
  - stress: both scaled by `s = 0.75` (the "IV 0.60, little premium" case).
  - break-even `s`: the premium scale at which the average trade is zero at mid - 1c fills.
- Fills, per leg per side, plus $0.04/contract/leg fees:
  - **mid - 1c**: 1c from mid (the paper fill rule Evan approved).
  - **natural**: 2c from mid for same-day legs, 3c for next-day legs.
- Exits are checked at each 5m bar close on the structure's modeled closing value, as in `condor_like`.
- Every filter uses only data known at entry.
- Stats per trade as % of max risk (credit structures) or % of debit (calendar); by period and by year;
  $ per 1 lot; max drawdown of the 1-lot equity curve; daily-P&L correlation with book D (unfiltered).
- Capacity in the $10k paper account: `lots = floor($300 / max loss per lot)` (HANDOFF section 11, $10k row),
  inside the $1,500 account open-risk cap.

Bar index k: 5m bar k closes at 09:35 + 5k ET. k=5 is 10:00, k=47 is 13:30, k=70 is 15:25 ET.

## Candidates

### F1. Afternoon iron condor (13:30 to 15:25 ET)
- Thesis: 0DTE time decay is fastest late in the session; D sells at 10:00 and carries the whole day's risk.
- Entry k=47: short call `ceil(S + 0.9 EM)`, short put `floor(S - 0.9 EM)`, EM = remaining-session priced sd;
  wings $2 beyond. Skip if the opening credit after costs is below $0.10.
- Exits: take profit when the closing debit is at most 50% of the credit; stop when the debit reaches 2x the
  credit; otherwise close at k=70 (15:25 ET).
- Sensitivity (not a separate test): price remaining variance with the trailing 250-session intraday
  variance profile (U-shape) instead of the flat share.

### F2. 10:00 ET put credit spread (put side of D, alone)
- Thesis: the put half of D keeps the variance premium plus the market's upward drift and pays half the legs.
- Entry k=5: short put `floor(S - 0.9 EM)`, long put $2 lower. Skip if credit after costs < $0.10.
- Exits: as F1, close at k=70.
- **F2-trend variant** (declared now): trade only when the prior close is above the average of the prior 50
  closes. 50 rather than 200 days because the 2025-26 SPY file starts in April 2025 and a 200-day average would
  leave about 40 recent sessions.

### F3. 0DTE / 1DTE ATM call calendar (10:00 to 15:25 ET)
- Thesis: sells today's decay while the next-day call hedges the tail, so the worst day is the debit, not a wing.
- Entry k=5, Monday-Thursday only when the next session is the next calendar day: sell the 0DTE call and buy the
  next-day call, both at `round(S)`. One 2-leg debit.
- Exits: take profit at +25% of the debit, stop at -35%, otherwise close both legs at k=70.
- Known model limit: IV is held at the prior VIX close all day, so the calendar's long vega on selloffs is ignored.

### F4. Overnight put credit spread (15:25 ET to 10:00 ET next day)
- Thesis: overnight index returns carry most of the equity drift (Cliff, Cooper & Gulen 2008), and delta-hedged
  option returns are most negative overnight (Muravyev & Ni 2020), so a short put held overnight collects both.
- Entry k=70, Monday-Thursday only when the next session is the next calendar day, in the next-day expiry:
  short put `floor(S - 1.0 * sd_exit * S)`, long put $2 lower, where `sd_exit` is the priced sd from entry to
  10:00 ET next day. Skip if credit after costs < $0.10.
- Exits: take profit at 50%, stop at 2x credit, checked at every bar close today and next morning; otherwise
  close at k=5 next day (10:00 ET).
- **Needs Evan's OK before paper even if it passes**: it holds short legs past 15:30 ET (they expire the next
  day, so it is not a 0DTE hold into expiry, but HANDOFF section 12 says never hold short legs into 15:30 ET).

### Reference: D unfiltered, re-run through the same code, to check the new code reproduces
`strategies_bcd.condor_like(kind="condor")` and to measure correlation.

## Decision rule (frozen)

Five tests (F1, F2, F2-trend, F3, F4), so the bar is the HANDOFF's multiple-test bar, |t| > 2.86.
A candidate is recommended for a paper book only if all hold:
1. t > 2.86 in 2005-14 at mid - 1c.
2. Average > 0 at natural fills in 2015-20 and in 2025-26.
3. Break-even premium scale `s` at most 0.85 (it survives premium 15% cheaper than modeled).
4. At least 1 lot fits the $300 per-position cap.
At most two are recommended. Every recommendation still needs the real-quote replay (ThetaData) before paper
promotion; F1 and F2 can run on the same-day-expiry pull, F3 and F4 need next-day expiry quotes as well.

## Addendum 1 (2026-09-28, after the first F1-F4 run, before any G run)

### Two metric fixes found in the first run (they change no rule or trade)
- **Break-even s** is now computed on the fixed set of trades taken at s = 1. The first run re-applied the $0.10
  minimum credit at every s, so the trade set shrank as s fell and the search returned its lower bound (0.40)
  for F1, F2 and F4. The s = 0.75 table rows still re-apply the minimum credit, because real cheaper premium would
  also skip those days.
- **Capacity** is now per trade, `floor($300 / that trade's max loss)`, and "fits the cap" means the median trade
  fits at least 1 lot. The first run divided by the single largest max loss in the whole period (for F3 a $755
  debit in 2008), which reported 0 lots although about 90% of F3's debits are under $3. Days where no lot fits
  are counted as not traded in `$ per year at cap`.

### New candidates G1 and G2 (registered before they were run)
Both are directional calendar/regime effects on daily data, expressed as a **$5-wide call debit spread** (long
`round(S)` call, short +$5 call) that expires on the exit session. Debit spreads largely cancel the variance
premium, so these are the least model-dependent option tests here. Multi-day legs are priced on a trading-day
clock: each whole session ahead adds `m_on^2 v + m_rth^2 v`. Entry at the 15:55 ET bar close (k=76); exit at the
exit session's 15:25 ET bar close (k=70), before the 15:30 ET short-leg rule. No take-profit or stop. One position
at a time. Fills and fees as above. The underlying's own return over the same window (minus 2 bp) is reported too,
since it needs no option model.
- **G1. Turn of the month** (Lakonishok & Smidt 1988; McConnell & Xu 2008): enter at the close of the
  second-to-last trading session of the month, exit on the third trading session of the new month. Sessions are
  counted from the data's own session list.
- **G2. VIX stretch rebound** (buy after fear spikes): enter at the close of session t when the prior VIX close
  (t-1) is above 1.20x the average of the 10 VIX closes before it; exit on session t+5.

The decision rule is unchanged and now covers seven tests (F1, F2, F2-trend, F3, F4, G1, G2); the bar stays
|t| > 2.86, which is already stricter than a Bonferroni bar for seven tests (about 2.69).
