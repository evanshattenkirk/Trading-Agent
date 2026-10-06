# New book candidates H1-H4: pre-registration (frozen 2026-10-06, before any result)

Thread "Find a profitable strategy for a new book" (Evan, 2026-10-06: "find a profitable strategy for a new one to
try"). Written and committed **before** any of the candidates below was run. Rules, parameters, periods, fills and
the pass bar are frozen here; anything changed after a result is a new, separately labelled variant (HANDOFF v3
section 12). Nothing here changes books A to G, the engine or `config.yaml`. A candidate that passes is a proposal
for Evan, who decides whether it becomes paper book H.

Why these four and not others (the full screen is in the project note `research/new-strategy-2026-10-06.md`):
everything directional and intraday that the repo tested failed out of sample, and 0DTE premium selling loses to
costs at real fills. These four rest on effects with a published mechanism and decades of evidence that the repo
has not tested yet:

| Code | Candidate | Published basis | Runs where |
|---|---|---|---|
| H1 | SPY overnight hold (15:55 ET to 09:35 ET next session) | Cliff, Cooper & Gulen (2008); Lou, Polk & Skouras (JFE 2019); Boyarchenko, Larsen & Whelan (RFS 2023) | here (2005-2020), then the Mac holdout |
| H2 | H1 only on nights after a down day | Boyarchenko, Larsen & Whelan (2023): sell-offs into the close are followed by larger overnight reversals (dealer inventory) | here, then the Mac holdout |
| H3 | RSI(2) dip in an uptrend, SPY shares | Connors & Alvarez (2008), short-term index mean reversion | here, then the Mac holdout |
| H4 | Overnight 1DTE SPY iron fly (sell 15:50 ET, buy back 09:45 ET next session) | Jones & Shemesh (JF 2018), Muravyev & Ni (JFE 2020): options price calendar time, so they lose value over non-trading hours faster than the realized move | Mac only (ThetaData) |

Known before testing, and recorded so the reading can't move afterwards: the authors of the overnight-drift paper
reported on 2026-07-01 ("The Disappearing Overnight Drift", Liberty Street Economics) that the 02:00-03:00 ET
window, which carried most of the drift, has averaged close to zero since 2021. H1 and H2 may therefore pass on
2005-2020 and fail the 2020-2026 holdout. The holdout decides; 2005-2020 alone never promotes a candidate.

## 1. Shared method for H1-H3 (shares)

- **Data here:** S&P 500 CFD 1-minute bars (Oanda, `research/fetch_data.sh` + `research/load_oanda.py`), regular
  sessions with at least 370 minutes (half days and gappy days dropped), 2005-01 to 2020-05. VIX daily close
  (`research/data/vix.csv`) is not used by H1-H3.
- **Periods:** in-sample 2005-01-01 to 2014-12-31; out-of-sample 2015-01-01 to 2020-05-31, by the session on which
  the position is opened (H1/H2) or the session being marked (H3).
- **Recent check (information only):** SPY 5-minute bars 2025-04-15 to 2026-03-20 (`data/spy_5m_2025_2026.csv`),
  full 78-bar sessions. This window lies inside the Mac holdout (section 5) and is reported only; it is not part of
  any bar and nothing is selected from it.
- **Prices:** the 15:55 ET price is the close of the bar ending at 15:55 (1-minute: the 15:54 bar; 5-minute: the
  15:50 bar). The 09:35 ET price is the close of the bar ending at 09:35. The daily close is the last regular-session
  bar's close. Price-only returns (no dividends) are the judged numbers; that understates a long holder by about
  0.6 bp per night (sensitivity row).
- **Trading sessions:** the next session after day d is the next weekday that is not an NYSE closure
  (`research/nyse_calendar.py` rules, plus 2007-01-02 and 2012-10-29/30 for the 2005-2020 data). A night d -> next
  counts only when both sessions are full sessions in the data; otherwise that night is a no-trade.
- **Fills per side, in bp of notional:**
  - **mid1** (the analog of the approved mid - 1c rule): 1 cent per share at the repo's SPY-765 scale = 0.131 bp,
    plus 0.3 bp regulatory fee on every sale (SEC section 31 + FINRA TAF).
  - **taker**: 1.0 bp per side plus the 0.3 bp sale fee. That is more than ten times SPY's real half-spread; it
    stands in for auction and opening slippage.
- **Size for the statistics:** a fixed $10,000 notional per position, one position at a time per candidate.
- **Statistic:** P&L per session in bp of notional, 0 on sessions with no trade (as `book_a_variants_prereg.md`):
  mean, standard deviation, t = mean / (sd / sqrt(n)). Also reported: trades, win rate, mean bp per trade, profit
  factor, annualized return on notional, Sharpe, worst trade, max drawdown, results by year.
- **Reference rows (not tests):** buy-and-hold marked 15:55 to 15:55, and the intraday complement (09:35 to 15:55
  every session).

## 2. Candidates H1-H3

### H1. Overnight hold
- Every session d whose next session is valid: buy at d's 15:55 ET price, sell at the next session's 09:35 ET price.
  Weekend and holiday nights included.

### H2. Overnight hold after a down day
- As H1, but only when d's 15:55 ET price is below the previous session's daily close (the previous session must be
  a full session in the data; otherwise no trade).

### H3. RSI(2) dip in an uptrend
- Daily close series: the full sessions in the data, in order. At 15:55 ET on session t the provisional series is the
  prior daily closes plus t's 15:55 price P.
- RSI(2): Wilder's RSI with period 2 (`agentdesk.indicators.RSI(2)`, its `preview` for P), run over the whole series
  from the first session.
- SMA200: mean of the 199 prior daily closes and P. SMA5: mean of the 4 prior daily closes and P.
- **Entry:** when flat, P > SMA200 and RSI(2) < 10: buy at P.
- **Exit:** when long, from the next session on, the first 15:55 ET price P > SMA5: sell at P. No stop, no time
  limit (as published).
- Marked to market at each 15:55 ET price, so each session carries that day's P&L; entry and exit costs fall on their
  sessions.

## 3. Pass bar, stage 1 (here; all three)

1. In-sample mean daily P&L > 0 at **mid1**.
2. Out-of-sample mean daily P&L t >= **2.33** at **mid1** (one-sided p of about 0.01 = 0.05 split over this file's
   four candidates and rounded to round 1's bar).
3. Out-of-sample mean daily P&L > 0 at **taker**.

A candidate that passes stage 1 goes to the Mac holdout (section 5). One that fails is reported and dropped.

## 4. H4: overnight 1DTE iron fly (Mac, ThetaData real quotes)

- **Data:** the existing same-day pull `data/thetadata/spy_0dte/YYYY-MM-DD.parquet`, plus one new pull,
  `data/thetadata/spy_next/YYYY-MM-DD.parquet`: for each trading day d, ThetaData 1-minute NBBO 09:30-16:00 ET of
  the first listed SPY expiry after d (all strikes, calls and puts). This is the same pull book G's real-quote check
  already needs (`strategies_new_writeup.md`).
- **Nights traded:** session d (not a 13:00 close) whose first expiry after d is the next trading session e, and d is
  not the session before an SPY ex-dividend date (the third Friday of March, June, September and December), where a
  short in-the-money call can be assigned early. Since November 2022 that is every night, Friday -> Monday
  included; before it, only the nights before a listed expiry.
- **Entry, 15:50 ET on d,** from the spy_next file: spot S by put-call parity as in `bd_real_quotes.spot`, K =
  round(S). Sell the K call and K put, buy the K+5 call and K-5 put, expiry e. One 4-leg credit order. Skip the night
  if any leg lacks a quote, or any leg's bid/ask is wider than 25% of its mid and more than 2 cents (the paper fill
  rule in `books.fills`).
- **Exit, 09:45 ET on e,** from e's spy_0dte file: buy back all four legs. If a leg lacks a quote at 09:45, the first
  minute up to 10:00 where all four have one; otherwise the night is dropped and counted.
- No take-profit and no stop: nothing can be traded overnight, and the $5 wings cap the loss.
- **Fills per leg:** mid; **patient** = mid -/+ 1 cent, never worse than natural (the mid - 1c rule); **taker** =
  bid/ask. Fees $0.04 per contract per leg per side ($0.32 per lot round trip).
- **Per trade:** P&L in $ per 1 lot; return on max risk, max risk = 100 x (5 - entry credit) + fees.
- **Periods:** in-sample 2016-01-01 to 2021-12-31; out-of-sample 2022-01-01 to the last day pulled (2026-09), by d.
- **Pass bar (all three):** in-sample mean P&L per trade > 0 at **patient**; out-of-sample t >= **2.33** at
  **patient** (t over trades); out-of-sample mean > 0 at **taker**.
- **Also reported, never selected from:** by year; weekday nights vs weekend/holiday nights; worst night; the same
  trade without wings (short ATM straddle, % of the entry premium; not tradeable in a Level 3 account) as a
  measure of the raw overnight premium.

## 5. Mac holdout for stage-1 survivors (H1-H3)

- **Data:** SPY SIP 1-minute bars `data/spy_1m` (Alpaca, from 2018-08), regular-session bars only; half days are
  not full sessions. Judged window 2020-06-01 to 2026-09-30, by the same session rule as section 1; H3 warms up its
  SMA200 and RSI on the bars before 2020-06. Fills, costs and the statistic as in section 1.
- **Bar (both):** mean daily P&L t >= **1.65** at **mid1** (one-sided 5%; this is a confirmation of a candidate
  already selected on 2005-2020, so the two stages together are far stricter than either alone) and mean > 0 at
  **taker**.
- Only a candidate that passes stage 1 **and** the holdout (or H4's bar) is offered to Evan as paper book H.

## 6. Sensitivities (reported, never selected from)

- H1: exit at the 09:31 and the 10:00 ET price; weekday nights only (no weekend or holiday nights); with 0.6 bp
  per night added for dividends; costs of 0.5 and 2.0 bp per side.
- H2: only when d's 15:55 price is at least 0.5% below the previous close; or, instead of the day's return, the last
  hour (15:00 to 15:55 ET) is down.
- H3: RSI(2) < 5; a 10-session time stop.
- H4: exit at 09:35 and 10:00 ET; wings at +/-$3 and +/-$10; weekday nights only.

## 7. What is not changed after seeing results

Candidates, rules, periods, fills, costs and both bars above. A new filter, exit time or structure that comes from
these results is a new variant for a new pre-registration, tested on data this run never touched (new paper days,
or a different instrument).

## 8. Run

Here (no network after `research/fetch_data.sh`):

    python research/load_oanda.py                 # data/spx_rth_1m.pkl
    python research/book_h_candidates.py          # book_h_candidates.md / book_h_candidates_results.json

Mac (repo root; the second and third need ThetaData):

    .venv/bin/python research/book_h_candidates.py --spy-1m data/spy_1m --holdout     # section 5, survivors only
    .venv/bin/python research/fetch_thetadata_spy_next.py                            # the spy_next pull
    .venv/bin/python research/h4_overnight_fly.py --quotes data/thetadata/spy_0dte \
        --next-quotes data/thetadata/spy_next --out data/thetadata/h4_results        # section 4
