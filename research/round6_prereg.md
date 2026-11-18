# Round 6: prediction markets, PM1-PM5 (pre-registration)

Status: **FROZEN 2026-11-18.** Evan asked "what else can we trade/consider kalshi and polymarkets etc ... be a genius
and brainstorm and backtest" (thread "Find new markets to trade, incl. prediction markets", 2026-11-18). This file is
committed before any replay script for these tests exists and before any outcome is computed; a later change is an
amendment with its own date. Research only: no book, rule or engine change. The agent can't trade event contracts
through Robinhood's MCP (it has no prediction-market tools), so a pass is information for Evan, and any live use
would need a new venue (Kalshi's API) and his approval.

What was looked at before this file was frozen (so a reader can judge the contamination):
- Round 5's K1 (Kalshi favourites 70-97c a day before the close, all categories): FAIL, out-of-sample −0.68% a
  contract at the ask; the longshot side loses more than half; maker version +2.8% (fills unknowable).
- Kalshi API field lists; one 2025 and one 2026 S&P range event (brackets, volumes, a few minute candles); the
  current fee type of each series used here; event counts per series. No entry rule was evaluated on any of them.
- XSP 0DTE quote files (round 5 pull, 2016-01-22 to 2026-10-29): columns only. Round 5's W/WX results (the 0DTE
  variance premium is real at settlement value; option spreads take it).
- Cleveland Fed monthly nowcast JSON: structure only.
- Public write-ups: a Fed FEDS paper (2026-010) finds Kalshi's headline CPI median beats the Bloomberg consensus; two
  hobby studies find Kalshi S&P brackets agree with SPX option-implied odds within detection limits on a handful of
  days; an Odds Reference study finds Kalshi weather markets about efficient after fees. Weather and sports are
  not tested here for that reason (and sports are not a forecasting strength of this project).

Why these five. Every earlier round found a premium that is real at mid or at settlement value and is eaten by
spreads on the way out. A Kalshi contract held to settlement pays the spread and fee once and nothing to exit.
PM1 and PM4 ask whether the variance premium (risk-neutral tails too fat) shows up in Kalshi's range brackets.
PM2 asks whether Kalshi's S&P prices drift from what SPX options imply, on our own option quotes. PM3 asks the
same of Bitcoin against a model built only from live prices. PM5 asks whether a public nowcast beats Kalshi's
CPI brackets.

## 1. Common rules

- **Data:** Kalshi's public API (no account): `/events?series_ticker=`, `/markets` and `/historical/markets`
  (`event_ticker=`), and minute candles (`period_interval=1`) from `/series/{s}/markets/{t}/candlesticks` or
  `/historical/markets/{t}/candlesticks`. Markets that settled `yes` or `no` only; voided or scalar markets dropped
  and counted.
- **Quote at entry time E:** the candle whose period ends at E (`end_period_ts == E`). If its yes bid or yes ask is
  missing, the latest candle ending in (E − 5 min, E] that has both; otherwise the market is skipped at E and
  counted. YES costs the yes ask; NO costs 1 − yes bid. A side is buyable only if its cost is in [0.02, 0.98].
- **Taker fee:** `0.07 × P × (1 − P)` per contract at cost P, times the series' current `fee_multiplier` (1 for
  every series used here today), for a 100-contract order rounded up to the whole cent: fee per contract =
  ceil(100 × 0.07 × P × (1 − P) × 100) / 10,000. Fee history is not modelled (the report says so).
- **Return per contract** = (payout − P − fee) / (P + fee), payout 1 or 0, held to settlement (no exit fee).
- **Clustering:** returns are averaged within an event first (one S&P day, one Bitcoin hour, one CPI release);
  t is over events. Events with no trade are dropped from that test's mean.
- **Halves:** events in time order, split at the middle event.
- **Family:** five tests. **Bar: t ≥ 2.58** (5% two-sided, Bonferroni over five) on the full sample at the taker
  fill, **and** a positive taker mean in each half. A test-specific extra condition is listed under each test.
- **Reported, never selected:** maker fill (buy at the bid, no fee; fills unknowable from candles), a 0.035 fee
  rate (some secondary sources list S&P and Nasdaq at half rate), and the sensitivities under each test. A fail
  drops the candidate; no sensitivity row is promoted in its place.
- **Sample end:** events closing on or before 2026-11-13. **Sample start:** the first event in each series that
  has the markets the rule needs (reported).

## 2. PM1: S&P 500 daily range, buy the modal bracket at noon

**Events:** series `KXINX` (including its legacy-ticker events), close 16:00 ET, with at least five `between`
markets. **Entry E = 12:00 ET.** Among the event's `between` markets with both quotes at E, the one with the highest
mid (yes bid + yes ask)/2 (ties: lower floor strike). Buy YES at the ask. Hold to settlement.
**Extra condition:** the same rule on `KXNASDAQ100` (a fresh index) has a positive taker mean.
**Sensitivities:** E = 10:00, 14:00, 15:00 ET; buy the modal bracket and its two neighbours.

