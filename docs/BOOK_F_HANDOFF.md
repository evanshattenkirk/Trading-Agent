# Book F handoff: large-cap "stocks in play" (for Claude Code)

**Task:** fold strategy F into AgentDesk as a paper-only book, and run its replication backtest.
**From:** the claude.ai research session, 2026-09-27. **Owner:** Evan.
**Read first:** `CLAUDE.md`, then `HANDOFF.md` sections 2, 5, 7F, 9, 10 and 12. This file is the build brief for F only. Where they conflict, HANDOFF.md's safety rules win.

---

## 1. What F is and why

**Evan's idea:** buy big-name, high-volume stocks (S&P 500, AI/memory names) when volume is far above average and there's news. Jump in when the news doesn't look priced in yet, or ride the momentum. Evan asked Claude to choose the entry, exit and sizing rules. The rules in section 3 are those choices, and they are frozen.

**Evidence so far** (`research/strategy_f.py`; results in `research/strategy_f_results.json`):
- **Data:** daily bars for 505 S&P 500 stocks, 2013–2018. Returns measured against the equal-weight universe, 10 bp round trip, Newey-West t.
- **Ride the momentum** (volume 2–3× its 20-day average, up 3–5%, close near the high → buy the next open, hold 1–20 days): −0.27% to +0.25% per trade. The best t is 1.31 against a Bonferroni bar of 2.96. **No edge.** So F2 (the multi-day hold) is disabled.
- **The same move on normal volume** reverses −18 to −31 bp the next day (t −4.5). Volume separates news from noise, but there's no follow-through to buy at the next open.
- **Chasing gap-ups at the open, sold at the close:** −29 bp per trade (t −8.2) for gaps of 2% or more; −44 bp (t −4.6) for 4% or more. It loses reliably.
- **The version with published support is intraday:** Zarattini, Barbon & Aziz (2024), SSRN 4729284.
  - Rules: top 20 stocks by first-5-minute relative volume; trade the break of the 5-minute opening range in the first candle's direction; stop at 10% of ATR14; exit at the close.
  - Reported (2016–2023): 41.6%/yr, Sharpe 2.81.
  - Caveats: up to 4× leverage, shorts and small caps included, and no clean out-of-sample period.
  - QuantConnect's 2016 replication: Sharpe 2.4, with warnings that it's cost-sensitive.
- **F adapts that paper to Evan's account:** large caps plus his AI/memory list, long-only, shares, top 5, no leverage.

**Account facts that shape the build:**
- **The PDT rule is gone.** FINRA Notice 26-10 took effect June 4, 2026, and Robinhood adopted it, so intraday round trips aren't counted.
- **No stock shorting** at Robinhood.
- **Fractional shares** only go through market orders in regular hours, so F uses **whole shares with marketable limit orders**.
- Agentic account ••••6452 (show only the last 4 digits anywhere user-facing).

---

## 2. Order of work

1. **Replication backtest** (section 5). It's research only, with no engine changes, and it goes first because its result decides how much weight F's paper results get.
2. **The multi-book framework**, if it isn't built yet (HANDOFF section 9, phase 4). F plugs into its `Book` / `Strategy` interface. Don't bolt F onto book A's engine path.
3. **The equity path:** broker, paper broker, data, and the scan (section 4).
4. **The F strategy module**, with tests first (section 3 rules, section 6 tests).
5. **Crew news tag, dashboard, journal, `f-report`.**
6. **One sim day and one paper session**, then report back (section 8).

Use test-driven development for steps 3–5. Run `python -m pytest -q tests` after every step; the existing 25 tests must stay green.

---

## 3. F1 rules (frozen; don't tune)

All times are US/Eastern in the logic (the engine clock is CT, so convert once). Half-days: the scan and entries are unchanged, and the exit moves to 12:55 ET.

