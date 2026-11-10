# R1: retailer search interest before earnings — frozen pre-registration

Date: 2026-11-10, thread "Explore SkyFi as a quant data source". Evan asked for a free test of the idea behind
satellite parking-lot counts ("write up the free test plan and execute", 2026-11-10 15:23Z). This file is committed
**before any Google Trends series, earnings date or price for R1 has been downloaded or looked at** in this repo (the
only data touched so far: one 2017 Trends pull for "Walmart" to check the API answers, and topic-id lookups, which
carry no returns). Nothing below changes after a result is seen; a change made later is a new, separately labelled
variant. R1 is research only: no book, no paper trades, no code in `agentdesk/`.

## 1. Why

Satellite car counts at US retailers predicted earnings-day returns in 2011-2017: top-minus-bottom quartile of
fill-rate growth earned about 4.7% over the 3-day window (*On the Capital Market Consequences of Big Data: Evidence
from Outer Space*, JFQA). The channel is consumer demand that the market has not fully priced before the report.
Google search interest measures the same demand for free: in 2004-2008 one standard deviation of product search
growth predicted revenue surprises and +27 bp over the 3-day announcement window (Da, Engelberg & Gao, *In Search of
Fundamentals*, 2011). Buying imagery to test the satellite version would cost roughly $0.6M-$3M (SkyFi doc,
2026-11-10). R1 asks the cheap question first: **does a demand signal still move US retailers' report-day returns
in 2016-2026?** If it does not, imagery is a poor bet; if it does, R1 is a candidate strategy itself.

## 2. Universe (fixed now)

19 US store-based retailers where shoppers come to the store, each with listed options. The Google Trends entity is
the company's Knowledge Graph id (looked up 2026-11-10 by name; no data was pulled for the lookup):

| Symbol | Trends entity | Symbol | Trends entity |
| --- | --- | --- | --- |
| WMT | /m/0841v Walmart | KSS | /m/037x4r Kohl's |
| COST | /m/029f32 Costco Wholesale | M | /m/01pkxd Macy's, Inc. |
| HD | /m/01zj1t Home Depot | ULTA | /m/03p3r21 Ulta Beauty |
| LOW | /m/037922 Lowe's | TSCO | /m/0378_f Tractor Supply Company |
| TGT | /m/01b39j Target Corporation | DKS | /m/06fgv_ Dick's Sporting Goods |
| BBY | /m/01zrdx Best Buy | BJ | /m/0797b5 BJ's Wholesale Club |
| DG | /m/0204_2 Dollar General | BURL | /m/03p5mm Burlington Stores |
| DLTR | /m/040rs1 Dollar Tree | AZO | /m/02z6wl AutoZone |
| TJX | /m/097b3s T.J. Maxx (its largest banner) | ORLY | /m/0h7_x_ O'Reilly Auto Parts |
| ROST | /m/08950y Ross Stores | | |

Survivorship: these are names listed today (Nordstrom, Bed Bath & Beyond and others that left are not in it). A
sign trade on a timing signal is little affected; the report says so.

## 3. Data

- **Report times:** SEC EDGAR Form 8-K filings with Item 2.02, exactly as `research/fetch_earnings_edgar.py` reads
  them (acceptance time in Eastern time, AAPL/KO anchor check, 45-day clustering keeps the last filing, report session
  R = first session opening after acceptance, during-market filings take R = the filing day per X1 amendment 1, entry
  session = the session before R), from 2014-01-01. SEC isn't reachable from cloud sessions, so
  `research/fetch_retail_mac.py` runs on Evan's Mac and writes `research/data/x1/retail_earnings.csv`.
- **Prices:** Alpaca daily bars, SIP feed, split-adjusted, 2015-12-01 to 2026-11-09, for the 19 names and SPY (same
  Mac script, `research/data/x1/retail_daily.csv`). Dividends are ignored (one-day holds).
- **Search interest:** Google Trends, United States, all categories, monthly, 2004-01-01 to 2026-10-31, one request
  per entity (so each series has one scale), pulled once by `research/fetch_retail_trends.py` and saved to
  `research/data/retail/trends.csv`. Trends values are sampled and rescaled by Google; R1 uses ratios within one
  series, and the saved pull is the data of record.

## 4. The rule

For each kept report of a name with report session R:

1. M = the calendar month containing R − 35 days (that month always ends before R, so its value was public).
2. cur = mean search value over months M−2, M−1, M; base = the same three months one year earlier.
   g = ln(cur / base). Skip the report if cur or base is 0.
3. Surprise s = g − mean(g over the same name's 4 previous kept reports). Skip if fewer than 4 previous reports
   have a g.
4. Position: long the stock if s > 0, short if s < 0 (s = 0: skip). Enter at the close of the entry session, exit at
   the close of session R.
5. Trade return = position sign × (stock return − SPY return) over that close-to-close window, minus **5 bp** cost
   (round trip, both sides, for large-cap shares).

**Sample:** reports with R from 2016-01-15 through 2026-10-31 (the first R needs a prior close in the price file).

## 5. Pass rule (one test, decided before results)

- Mean net trade return > 0 with **t ≥ 2.33** (one-sided 1%, the bar the earlier rounds used for a single test),
  standard errors clustered by the ISO week of R (several retailers report the same week).
- **And** both halves positive, split at the median R date.
- Otherwise R1 **fails**, and the satellite/consumer-demand line for retail earnings is closed.

## 6. Reported, not part of the pass rule

- Gross (0 bp) mean and t; long-only and short-only means; per-name table; per-year means.
- Pooled OLS slope of the abnormal return on s (z-scored across the sample), clustered by week.
- The 3-day window (entry close to the close of the session after R), as in Da, Engelberg & Gao.
- Counts: reports found, skipped (no prices, zero search values, fewer than 4 prior reports), traded.

## 7. If R1 passes

A second pre-registration tests the options vehicle (a single ATM call or put bought at the entry close, sold at the
exit close, on ThetaData quotes at mid −1¢ and taker), then a forward paper log. Neither starts without Evan's OK.
