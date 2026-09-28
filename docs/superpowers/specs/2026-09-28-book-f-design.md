# Book F (large-cap "stocks in play"): integration design

Date: 2026-09-28. Status: **draft, waiting for Evan's review (his merge of the PR is the approval).**
Source of truth: `docs/BOOK_F_HANDOFF.md` (Evan's build brief; its section 3 rules are frozen) and HANDOFF v3 sections 9 and 12 (safety wins on any conflict). This document only decides *how F plugs into the code*; it does not change a rule.

## 1. What this delivers

- `agentdesk/books/f_stocks_in_play.py`: the pure section 3 functions (`atr14`, `rvol5`, `rank_candidates`, `shares_for`, `should_exit`, plus the bar-level fill rules) and the `StocksInPlay` strategy state machine.
- `agentdesk/books/f_host.py`: `FHost`, the equity counterpart of `BookHost`. It drives the 07:30 CT cache, the 08:35:05 CT (09:35:05 ET) scan, entries until 09:30 CT (10:30 ET), stops, and the 14:55 CT (15:55 ET) exit, with F's own `Book` state, its own daily loss, the shared kill switch and the account open-risk cap.
- `agentdesk/books/group.py`: `HostGroup`, which lets the engine keep calling one `engine.books` object while B/C/D (BookHost) and F (FHost) run side by side.
- Equity broker path: `agentdesk/brokers/paper_equity.py` (`PaperEquityBroker`) and `agentdesk/brokers/robinhood_equity.py` (`RobinhoodEquityBroker`, review first, place only in live with `live_enabled` and Evan's account number), plus an equity section for `rh-inspect`.
- Data: `agentdesk/feeds/f_data.py` (`RobinhoodEquityData` with the `~/.agentdesk/cache/f/` cache, the S&P 500 list cache, a per-minute call counter and a 20 calls/s burst cap; `SimEquityData` for sim days).
- Journal: `agentdesk/books/f_journal.py` adds `f_scans`, `f_shadow_shorts`, `f_positions` (persisted open positions for the restart check) and `f_trades` (with R multiple) to the same SQLite file. F trades also go into `trades` with `book='F'`.
- News tag: `agentdesk/books/f_news.py` (Tape + Macro via the crew's Claude client and web search; offline = unknown/unknown). Observe-only.
- Dashboard: `agentdesk/web/book_f.js` (scan table + F strip; Tape monitor shows the picks) loaded by one `<script>` line.
- CLI: `python -m agentdesk f-report [--since YYYY-MM-DD]`.
- Research: `research/strategy_f_intraday.py` (resumable Alpaca SIP pull + the pre-registered replication backtest, run by Evan on his Mac).
- `tests/test_book_f.py` (+ `tests/test_f_research.py`): every test in handoff section 6.

## 2. Integration decisions (recommendation is what's built)

| # | Question | Built as | Why |
|---|---|---|---|
| F-Q1 | PR #6's `BookHost` is built around SPY option combos (legs, contracts, combo quotes). | F gets its own host (`FHost`) that reuses `Book`, `OrderIntent`/`ExitIntent`/`Skip`, `MarketContext` and `AccountRisk`, and a `HostGroup` so the engine still sees one `engine.books`. | No edits to PR #6's files; F can't disturb B/C/D fills. |
| F-Q2 | The account open-risk cap must see F's risk and B/C/D must see F's. | F's open risk = Σ shares × (entry − stop). `HostGroup` gives `BookHost.open_risk` F's risk (instance hook, no file edit) and F checks the same `AccountRisk.can_open` against A+B+C+D+F. F also refuses an entry whose share notional would exceed free paper equity (no margin). | Section 9 cap applies to all books; no margin borrowing (handoff 7). |
| F-Q3 | Trigger source. The handoff says subscribe the picks on the Alpaca IEX stream; the free plan allows one websocket, which the engine already holds for SPY. | Triggers come from Robinhood equity quotes polled every 1 s (last trade price, falling back to ask) and each completed 1-minute bar high. The IEX add-on (5 extra symbols on the engine's own connection) is left as a follow-up in `feeds/alpaca.py`, which this thread doesn't own. | Never opens a second connection (coordinator note); IEX prints are 4–6% of volume anyway, and RVOL5 must not mix IEX volume. |
| F-Q4 | Entry order when price runs through the limit (OR high × 1.0005). | A buy is a limit at OR high × 1.0005. Paper fills at max(trigger, ask) + 1 bp only if that is ≤ the limit; otherwise the name stays armed and can trigger again before 10:30 ET (at most 3 sends per name). No chasing above the limit. | A marketable limit can miss; chasing would change the rule. |
| F-Q5 | Sizing basis before the fill is known. | Shares from the limit price and stop = limit − 0.10 × ATR14; after the fill the stop is re-anchored at fill − 0.10 × ATR14 (section 3). | Size can only be computed before the order. |
| F-Q6 | Daily loss −$75. | Realized F P&L ≤ −$75 halts F for the day and flattens anything F still holds (same as a B/C/D book halt). A and B–E keep trading. | Matches the framework's per-book halt; "halts F only". |
| F-Q7 | Restart with F shares open. | Open paper positions are persisted in `f_trades` (status open). At startup an open row from an earlier session, or found after the 15:55 ET exit, halts F ("held overnight"); an open row from earlier today is restored and managed. In live, any equity position in the account at startup that F isn't tracking halts every book. | "Never held overnight", "paper must survive restarts". |
| F-Q8 | Macro event check. | Skip the whole scan if a high-impact event (crew calendar or config events, the same list the blackouts use) is scheduled 09:35–10:30 ET. | Section 3. |
| F-Q9 | Half-days. | `calendar.early_close` dates: scan and entries unchanged, exit 12:55 ET. | Section 3. |
| F-Q10 | Where the news tag lives. | `books/f_news.py`, not `crew.py`: it borrows the crew's client and emits Tape/Macro crew events. | `crew.py` is being changed by PR #4 and the new-desks thread; no conflict this way. |
| F-Q11 | `research/strategy_f.py` (the 2013–18 daily study) is not in the repo. | The intraday script also writes the per-symbol daily CSVs the handoff asks for; the daily rerun waits until Evan adds `strategy_f.py` from the claude.ai session. | It can't be rerun without the original script. |

## 3. Data flow

1. **07:30 CT** `FHost` builds the universe: S&P 500 list (weekly cache) → daily bars for all (Robinhood, 10 symbols/call) → top 130 by 20-day dollar volume + the 18 extras → filters (close ≥ $10, ATR14 ≥ $0.50, 20-day $ vol ≥ $100M) → tradability check → the prior 14 sessions' 09:30–09:34 1-minute volume per name. Cached per day under `~/.agentdesk/cache/f/`.
2. **08:35:05 CT** scan: today's 09:30–09:34 bars for the universe (~15 calls, one retry each, missing = dropped). RVOL5 and the first candle per name → `rank_candidates` → top 5 green (ties by 20-day $ volume). All rows go to `f_scans` with a reason; red candles go to shadow-short tracking. The news tag request starts in the background.
3. **Until 09:30 CT**: 1 s quote polls for armed and open names. A trade above OR high triggers the limit buy (subject to the gates: F halted? kill? paused? account cap? F max 5 open? per-name once?). At 09:30 CT armed names expire.
4. **Open positions**: stop = fill − 0.10 × ATR14 checked on every quote (bid ≤ stop → marketable limit sell) and on every 1-minute bar (gap → bar open; entry bar low ≤ stop → stopped). Exit at 14:55 CT (11:55 CT on half-days). Kill switch / engine safety halt / F halt flatten.
5. **Shadow shorts**: the top 5 red-first-candle names by RVOL5 (≥ 2.0) log a would-be short at the OR-low break with the same stop and exit math; never an order.

## 4. Error handling

- A stale (> 5 s) or missing quote fails closed: no entry; open positions keep their old `last_quote_ts`, so the engine watchdog halts everything after `quote_stale_sec` as for A–D.
- Robinhood errors during cache/scan: one retry per call; a name without data is dropped; if the scan can't run the day is skipped and logged.
- Three errors in a row inside F halt and flatten F only (framework rule).
- Robinhood calls are throttled to ≤ 20/s with a per-minute count logged.

## 5. Testing

`tests/test_book_f.py` covers every item of handoff section 6 with fakes (no network). `tests/test_f_research.py` covers the backtest's day simulation and stats on synthetic bars. A sim day (`SimEquityData`) runs A–F together and checks F's trades don't touch A/B/C/D state.

## 6. What still needs Evan

- Run the replication pull + backtest on his Mac (one command, in the PR and the thread).
- Add `research/strategy_f.py` from the claude.ai session so the 2016–2026 daily rerun can happen.
- `rh-inspect` with the new equity section, and the first live-market paper session, on a market day after the merge.