| Item | Rule |
|---|---|
| Universe (rebuilt pre-market, 07:30 CT) | Top 130 S&P 500 names by 20-day average dollar volume, **plus** NVDA, AMD, AVGO, MU, TSM, ARM, MRVL, SMCI, WDC, STX, MSFT, META, GOOGL, AMZN, AAPL, ORCL, PLTR, TSLA. Keep names with prior close ≥ $10, ATR14 ≥ $0.50 and 20-day dollar volume ≥ $100M |
| ATR14 | Wilder ATR on daily bars, computed through yesterday's close |
| Opening range (OR) | The single 09:30:00–09:34:59 1-minute-aggregated 5-minute bar: OR high, OR low, first-candle open and close, 5-minute volume |
| RVOL5 | Today's 09:30–09:35 volume ÷ the mean of the same 5-minute volume over the prior 14 sessions. **One consistent source:** Robinhood `get_equity_historicals` 1-minute bars, both live and in the 14-day cache. Never mix IEX-only volume into the ratio |
| Scan (09:35:05 ET) | Keep names with RVOL5 ≥ 2.0 and a green first candle (close > open). Rank by RVOL5 and take the **top 5**. Skip the whole scan if a high-impact macro event (crew calendar) falls between 09:35 and 10:30 ET |
| Entry | Buy-stop at the OR high. It triggers on the first trade/1-minute high above it; send a marketable limit at OR high × 1.0005, `gfd`. Valid until **10:30 ET**, then cancel. At most one entry per name per day |
| Stop | Entry fill − 0.10 × ATR14, held in the engine; exit with a marketable limit sell. No profit target, no trailing, no breakeven move (paper rule as published) |
| Exit | 15:55 ET, or the stop, or the kill switch / a book halt. **Never held overnight.** The startup reconciliation must flag any F shares found at the open |
| Size | shares = floor(min(risk_per_trade ÷ (entry − stop), max_notional ÷ entry)). Skip if shares < 1 |
| Limits | $25 risk per trade, $1,000 max notional per name, at most 5 open, book daily loss −$75 halts F for the day. The account-level open-risk cap (HANDOFF section 9) still applies |
| Red first candle | Log a would-be short (the OR-low break, same stop and exit math) to `f_shadow_shorts`. **Never place an order** |
| News tag | Observe-only (section 4.5). It never filters or sizes |

`config.yaml` already has this block. Treat it as the source of truth, and add fields only if needed:

```yaml
F_stocks_in_play: {enabled: true,  paper_only: true,  instrument: shares, scan_et: "09:35", rvol5_min: 2.0, top_n: 5, first_candle: green, entry_cutoff_et: "10:30",
                   stop_atr_frac: 0.10, exit_et: "15:55", risk_per_trade: 25, max_notional: 1000, max_positions: 5, daily_loss: 75,
                   universe: {min_price: 10, min_atr: 0.50, min_dollar_vol_20d: 100000000, top_sp500_by_dollar_vol: 130,
                              extra: [NVDA, AMD, AVGO, MU, TSM, ARM, MRVL, SMCI, WDC, STX, MSFT, META, GOOGL, AMZN, AAPL, ORCL, PLTR, TSLA]},
                   news_tag: observe, shorts: log_only}
F2_momentum_hold: {enabled: false, paper_only: true, note: "..."}
```

The S&P 500 constituent list needs a source. Use `datasets/s-and-p-500-companies` (`data/constituents.csv` on GitHub), cached weekly under `~/.agentdesk/cache/`.

---

## 4. Build spec

### 4.1 Equity broker path (`agentdesk/brokers/`)
- **`RobinhoodEquityBroker`:**
  - `review_equity_order` first, always. Then, in live mode only, `place_equity_order`.
  - Orders are `type: limit`, `time_in_force: gfd`, `market_hours: regular_hours`, whole-share `quantity` as a string, and a `ref_id` UUID if the schema has one.
  - Poll status; cancel on timeout (`cancel_equity_order`); reprice at most 2 times.
  - Refuse to run in live mode unless `live_enabled: true` and `robinhood.account_number` is set by Evan.
  - Call `get_equity_tradability` once per day for the universe and drop anything not tradable in regular hours.
  - Verify argument shapes against the live schemas with `rh-inspect`, and add an equity section there: tradability for 3 names plus a `review_equity_order` for 1 share of SPY. Reuse `fit_args`.
- **`PaperEquityBroker`:**
  - Buy fills at max(trigger price, current ask) plus 1 bp; sells fill at min(stop, current bid) minus 1 bp.
  - If a 1-minute bar gaps through the stop, fill at that bar's open.
  - If entry and stop fall in the same 1-minute bar, assume the stop hit.
  - Reject stale quotes (older than 5 s) and fail closed.
- **Positions:** extend the startup reconciliation with `get_equity_positions`. Any unexpected equity position in the agentic account halts all books until Evan clears it.

### 4.2 Data (`agentdesk/feeds/`)
- **Pre-market cache (07:30 CT):**
  - Daily bars (ATR14, 20-day dollar volume) and the prior 14 sessions' 09:30–09:35 1-minute volumes for the universe.
  - Source: Robinhood `get_equity_historicals`, up to 10 symbols per call. Store under `~/.agentdesk/cache/f/`.
