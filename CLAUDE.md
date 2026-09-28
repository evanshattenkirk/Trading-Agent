# AgentDesk — context for Claude Code

Evan's automated options trading desk on Robinhood Agentic Trading (MCP), built in a claude.ai session on 2026-09-27. **Read HANDOFF.md (v3) first.** It has the full rules for all five strategy books, the backtest tables, the multi-book build spec (section 9), the phased plan with acceptance criteria (section 10), sizing (11) and safety rules (12). This file is the short version.

## What exists
- `agentdesk/` Python engine (deterministic rules) + FastAPI/websocket dashboard with a pixel-art office (`web/`). Modes: `sim` (synthetic day, calm/event moods), `paper` (default: real data, simulated fills), `shadow` (every order sent to `review_option_order`, fills simulated), `live` (needs `live_enabled: true` + `robinhood.account_number`).
- CLI: `python -m agentdesk run | record-demo | rh-inspect | backtest | l2-report`.
- Book A (Evan's MACD 0DTE calls) is fully built in `engine.py`. Books B, C, D are built in `agentdesk/books/` (paper only; every fill simulated by `PaperBroker.submit_combo` at mid −1¢/leg, never worse than natural; shadow only adds a read-only `review_option_order`). `BookHost` runs them from one-line engine hooks; book A's path is unchanged. Book E exists only as config (phase 5).
- Multi-book account (`books.account`): $10,000 paper balance, $1,500 open-risk cap (book A's open debit counts), $300 max loss per B/C/D position, C budget $400 (the lower wins), $0.04 fee per leg. A book that errors `max_consecutive_errors` times in a row halts and flattens itself only. `paper_only: true` is enforced; live mode needs exactly one promoted book and only A has a live path (`check_live_promotion`).

## The five books (all paper-only; Evan decided 2026-09-27: run them side by side)
- **A, Evan's MACD calls (built, unchanged):** 15m + 5m MACD(12,26,9) above signal = filter; 1m + 144-tick cross-up = trigger (fresh 1m cross -> SWING, 144t-only -> SCALP); RSI(14) 30–70 on 15m/5m/1m. Strikes by time of day + nearest resistance. $500 / max 5 contracts. Exits: −20% stop, scale-outs, breakeven, runner trail, cross-back, "ripping" hold, flatten 14:40 CT.
- **B, iron fly (keep exactly as specified; Evan said don't change it):** 08:45 CT, $5 wings, TP 50%, stop 1× credit (closing debit 2× credit, `stop_debit_x_credit: 2.0`, same as the backtest), close 14:30 CT. Skips high-impact events before 14:00 CT and days the Vol desk sets `vix1d_flag`.
- **C, ChatGPT's bullish 30-min ORB -> $2 bull-put spread:** built as requested, but the backtest is negative (underlying −3 bp/trade, t −5.75 in-sample; spread −3.5% on risk). Expect paper to confirm that.
- **C_bear_puts (disabled):** long puts on the bearish mirror; failed its backtest; stays off until a pre-registered filter set passes out-of-sample.
- **D, 10:00 ET iron condor (new):** short strikes at 0.9× expected move, $2 wings, TP 50%, stop 2× credit, close 14:25 CT, quiet-day filter. The published filter numbers (+8.4% / +7.3% / +12.6%) used the 9:30–10:30 ET range, 30 minutes past entry. The built filter uses only 08:30–09:00 CT (range vs its 14-day median) plus price within 0.12% of VWAP: +7.5% / +5.7% / +12.0% at 1¢, +3.3% / +1.5% / +9.0% at taker fills, 0.0% / −3.7% / +4.1% at IV multiplier 0.60 (`research/d_quiet_check.py`). Strikes use the prior VIX close from Robinhood's index data.
- **E, pre-earnings IV run-up (new, experiment):** E1 = ATM straddle in the first post-earnings expiry (4–10 DTE), bought at T−3, sold before the announcement. E2 = calendar (short pre-earnings weekly / long post-earnings weekly) entered T−10..T−8, exited T−1. Never held through the announcement. About 30 liquid large caps; the Vol desk runs the screen. No quote backtest yet (ThetaData later).

## The crew (`crew.py`, `proposals.py`)
- Desks: Macro, Rates, Fed, Vol, Quant, Risk, Tape. At huddles they brief, then talk to each other in a roundtable (lines carry from/to), then vote size 0.5–1.25×.
- Any vote below 1 cuts size (lowest wins). A size-up (max 1.25×, book A SWING only) needs every check in `Crew.size_up` to pass; the checklist shows on each entry.
- Proposals (rules Evan approved):
  - Next-trade and today tweaks auto-apply, but only for the `TWEAKS` whitelist within hard bounds: stop 10–25%, trail, first target, RSI cap, earlier cutoff, fewer trades, skip a setup, strike distance.
  - The crew can never change size or loss limits, including anything under `books.*`.
  - Standing changes and new strategies wait for Evan's Approve button. Approved strategies are built and backtested first; nothing new trades on its own.
- Offline (no `ANTHROPIC_API_KEY`) the crew runs templated briefs and roundtables. Online it uses web search.

## Data stack (paper)
- Robinhood MCP: option quotes/greeks, Level 2 (`get_equity_price_book`, Nasdaq TotalView), SPY bars, earnings calendar, order review.
- Free Alpaca IEX feed for SPY prints. IEX carries only about 4–6% of volume, so on `provider: alpaca` + `feed: iex` the engine uses `tick_bar_size_iex: 8` prints as the "144t" approximation (it logs a warning). Upgrade to Algo Trader Plus (SIP + OPRA, $99/mo) before live if A looks effective or data turns out to be the blocker.
- The recorder saves real 0DTE quotes, ATM−10..ATM+10 calls and puts, every 10s into `journal.option_quotes`. That is the data for real-quote analyses of A, B, C and D.
- Level 2 (`l2.py`) runs in observe mode: it logs book state per entry and never blocks. Use `l2-report` after a few weeks.

## Robinhood facts verified 2026-09-27
- Agentic account ••••6452, limited_margin, **option_level_3**, $500 cash. Show only the last 4 digits in anything user-facing.
- `review_option_order` accepted a 2-leg credit spread. Multi-leg orders need `direction`. Arg shapes are in `brokers/robinhood.py` (`order_args`, `fit_args`), and schemas have additionalProperties:false.
- SPY 0DTE `sellout_datetime` = 14:45 CT. Same-day opens are allowed until 3:30 PM ET; at-risk closeout starts 3:30 PM ET (3:45 for SPY). The engine flattens at 14:40 CT and 5 min before each contract's sellout. Short legs are never held into 3:30 PM ET.
- `get_option_historicals` returns only gap-filled bars for expired 0DTE contracts, so it can't be used for option backtests.
- $500 is too small for the default sizing. See the HANDOFF section 11 table, and ask Evan before changing any size or risk value.

## Research (research/, reproducible)
- 11 published intraday strategies: none significant out-of-sample. Evan's MACD proxy: t 0.23 / 1.69 / 0.39.
- The variance risk premium (short 0DTE premium) is the one robust effect, modeled from VIX. B and D both rely on it.
- `strategies_bcd.py` -> `strategies_bcd_results.json` covers B, C (both directions), D and cost/IV sensitivity. Options are modeled with Black-Scholes from VIX (M_RTH 0.80), re-based to SPY 765.

## Rules for working on this repo
- Never call `place_option_order`, `cancel_option_order` or `exercise_option` yourself. Orders go only through the engine in `--mode live` with `live_enabled: true`. Keep MCP permission prompts ON for those tools.
- Paper stays the default. Don't change strategy rules (A as Evan trades it, B as specified) without asking. A filter added after seeing results is a new variant to test separately.
- Run `python -m pytest -q tests` after changes (112 pass today; the two sim-day tests take about 50 s). Rebuild the demo with `tools/build_demo.py` if the UI changes.

## Next steps (HANDOFF section 10)
1. Mac setup + sim. 2. Alpaca keys + `rh-inspect`. 3. One full paper session of book A. 4. Multi-book framework + B, C, D. 5. Book E + `iv_history`. 6. Four or more weeks of paper with weekly Quant reports. 7. Real-quote analyses. 8. Evan promotes at most one book, then shadow for a week, then live at 1 lot.
