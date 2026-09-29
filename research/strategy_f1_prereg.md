# Book F1 v2 (stocks in play, long shares): pre-registration (2026-09-29)

Written before any F1 v2 result exists. It goes to paper right away (Evan, 2026-09-29) and is backtested once the
data in section 5 is bought. The rules below don't change after results are seen; any new idea is a new variant
with its own pre-registration.

## 1. Why F changes

Book F (now F1) replicated a published opening-range breakout (Zarattini, Barbon & Aziz 2024: the top 20 US stocks by
first-5-minute relative volume, longs and shorts, stop at 10% of ATR14, exit at the close; Sharpe 2.8 over 2016–2023)
but kept only about 150 large caps, longs only and the top 5. Over 2016-01-04 to 2026-09-25 it failed: 5,020 trades,
PF 0.66, t −10.5, every year negative (`research/strategy_f_intraday.md`).

Two differences from the paper stand out, and F1 v2 changes exactly those:
- **The universe.** The paper's edge came from all US stocks, including mid and small caps. F1 now scans every
  liquid US common stock.
- **The stop.** At 0.10 × ATR14, 92% of F's trades stopped out, and a looser stop improved every result
  (0.05 → PF 0.31, 0.10 → 0.66, 0.20 → 0.88). That sensitivity was seen on the full sample, so the stop choice here is
  partly informed by it; it follows the classic opening-range rule (stop at the range's low) rather than the best
  row, and the published 0.10 × ATR stop is logged beside every trade as a shadow.

Shorting stock isn't possible at Robinhood, so the bearish side lives in book F2 (put debit spreads).

## 2. Rules

| Item | Rule |
|---|---|
| Universe | Every listed US common stock and ADR (NASDAQ Trader symbol directory; ETFs, test issues, warrants, rights, units and preferreds dropped) with 20+ daily bars, prior close ≥ $10, ATR14 ≥ $0.50, 14-day average volume ≥ 1M shares, 20-day average dollar volume ≥ $25M; plus the 18 AI/memory names when they pass the same floors |
| Universe cap | With Robinhood minute bars (`bars_source: robinhood`), the top 400 by 20-day dollar volume, so the 09:35 scan fits the call budget. With Alpaca SIP bars (`alpaca_sip`), no cap |
| RVOL5 | 09:30–09:35 ET volume ÷ the mean of the same window over the prior 14 sessions, from one source only |
| Scan (09:35:05 ET) | Green first candle, RVOL5 ≥ 2.0, opening range ≤ 0.50 × ATR14 (wider is skipped as overextended). The top 10 by RVOL5 are armed; the macro-event skip is unchanged |
| Entry | Buy-stop at the OR high, limit × 1.0005, at most 3 sends, until 10:30 ET; at most 5 open; one entry per name |
| Stop | The OR low, clamped so it sits 0.10–0.50 × ATR14 below the fill |
| Exit | 15:55 ET (12:55 on half-days) or the stop. Never held overnight |
| Size and limits | Unchanged: shares = floor(min($25 ÷ risk per share, $1,000 ÷ entry)); 5 open; a −$75 day halts F1 |

**Logged, never traded:**
- the published-rule shadow: each trade's P&L had its stop been fill − 0.10 × ATR14;
- would-be shorts on red first candles (unchanged).

**Observe-only tags on every F1 trade** (never gate or size; they exist so a later, separately pre-registered filter
can be tested out of sample): gap % at the open, opening range ÷ ATR14, RVOL5, minutes after the scan, SPY versus its
VWAP in bp, and the news tag's catalyst.

## 3. What counts as working

The same bar as the replication, on paper and on the backtest separately: PF ≥ 1.1, t > 2 (clustered by day), and a
positive mean in both halves of whatever period is tested. On paper: at least 20 sessions and 100 trades first
(HANDOFF 10.8).

## 4. Known limits

- The broad universe uses today's symbol list for every past year (survivorship bias, larger than the S&P version).
- The 400-name cap favors the most traded names; it ends when SIP bars are in use.
- Robinhood and Alpaca volumes can differ; each run uses one source for the whole RVOL5 ratio and never mixes them.

## 5. Backtest (after the data purchase)

`research/strategy_f_intraday.py --universe broad` replays these rules on Alpaca SIP 1-minute bars, 2016-01-04 to
2026-09-25, and writes `research/strategy_f1_results.json` and `research/strategy_f1.md`. The original replication
(`research/strategy_f_intraday.md`) stays as the record of the section 3 rules.