- **Scan at 09:35:05 ET:** about 15 calls of 10 symbols for today's 09:30–09:34 1-minute bars. Retry a failed call once; if a symbol has no data, drop it (never guess).
- **After the scan:** subscribe the 5 picks on the Alpaca IEX stream (inside the 30-symbol limit, alongside SPY), and poll Robinhood equity quotes for bid/ask on picks and open positions every 1 s.
- **Rate discipline:** no more than 20 Robinhood calls per second in bursts. Log call counts per minute.

### 4.3 Strategy module (`agentdesk/books/f_stocks_in_play.py`)
- Implements the section 9 `Strategy` interface: `on_clock(now)` drives the pre-market cache, the scan, the entry cutoff and the exit; `on_bar` / `on_quote` drive triggers and stops. It emits `OrderIntent` / `ExitIntent`.
- **Pure functions, unit-tested:**
  - `atr14(daily_bars)`
  - `rvol5(today_vol, hist_vols)`
  - `rank_candidates(rows, cfg)`
  - `shares_for(entry, stop, cfg)`
  - `should_exit(now, pos, quote, cfg)`
- Its own `RiskManager` instance with F's limits. It shares the global kill switch.

### 4.4 Journal (`agentdesk/journal.py`)
- `trades.book` column (the shared multi-book change).
- New `f_scans` table: date, symbol, rvol5, first-candle direction, OR high/low, ATR14, rank, picked, news tag, and the reason when a name wasn't picked.
- New `f_shadow_shorts` table: the would-be short trades with their hypothetical P&L.
- Record R multiple (P&L ÷ initial risk) on every F trade.

### 4.5 Crew: news tag (`agentdesk/crew.py`)
- **At the 08:35 CT scan:** Tape and Macro get the 5 picks plus up to 5 runners-up. With web search, they return per symbol: `{symbol, catalyst: earnings|guidance|analyst|m&a|sector_macro|none_found, priced_in: early|partly|fully, confidence: 0..1, note: "≤ 20 words", sources: [urls]}`.
- **Offline fallback:** `catalyst: unknown, priced_in: unknown`.
- **Timing:** the tag must land within 60 s. A late tag is logged late and never delays an order.
- **Observe-only:** stored on the scan row and the trade. It never gates or sizes anything.
- **Proposals:** the crew may *propose* promoting the tag to a filter as a `standing` change, which waits for Evan's Approve. Add no F keys to the `TWEAKS` whitelist in `proposals.py`: F's size and loss fields must stay untouchable. Add a test that proves it.
- **Office:** when F has picks, the Tape desk's monitor shows the 5 tickers. Keep the existing huddle behavior.

### 4.6 Dashboard (`agentdesk/web/`) and CLI
- **Book switcher entry for F**, with a scan table: symbol, RVOL5, first candle, OR high, ATR14, tag, and status (armed / filled / stopped / closed / expired). Show per-position R and book P&L on the strip.
- **`python -m agentdesk f-report [--since YYYY-MM-DD]`:** trades, win rate, mean R, PF, t (clustered by day), worst day, and a split by news tag, catalyst, RVOL5 bucket and AI list vs the rest, plus the shadow-shorts summary.

---

## 5. Replication backtest (do this first): `research/strategy_f_intraday.py`

**Pre-registered.** The spec is section 3, run on history. Don't change the primary spec after seeing results. Sensitivity runs are reported, never selected from.

- **Data:** Alpaca historical bars API (`/v2/stocks/bars`), `feed=sip`, `adjustment=split`.
  - 1-minute bars 09:30–16:00 ET plus daily bars, **2016-01-04 → 2026-09-25**. The free plan serves SIP history older than 15 minutes.
  - Cache as parquet under `research/data/f_intraday/` (gitignored). Expect a multi-hour pull; make it resumable.
- **Universe, point-in-time:**
  - Each day, use only names that traded that day and pass the filters on prior data.
  - Constituent history isn't free, so use today's S&P 500 list plus the extras, and **state the survivorship bias** in the report.
  - Names that listed later (ARM 2023, PLTR 2020) enter only once they have 20 days of history.
- **Fills:** as in the `PaperEquityBroker` rules; 2 bp slippage per side; no commission.
- **Report** (write `research/strategy_f_intraday_results.json` plus a short `.md`):
  - trades, win rate, mean R, mean bp, PF, and t clustered by day
  - annualized return and Sharpe of the book at the section 3 sizing, with an equity curve PNG
  - results by year, 2016–2020 vs 2021–2026, and the AI/memory list for 2023–2026 separately
  - worst day and worst month
