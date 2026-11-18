# KP1: KPI binaries on numbers that leak before the report — frozen pre-registration

Date: 2026-11-17. Thread "Test satellite edge on KPI binaries". Evan approved the design on 2026-11-16
(`docs/superpowers/specs/2026-11-16-kpi-binaries-public-data-design.md`) and the Stage 1 plan on 2026-11-17
(`docs/superpowers/plans/2026-11-16-kp1-stage1-nowcast.md`).

**This file is committed before any reported KPI value, Robinhood monthly figure, MARA Pool block count or Coinbase
volume for KP1 has been typed or fetched.** The only data touched so far are a one-page check that each source
answers (2026-11-16: one mempool.space page of MARA Pool blocks near height 800,000, one Coinbase BTC-USD candle
window from January 2021, and Robinhood's July 2026 monthly release and Q3 2026 earnings release, read to learn
which metrics exist). Nothing below changes after a result is seen; a later change is a new, separately labelled
variant. KP1 is research and paper observation only: no orders, no book, no code in `agentdesk/`.

## 1. Why

Cboe KPI binary options pay $1 if a company's reported KPI is at or above the strike (call) or below it (put),
settled on the earnings press release figure. If public data published before the report pins a KPI tightly, a
binary priced far from that number is mispriced. Stage 1 (this file's sections 3-5) measures how tightly public data
pins seven KPIs, out of sample, against a naive baseline. Stage 2 (section 6) scores live binary prices after launch.

## 2. The KPIs

| Contract | KPI | Cboe scale | Settles on |
| --- | --- | --- | --- |
| KCHOD | HOOD Funded Customers | 1e6 | earnings release, highest printed precision |
| KBHOD | HOOD Revenue | 1e9 | same |
| KDHOD | HOOD ARPU ($/user, annualized) | 1 | same |
| KCMAR | MARA Total Blocks Won | 1 | same |
| KFMAR | MARA Bitcoins Mined | 1e3 | same |
| KBMAR | MARA Revenue | 1e6 | same |
| KCCOI | COIN Transaction Revenue | 1e6 | same |

All three companies report after the close. Strike intervals on the scaled value: 0.01 below 10, 0.10 below 100,
1.00 below 1,000, 10 below 10,000, 100 otherwise.

Not in KP1: HOOD Gold subscribers and EPS, MARA hashrate and bitcoin holdings, COIN EPS and subscription revenue.

## 3. Information rule and samples

- A quarter's nowcast uses only data published before its report date. A Robinhood monthly release counts if its
  dateline is earlier than the report date. On-chain blocks and exchange volumes inside the quarter all predate the
  report (each report comes weeks after quarter end).
- Model parameters for quarter q are fit only on quarters before q.
- Quarter boundaries for MARA Pool blocks and Coinbase candles: UTC calendar quarters, [start, end).
- Samples: HOOD from the first quarter whose report had at least two monthly releases for that quarter published
  before it, through 2026Q3. MARA 2021Q3 through 2026Q3. COIN 2021Q2 through 2026Q3. A KPI-quarter is dropped (and
  listed in the report) if the company did not print that KPI.
- Reported values are typed from the press release (Robinhood via its IR site or the mondovisione.com mirror; MARA via
  ir.mara.com; Coinbase via its shareholder letter or release), one row each with the printed text and source URL.

## 4. Nowcasts and naive baselines

**HOOD funded customers.** Model: last published month-end count + that month's change from the month before it
(the prior month may be the previous quarter's month 2 or, for month 1, the previous quarter's reported quarter-end
count). Naive: prior quarter's reported count + prior quarter's change.

**HOOD revenue.** Lines: options, equities, crypto, event contracts, other transaction, net interest, other revenues.
- Drivers per month: options contracts, equity notional, crypto notional, event contracts.
- A quarter's driver total = mean daily volume over its published months x the quarter's days, where days are NYSE
  trading days for options and equities and calendar days for crypto and event contracts. The prior quarter's driver
  total is estimated the same way from the months that were published before its own report, so rates are
  consistent.
- Transaction line nowcast = this quarter's driver total x (prior quarter's reported line / prior quarter's driver
  total). Other transaction revenue and other revenues carry at the prior quarter's reported level.
- Net interest = prior quarter's net interest x (mean of published month-end margin book + cash sweep this quarter /
  the same mean for the prior quarter).
- A line absent or zero in the prior quarter contributes 0.
- Naive: prior quarter revenue x (1 + prior quarter growth).

