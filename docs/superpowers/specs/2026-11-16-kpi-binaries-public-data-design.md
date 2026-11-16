# K1: KPI binaries on numbers that leak before the report — design

Date: 2026-11-16. Thread "Test satellite edge on KPI binaries". Status: **draft for Evan's approval**; nothing is
built until he approves it.

## 1. Intent

Robinhood will offer Cboe's KPI binary options: a call pays $1 if a company's reported number (revenue, a segment
line, a customer count) is at or above the strike, otherwise $0. Evan asked how to profit from them. Satellite
imagery fits only Target, and R1 already found no edge in a free retail demand signal (2026-11-10). So on 2026-11-16 Evan
chose to test a different source of edge first: **KPIs whose value is mostly public before the report**. If public
data pins the number, a binary priced far from that number is mispriced, and that's the clearest edge these
contracts can offer.

K1 is research plus paper observation. No orders, no new engine book, no code in `agentdesk/` until a result passes
and Evan approves a book.

## 2. Contract facts the design relies on

Sources: Cboe Day-1 product list v3 (2026-10-26), Cboe KPI FAQ (2026-11-06), rule filing SR-CBOE-2026-061 (SEC
approved 2026-10-13).

- Call pays $1.00 if reported KPI >= strike; put pays $1.00 if KPI < strike. Prices $0.01 to $1.03, 1¢ ticks.
  Limit or stop-limit, Day or IOC. Buyers pay in full.
- Expiry is the earnings release date. Companies that report before the open (AM-settled) stop trading at 15:00 CT
  the day before; after-close reporters (PM-settled) stop at 15:00 CT on the day. HOOD, MARA and COIN all report
  after the close (PM).
- Settlement value is the figure in the earnings press release (8-K Exhibit 99), at its highest printed precision,
  never revised later.
- Strikes are the KPI divided by a scale factor (1e9 for $ billions, 1e6 for millions); strike intervals are 0.01
  below 10, 0.10 from 10 to 100, 1.00 from 100 to 1,000. Cboe lists strikes "reasonably close" to the at-the-money
  value; up to two quarterly expirations.