- **Sensitivity (report only):** RVOL5 ∈ {1.5, 2, 3}; top N ∈ {3, 5, 10}; stop ∈ {0.05, 0.10, 0.20} × ATR; slippage 2 bp vs 5 bp per side.
- **Also:** download Alpaca daily bars for the same universe, 2016–2026, into per-symbol CSVs (`date,open,high,low,close,volume`). Rerun `python research/strategy_f.py <folder> research/strategy_f_results_2016_2026.json`. It checks whether the daily findings (no multi-day drift, losing gap chase) still hold, including in the AI/memory era.
- **Pass bar** (primary spec, after 2 bp costs): PF ≥ 1.1, t > 2, **and** positive in both 2016–2020 and 2021–2026.
  - **Pass:** F runs in paper as a candidate for promotion.
  - **Fail:** F still runs in paper, but only as a logging experiment, and the report says so plainly. Don't tune it to pass.

---

## 6. Required tests (`tests/test_book_f.py`)
- **RVOL5:** math and ranking; ties broken by dollar volume; a red first candle is excluded from buys and goes to shadow shorts.
- **Entry:** it triggers only above the OR high and only before 10:30 ET; there's one entry per name per day; it's cancelled at the cutoff.
- **Sizing:** `shares_for` respects both the risk and notional caps and skips at < 1 share.
- **Stop:** the stop is at 10% of ATR14; a gap-through fills at the bar open; with entry and stop in the same bar, the stop hits.
- **Exit:** at 15:55 ET, and at 12:55 ET on half-days. Nothing is held overnight: a simulated restart after close with a position open halts the book.
- **Limits:** the daily loss −$75 halts F only; the global kill switch flattens F.
- **Market events:** a macro event in 09:35–10:30 ET skips the scan.
- **News tag:** it never changes an order, even when set to "fully priced in."
- **Proposals:** they can't modify `books.F_stocks_in_play.*` risk fields, and F2 stays disabled.
- **Live safety:** the equity broker refuses live mode without `live_enabled` and an account number, and paper mode never calls `place_equity_order`.
- **Data errors:** stale or missing quotes fail closed.

---

## 7. Safety rules (repeat of HANDOFF section 12, plus F)
- Never call `place_equity_order`, `cancel_equity_order`, `place_option_order`, `cancel_option_order` or `exercise_option` yourself. Orders go only through the engine, in live mode, with `live_enabled: true` and an Evan-set account number. Keep MCP permission prompts on for those tools.
- Paper is the default and must survive restarts. F is `paper_only: true`, and only Evan changes that.
- No shorting, no margin borrowing, no overnight F positions.
- Don't change the section 3 rules. Any new idea (a tag filter, a trailing stop, more names) is a new, separately pre-registered variant.

---

## 8. Done means, and what to report back to Evan

**Done:**
- Replication report written (pass or fail, stated in the first line).
- `tests/test_book_f.py` green, plus all existing tests.
- A sim day runs A–F with no cross-contamination.
- `rh-inspect` shows equity tradability plus a `review_equity_order` with no alerts.
- One live-market paper session completes with the scan, tags, triggers and exits journaled, and no errors in the log.

**Report back, briefly, as numbers:**
- the replication verdict: PF, t, trades, both halves, and the AI-list slice
- the 2016–2026 rerun of the daily test
- day 1 paper: picks, fills, R per trade, and tag coverage
- anything that blocked the build, such as Robinhood schema differences or rate limits

---

## Sources
- Zarattini, Barbon & Aziz, *A Profitable Day Trading Strategy for the U.S. Equity Market*: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284
- QuantConnect replication: https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/
- ORB research review: https://danfin.net/opening-range-breakout-research
- Chan (2003), "Stock price reaction to news and no-news: drift and reversal after headlines," *JFE* 70(2)
- Martineau (2022), "Rest in Peace Post-Earnings Announcement Drift," *Critical Finance Review*
- FINRA Regulatory Notice 26-10: https://www.finra.org/rules-guidance/notices/26-10
- Robinhood, Day trading: https://robinhood.com/us/en/support/articles/day-trading/
- Alpaca market data: https://docs.alpaca.markets/us/docs/about-market-data-api
- 2013–18 test data: https://github.com/CNuge/kaggle-code (`stock_data/individual_stocks_5yr.zip`)