**HOOD ARPU** = revenue x 4 / mean(prior quarter's reported funded customers, this quarter's funded customers), using
the model nowcasts for the model and the naive nowcasts for the naive.

**MARA blocks won.** On-chain = count of MARA Pool blocks (mempool.space pool attribution) with timestamps inside the
quarter. Model = on-chain x mean(reported / on-chain) over all prior quarters. Naive = (MARA Pool blocks / all blocks,
prior quarter) x all blocks this quarter, where all blocks = height at quarter end − height at quarter start
(mempool.space blocks/timestamp).

**MARA bitcoins mined.** On-chain = sum of rewards (subsidy + fees) of those blocks. Model = on-chain x mean(reported /
on-chain) over prior quarters. Naive = naive blocks x the mean reward of MARA Pool's own blocks this quarter (a proxy
for the network's average reward per block).

**MARA revenue.** Model = model bitcoins mined x the quarter's volume-weighted average BTC-USD close (Coinbase
Exchange daily candles) + other revenue carried from the prior quarter, where other revenue = prior reported revenue
− prior reported bitcoins mined x prior quarter VWAP. Naive = naive bitcoins mined x the same VWAP + the same other
revenue.

**COIN transaction revenue.** Volume = sum of daily USD volume (volume x close) over BTC-USD, ETH-USD, SOL-USD,
XRP-USD, DOGE-USD, LTC-USD, ADA-USD, LINK-USD, AVAX-USD and USDT-USD on Coinbase Exchange; a product with no candle on
a day adds 0. Model = volume x mean of the last 4 prior quarters' (transaction revenue / volume), fewer if fewer
exist. Naive = prior quarter x (1 + prior quarter growth).

## 5. Stage 1 scoring and gate

- Error e = ln(actual / nowcast), computed for every quarter with a nowcast.
- A quarter is scored once at least 4 earlier errors exist for that KPI and method.
- Predictive P(KPI >= K) = 1 − T4((ln(K / nowcast) − mean(e)) / sd(e)), with mean and sd (ddof 1, floored at 1e-4) over
  that KPI's earlier errors only. T4 is the Student-t CDF with 4 degrees of freedom.
- Synthetic strikes: centre = nowcast rounded to the nearest strike interval; strikes at centre + k x interval for
  k = −3..3. Brier = mean over scored quarters and the 7 strikes of (p − 1[actual >= K])². The naive baseline is
  scored the same way with its own errors.
- Reported per KPI: scored quarters, median |e| for model and naive (and in strike intervals), share of scored
  quarters whose actual falls inside the model's 80% interval (10th-90th percentile), Brier model and naive.
- **Gate (per KPI):** model median |e| <= 0.5 x naive median |e| **and** 80% coverage between 60% and 95%. A KPI that
  passes is paper-traded in Stage 2; one that fails is only recorded, except COIN transaction revenue, which is
  always paper-traded as the low-information comparison.

Stage 1 proves no edge; it only says whether public data knows more than a trend line.

## 6. Stage 2 (after launch; frozen now, built later)

- Quotes: every listed strike for the seven contracts, bid/ask/sizes/last/volume, daily at 14:30 CT from listing,
  hourly on the last 3 trading days, final snapshot 14:50 CT on the last trading day. Source: Robinhood MCP read-only
  quote tools, else Cboe's public delayed quotes. Never an order tool.
- Only trades on snapshots from a live source (Robinhood MCP) count; delayed-quote trades are logged and scored but
  never count toward the pass bar.
- Rule, final snapshot only: P = the section 5 probability refreshed with all data public by then. Buy the call at its
  ask if P − ask >= 0.10; buy the put at its ask if (1 − P) − put ask >= 0.10. At most one side per strike. 10
  contracts. Only two-sided quotes with size >= 10.
- Fees: $0 through 2026-12-31; from 2027 $0.02 per contract until Robinhood publishes its fee.
- Settlement: the press release figure, checked against Cboe's KPI Settlement Values page.
- Cluster = one company report. **Pass bar:** HOOD and MARA only; at least 12 settled company reports with a trade;
  mean P&L per contract > 0 with t >= 2.33 clustered by company report; positive in both halves of the reports; model
  Brier below the market mid's. COIN is scored separately and shown next to them.
- Kill: after 2 full seasons, stop paper trading if the market mid's Brier is at or below the model's on every KPI.
  End: after 8 seasons without the bar, report "inconclusive" and stop.