## 3. PM2: Kalshi S&P range markets vs XSP option-implied odds

**Events:** PM1's events on sessions with an XSP 0DTE quote file (`data/round5/xsp_0dte`, through 2026-10-29).
**Entry E = 12:00 ET.** XSP quotes from the 11:59 ET row. A quote is usable if bid > 0 and ask > bid.
- **Digital curve.** For each pair of adjacent listed strikes k1 < k2 (XSP units) where both legs are usable:
  above the parity spot use calls, P(S ≥ (k1+k2)/2) = (mid C(k1) − mid C(k2)) / (k2 − k1); at or below it use puts,
  P(S ≥ m) = 1 − (mid P(k2) − mid P(k1)) / (k2 − k1). Spot by put-call parity at the listed strike nearest the
  forward (nearest strike with both legs usable: S = K + C − P). Points are clipped to [0, 1] and made non-increasing
  in strike by a running minimum from the low end. D(x) is linear interpolation between points; x outside the
  first and last point is not priced.
- **Fair value** for a Kalshi market, with x = SPX level / 10: `between` [lo, hi]: D(lo/10) − D(hi/10);
  `greater` floor f: D(f/10); `less` cap c: 1 − D(c/10).
- **Trade:** edge_yes = fair − yes ask − fee(yes ask); edge_no = (1 − fair) − (1 − yes bid) − fee(1 − yes bid).
  If the larger edge is ≥ 0.03 and that side is buyable, buy it. Every market in the event is checked; a day's
  return is the mean over its trades.
- **Sensitivities:** threshold 0.01 and 0.05; E = 14:00 ET.

## 4. PM3: Bitcoin hourly above/below vs a live-price volatility model

**Events:** series `KXBTCD` (hourly, settles on the CF Benchmarks BRTI average), close times from the first event
listed through 2026-11-13. If more than 1,500 events qualify, keep those whose SHA-1 of `event_ticker` mod k = 0,
with the smallest k that keeps at most 1,500 (no outcome used).
**Entry E = close − 15 min.** Spot S = close of the Coinbase BTC-USD 1-minute candle ending at E (public
`api.exchange.coinbase.com`, granularity 60). σ = standard deviation of the 360 1-minute log returns ending at E.
For a `greater` market with floor K: fair = Φ(ln(S/K) / (σ √15)). Markets: the six `greater` markets whose floor is
nearest S. Trade as PM2 (edge ≥ 0.03, the larger side, all six checked).
**Sensitivities:** E = close − 5 min and close − 45 min; σ from 60 and 1,440 returns; threshold 0.01 and 0.05.

## 5. PM4: Bitcoin hourly range, buy the modal bracket

**Events:** `KXBTC` events paired with PM3's sampled hours (same suffix, e.g. `KXBTCD-26NOV1717` ↔
`KXBTC-26NOV1717`). **Entry E = close − 15 min.** Among the six `between` markets whose midpoint is nearest S
(PM3's spot), the one with the highest mid (ties: lower floor). Buy YES at the ask.
**Sensitivities:** E = close − 45 min; modal plus its two neighbours.

## 6. PM5: CPI month-over-month brackets vs the Cleveland Fed nowcast

**Events:** series `KXCPI` (headline CPI m/m, including legacy tickers), every release from the first event listed
through 2026-11-13. **Entry E = 15:00 ET on the last weekday before the market's close day** (close is just before
the 08:30 ET release).
- **Nowcast N:** the Cleveland Fed's headline CPI m/m nowcast for that month (`nowcast_month.json`), the latest
  daily value dated on or before the entry date.
- **σ:** root-mean-square of (actual − nowcast-at-the-same-lead) over every earlier month in the file whose actual
  was released before the entry date, at least 24 months (otherwise the event is skipped). "Same lead" = the latest
  nowcast dated on or before the last weekday before that month's release.
- **Fair value:** the printed figure is rounded to 0.1, so for a `greater` market with floor f (printed > f):
  fair = 1 − Φ((f + 0.05 − N) / σ); `less` cap c: Φ((c − 0.05 − N) / σ); `between` [lo, hi]:
  Φ((hi + 0.05 − N)/σ) − Φ((lo − 0.05 − N)/σ). If a market's strike semantics can't be read from its fields, it is
  skipped and counted.
- **Trade:** as PM2 (edge ≥ 0.03, the larger side, every market checked).
- **Note:** about 60 releases, so the test has little power; a pass would still need the 2.58 bar.
- **Sensitivities:** threshold 0.01 and 0.05; entry at 15:00 ET two weekdays before the release.

## 7. Order of work

1. This file committed on branch `claude/project-thread-e7nhpk`.
2. Scripts: `research/round6_fetch.py` (Kalshi, Coinbase and Cleveland Fed pulls, resumable, into
   `research/data/round6/`, gitignored) and `research/round6.py` (rules and report), with synthetic-data tests.
3. Runs and one report, `research/round6.md`, with every verdict, pass or fail.

## Amendments

(none yet)