- No Cboe or Robinhood fees through 2026-12-31; 2027 fees unknown.
- Launch date: not set as of 2026-11-13 (waits on Cboe Clear US's SEC registration). A check-back runs 2026-11-18.

## 3. The KPIs (fixed now)

| Contract | KPI | What's public before the report | Expected precision |
| --- | --- | --- | --- |
| KCHOD | HOOD Funded Customers (#m) | Monthly operating data for months 1-2 of the quarter (published ~2nd week of the next month); month 3 is only in the earnings release | ~1 month of growth (~0.1-0.3m), 1-3 strike intervals |
| KBHOD | HOOD Revenue ($b) | Same monthly data: equity notional, options contracts, crypto notional, event contracts, margin book, cash sweep, securities lending revenue | a few % |
| KDHOD | HOOD ARPU ($/user) | Derived: revenue nowcast x 4 / average of start and end funded customers (the 10-Q definition) | follows the two above |
| KCMAR | MARA Total Blocks Won (#) | Every MARA Pool block is on-chain (mempool.space pool attribution); the whole quarter is known ~4 weeks before the report | near exact, if MARA's count matches the pool tag |
| KFMAR | MARA Bitcoins Mined (#k BTC) | Block rewards (subsidy + fees) of those blocks, on-chain | near exact, minus any third-party pool share |
| KBMAR | MARA Revenue ($m) | Bitcoins mined x daily BTC price, plus a hosting/other line fitted on past quarters | a few % |
| KCCOI | COIN Transaction Revenue ($m) | Coinbase Exchange public daily volumes (and total crypto spot volume), the whole quarter known before the report | likely 5-15%; the control case |

Not in K1: HOOD Gold subscribers (no monthly data; trend only), HOOD EPS, MARA hashrate and holdings (MARA's
monthly updates appear to have stopped after September 2025; sales since 2026 move holdings), COIN EPS and
subscription revenue. They can be added later as a separately labelled variant.

## 4. Approaches considered

1. **Public-data KPIs (chosen by Evan, 2026-11-16).** Free data, short history (14-22 quarters per KPI), but the
   edge doesn't hinge on beating analysts; it hinges on the binary price disagreeing with numbers anyone can see.
2. Target revenue as a stand-in for satellite parking-lot counts (Census store sales + R1's search data, pooled over
   19 retailers). Needs historical revenue consensus (paid FMP tier); R1's failure makes it a weak bet.
3. Record prices at launch only. No history; slowest. K1 includes this as Stage 2, so it isn't lost.

## 5. Stage 1: how well does public data pin each KPI? (historical, starts on approval)

**Rule of information:** a nowcast for a quarter uses only data published before that quarter's trading cutoff
(15:00 CT on the report day for these PM reporters). Monthly releases count from their publication timestamp.
Model parameters for quarter q are fit only on quarters before q (expanding window, minimum 4 quarters).

**Nowcasts:**
- Funded customers: last published month-end count + that month's change (a random-walk-in-growth). Naive
  baseline: prior quarter's reported count + prior quarter's change.
- HOOD revenue: each revenue line = observed driver x that line's rate from the previous quarter's earnings release
  (options revenue per contract, equity and crypto bps of notional, event contracts revenue per contract, net
  interest as a yield on margin + sweep + cash, securities lending as reported, "other" as last quarter). Month 3
  drivers = months 1-2 average daily volume x month 3 trading days; balances carried at month-2 level.
  Naive baseline: prior quarter revenue x (1 + prior quarter growth).
- ARPU baseline: naive revenue x 4 / average of naive start and end funded customers.
- Blocks won: count of MARA Pool blocks with timestamps inside MARA's fiscal quarter (calendar quarter, UTC; the
  builder confirms the time zone against reported history). Bitcoins mined: sum of those blocks' rewards. Then a
  fitted ratio (reported / on-chain) from past quarters, which absorbs any third-party pool share.
  Naive baseline (difficulty-aware, so the halving and hashrate jumps don't make it a straw man): MARA Pool's share
  of all network blocks last quarter x all network blocks this quarter; bitcoins mined = that x this quarter's
  average reward per network block.
- MARA revenue: bitcoins mined x volume-weighted daily BTC price (Coinbase Exchange candles) + other revenue
  carried from last quarter. Naive baseline: the naive bitcoins mined x the same average price + other revenue.
- COIN transaction revenue: fitted ratio of transaction revenue to Coinbase Exchange USD volume (sum over its top
  products) from past quarters, times this quarter's volume. Naive baseline: prior quarter x (1 + prior quarter
  growth).

**Outputs per KPI:** out-of-sample errors (percent and in strike intervals), median absolute error, the predictive
distribution (Student-t on past log errors, df 4), 80% interval coverage, and the Brier score on synthetic binaries at
the at-the-money strike and +/-1, 2, 3 intervals, versus the naive baseline.

**Stage 1 gate (per KPI):** a KPI goes on to Stage 2 if its out-of-sample median absolute error is at most half the
naive baseline's, and its 80% interval covers between 60% and 95% of quarters. A KPI that fails is still recorded in
Stage 2 (prices are free) but isn't paper-traded. Stage 1 alone proves no edge; it only says whether we know more
than a trend line. COIN transaction revenue is the exception: it is paper-traded whatever its gate result (see
section 6).

**Data sources (all free):** Robinhood IR monthly and quarterly releases; MARA and Coinbase quarterly releases;
mempool.space pool blocks API; Coinbase Exchange public candles. Reported actuals are typed into one CSV with the
source URL and the exact printed figure; SEC filings are Mac-only (the cloud proxy 403s SEC), so the IR pages are the
primary source and the Mac cross-checks Exhibit 99 where they differ.

## 6. Stage 2: are the binaries mispriced? (live paper, starts at launch)

**Quotes:** once launched, record every listed strike (calls and puts) for the seven contracts: bid, ask, sizes, last
and volume, daily at 14:30 CT from listing, hourly on the last 3 trading days, and a final snapshot at 14:50 CT on
the last trading day. Source: Robinhood MCP read-only quote tools if they return KPI binaries; if not, Cboe's public
delayed quotes. Never an order tool. If neither source works, Stage 2 waits and Evan is told.

**Paper trade rule (frozen now, run on the final snapshot only):**
- Trades count only when the snapshot came from a live quote source (Robinhood MCP). Trades on Cboe's delayed
  quotes are logged and scored but never count toward the pass bar.
- Fair value P = model probability that KPI >= strike, from the Stage 1 distribution refreshed with all data
  public by then.
- Buy the call at its ask if P - ask >= 0.10; buy the put at its ask if (1 - P) - put ask >= 0.10. At most one side
  per strike. 10 contracts per trade. Only quotes with size >= 10 and a two-sided market.
- Fees: $0 through 2026; from 2027, $0.02 per contract as a placeholder until Robinhood publishes its fee.
- Settlement per the press release figure, checked against Cboe's KPI Settlement Values page.

**Scoring:** P&L per contract and per KPI-quarter; Brier score of the model versus the market mid at the final
snapshot; a calibration table of market mid vs outcome (useful even if we never trade).

**Cluster = one company report.** HOOD's three KPIs settle on one release and ARPU is built from the other two;
MARA's three share one on-chain count. So every trade from one company's report is one cluster (strikes and KPIs
inside it are not independent).

**Pass bar (frozen):** HOOD and MARA trades only (COIN scored separately). At least 12 settled company reports with a
trade; mean P&L per contract > 0 with t >= 2.33 clustered by company report; positive in both the first and second
half of the reports; model Brier below the market mid's. Then, and only then, a spec for a paper book goes to Evan.
With two companies reporting quarterly that is at least 6 seasons of HOOD and MARA together (~18 months, fewer if a
company has two expirations listed); each season's report says where it stands rather than declaring anything early.

**COIN, the low-information comparison:** always paper-traded with the same rule, scored on its own and shown next
to HOOD/MARA. If COIN earns as much as HOOD/MARA, the profit is coming from the market being loose in general, not
from leaked data; the report says so and Evan decides what that means.

**Kill rule:** after 2 full seasons, if the market mid's Brier is at or below the model's on every KPI, stop the
paper trading and keep only the calibration log. **End date:** after 8 seasons (2 years) with the bar unmet, K1
reports "inconclusive" and stops.

## 7. Where things go

- `research/k1_kpi_prereg.md`: sections 3, 5 and 6 frozen before any actual KPI value or nowcast is compared.
- `research/k1_kpi_nowcast.py` (+ tests in `research/tests/`): Stage 1; data in `research/data/k1/`, a copy in
  `/mnt/project-files/research/k1/`.
- `research/k1_kpi_quotes.py`: the Stage 2 recorder, read-only, built after launch once a quote source is confirmed;
  a Mac launchd job only with Evan's OK at that time.
- Results: `research/k1_kpi_nowcast.md`, then one section per earnings season.

## 8. Non-goals and anti-goals

- No live orders; no `place_*`, `cancel_*` or `exercise_*` calls. No engine book.
- No paid data. No satellite imagery.
- Never change a nowcast or the trade threshold after seeing binary prices or outcomes; a change is a new labelled
  variant.
- A failure: calling an edge on one season, or on strikes of one KPI-quarter counted as independent trades.

## 9. Calls I made (Evan can override)

- Cluster by company report, not by KPI: honest, but it stretches the pass bar to ~18 months.
- Only live-quote trades count; delayed-quote trades are logged.
- The 10¢ edge threshold, 10-contract size and final-snapshot-only trading.
- COIN transaction revenue is always traded as the low-information comparison, outside the pass bar.
- HOOD Gold, MARA hashrate and holdings are left out (no public data before the report).

## 10. Left to the builder

Scraping details for the IR pages, the exact Coinbase product list for COIN's volume (fixed in the prereg before
fitting), how to treat MARA Pool blocks if pool attribution changes, and the test layout.
