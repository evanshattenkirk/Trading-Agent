# F2-C premise on free data: the stock's drift after the breakout (pre-registration, 2026-09-29)

Written and committed before any result. F2-C (call debit spreads on an opening-range breakout,
`research/strategy_f2_prereg.md`) is already on paper by Evan's decision; this study doesn't gate it. It answers,
on the shares data already cached, the question every option structure depends on.

## Question

A call debit spread is still a long bet on the stock. For a same-day hold it earns roughly its net delta times the
stock's move, minus option costs far above the 2 bp per side the shares tests use. So: after F2-C's entry, does the
stock drift up? F's replication (large caps, tight 0.10 × ATR stop) lost 9.8 bp per trade with 92% of trades
stopped out, so a drift with no stop, or F2-C's looser opening-range-low stop, is the thing to measure.

## Variants

Both use F's scan inputs (the section 3 universe and RVOL5 window from `research/strategy_f_intraday.py`), restricted
the way F2-C arms: F2's 34 names only, green first candle, RVOL5 ≥ 2, the top 3 by RVOL5. Entry: buy-stop at the
opening-range high, limit × 1.0005, at most 3 sends, until 10:30 ET; at most 5 open; floor($1,000 ÷ limit) shares.
Fills use the replication's bar rules with 2 bp slippage per side.

- **C-hold (primary):** no price stop; exit at the 15:55 ET bar's open (12:55 on half-days).
- **C-orlow:** the same, plus F2-C's first-day stop: a bar at or below the opening-range low exits there.

**Sensitivity (reported, never selected from):** all section 3 picks (top 5, any name), and 5 bp slippage.

## Metrics

Trades, win rate, mean and median bp per trade, profit factor, t clustered by day, annualized return on $10,000;
split 2016–2020 / 2021–2026 and by year. Descriptive: the maximum favorable and adverse excursion after entry, in bp
and ATR14, with the share of trades reaching +1%, +2%, +3%, +0.5 ATR and +1.0 ATR (for strike placement in a
later, separately pre-registered spread design).

## Reading

- **Pass:** PF ≥ 1.1, t > 2, and a positive mean in both halves (the replication's bar).
- **C-hold mean ≤ 0 bp:** the stock doesn't drift up after the entry, so call spreads on it are expected to lose on
  paper too. Reported to Evan, who decides whether F2-C keeps running.

## Limits

- The cache holds each candidate's day only, so this measures the same-day drift; days 2–3 of F2-C's hold need the
  option replay (`research/f2_real_quotes.py`).
- Survivorship bias (today's S&P 500 list), 1-minute-bar fills, no option prices.

## Run

On the Mac, after `research/strategy_f_intraday.py` has cached its data (no network):

    python research/f2c_drift.py

Outputs: `research/f2c_drift_results.json`, `research/f2c_drift.md`
