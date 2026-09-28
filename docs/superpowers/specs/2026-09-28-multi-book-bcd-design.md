# Multi-book framework + books B, C, D: design spec

Date: 2026-09-28. Status: **draft, waiting for Evan's review.** Source of truth: HANDOFF v3 sections 7B–7D, 9, 10 (phase 4), 11, 12. Nothing here changes a strategy rule without Evan's answer in section 2.

## 1. What this delivers

Phase 4 of HANDOFF section 10: "Done when all required tests pass and a sim day runs all books without cross-contamination."

- A `agentdesk/books/` package: the `Book` / `Strategy` / `OrderIntent` / `ExitIntent` interfaces from section 9, multi-leg (combo) positions, multi-leg paper fills with a reprice loop, account-level risk.
- Books B (iron fly), C (ORB bull-put spread), D (iron condor), implementing 7B–7D exactly (plus the answers in section 2).
- Engine hooks so the books run alongside book A in `sim`, `paper` and `shadow`. Book A keeps its current code path and results.
- Journal `book` column, per-book dashboard view, section 9's required tests.

Out of scope (other threads): book E and `iv_history` (thread 6), the Quant weekly report (thread 8), running the quote recorder standalone (thread 3), live sizing (deferred by Evan), any change to the order path, halts or watchdog internals (safeguards thread).

Already decided by Evan (thread plan, 2026-09-28): paper runs as a **$10,000** notional account, the section 11 $10k row. Book A's $500 / 5 contracts / $400 daily loss stay as they are.

## 2. Decisions that need Evan's answer

Each has a recommendation. The build uses the recommendation unless Evan says otherwise.

| # | Question | Recommendation | Why |
|---|---|---|---|
| Q1 | **D's quiet filter looks 30 minutes into the future in the backtest.** `strategies_bcd.py` measures the "first-hour range" over 9:30–10:30 ET, but D enters at 10:00 ET. Live code can't know that range at entry. | Use the range known at entry (9:30–10:00 ET) vs its own trailing 14-day median. Entry time, VWAP check and everything else unchanged. | It's the only version that can run live. Re-run below: still positive in all three periods at 1¢ and at taker, but weaker than the published table. |
| Q2 | **`stop_x_credit` means two things** (B = 1.0 "loss equals credit", D = 2.0 "debit reaches 2× credit"). Both describe the same stop: closing debit ≥ 2× credit, which is what the backtest does for both. | Replace both with one explicit field, `stop_debit_x_credit: 2.0`, for B and D. | No behavior change; removes the chance of coding one book with the wrong meaning. |
| Q3 | **C's size budget.** Section 7C says `floor($400 / max loss)`. Section 11's $10k row (the one Evan picked for paper) caps B, C, D at $300 max loss per position. | Keep C's formula, but apply the $300 section 11 cap to every B/C/D position (lower wins). In practice C usually trades 1 lot instead of 2. | The $10k row was adopted as the paper sizing. Keep $400 if Evan prefers C exactly as the ChatGPT spec. |
| Q4 | **Account-level open-risk cap** (section 9 asks for one, no number given). | `$1,500` (15% of $10k): the sum of max loss of every open position across A–D. Worst case all at once is about $1,150–1,250, so all books can open together; a runaway can't stack more. B/C/D also need paper buying power ≥ combined max loss (section 9). Book A is counted but never blocked by this (A unchanged). | Fits everything the spec expects to be open at once, and nothing more. |
| Q5 | **How paper fills a multi-leg order.** | Work each order from mid toward natural; paper fills when the limit reaches mid − 1¢ per leg for a credit (mid + 1¢ per leg for a debit), never better than natural. Every fill also logs the natural and mid price so the weekly report can re-price at taker. | Matches the backtest's base case (1¢ half-spread per leg), so "paper within its backtest range" (the promotion gate) compares like with like; taker stays available in the log. |
| Q6 | **B's VIX1D check.** Robinhood's index list has VIX but no VIX1D (checked 2026-09-28 with `get_indexes`). | B skips on the Vol desk's VIX1D flag when the crew is online (web search). With no VIX1D source (offline crew), B trades and logs "VIX1D unavailable". | Skipping B every offline day would silently stop the book. A data source for VIX1D can be added later. |

### Q1 evidence: D with the entry-time filter

`research/d_quiet_check.py` (new, reproducible; same data and model as `strategies_bcd.py`, which it imports). "As backtested" reproduces the HANDOFF table exactly.

| Period | Cost / IV | No filter | Quiet, 1st hour (as backtested) | Quiet, 9:30–10:00 (known at entry) |
|---|---|---|---|---|
| 2005–14 | 1¢, IV 0.80 | +4.7%, PF 1.57, t +9.2 | +8.4%, PF 2.58, t +12.1 | **+7.5%, PF 2.25, t +10.5** (n=797) |
| 2015–20 | 1¢, IV 0.80 | +3.1%, PF 1.34, t +3.7 | +7.3%, PF 2.29, t +7.2 | **+5.7%, PF 1.83, t +5.2** (n=351) |
| 2025–26 | 1¢, IV 0.80 | +8.5%, PF 2.59, t +6.7 | +12.6%, PF 6.91, t +9.0 | **+12.0%, PF 5.76, t +8.0** (n=90) |
| 2005–14 | 2¢ taker | +0.4%, PF 1.04 | +4.0%, PF 1.58 | **+3.3%, PF 1.44, t +4.6** |
| 2015–20 | 2¢ taker | −1.0%, PF 0.90 | +3.5%, PF 1.50 | **+1.5%, PF 1.18, t +1.4** |
| 2025–26 | 2¢ taker | +5.7%, PF 1.96 | +10.1%, PF 5.12 | **+9.0%, PF 3.94, t +6.1** |
| 2005–14 | 1¢, IV 0.60 | −4.8% | +0.2%, PF 1.02 | **0.0%, PF 1.00** |
| 2015–20 | 1¢, IV 0.60 | −6.9% | −1.0%, PF 0.90 | **−3.7%, PF 0.69** |
| 2025–26 | 1¢, IV 0.60 | −0.9% | +5.9%, PF 2.13 | **+4.1%, PF 1.64** |

Read: D stays the most robust modeled book, but about a fifth of the published edge came from the look-ahead, and at taker fills 2015–20 is no longer significant (t 1.4). The real-quote check (phase 7a) matters more than the table suggested.

## 3. Defaults picked without asking (say if any is wrong)

- **Architecture: "host" approach** (section 4). Book A's engine path is untouched; B/C/D run in a new book host the engine calls. Chosen over (a) refactoring A into the new `Book` class, which rewrites the engine the safeguards thread just hardened and risks changing A's results, and (b) one process per book, which multiplies the Robinhood rate budget and makes a global kill harder. A gets a thin read-only `Book` adapter so the dashboard and journal treat it like the others.
- **`paper_only` is enforced** (open item from the safeguards review). A `paper_only: true` book always gets simulated fills, in every mode; in `shadow` its orders also go to `review_option_order`; `place_option_order` is unreachable for it. `--mode live` refuses to start unless exactly one enabled book has `paper_only: false`, and in this build that can only be A. Today every book is `paper_only: true`, so live mode refuses until Evan flips one (section 12: promotion is an explicit config change).
- **Crew:** event blackouts (config events + crew briefs) apply to A–D. A crew size cut applies to B/C/D as `max(1, floor(lots × mult))`, so a 1-lot book is never silently skipped by a size vote; skipping a day stays an explicit crew restriction. No size-up for B–D.
- **D's strikes follow the backtest formula:** expected move = 0.80 × (prior VIX close / 100 / √252) × √(remaining RTH share + 15/390) × spot; shorts at `ceil(spot + 0.9·EM)` / `floor(spot − 0.9·EM)`, wings ±$2. Prior VIX close comes from Robinhood (`get_indexes` / index history, read-only; VIX verified available); sim uses the sim IV. No VIX → D skips the day (fail-closed).
- **B/C/D exit triggers use the combo mid** (like A's premium-mid rule). Take-profit and time exits are worked from mid toward natural; stops and flattens go straight to natural.
- **Halts:** the kill switch and every safety halt (watchdog, ambiguous order, startup) stop and flatten all books. A's own daily-loss / profit-lock halts stop A only. B/C/D have no separate daily-loss number: each is capped by its own max loss × trades per day (B 1, C 2, D 1).
- **History for D and C:** the books load their own 20 days of 1m history at startup (paper: one extra Alpaca/Robinhood history call; sim: a separately seeded generator, so book A's sim day is unchanged).
- **Fees:** $0.04 per contract per leg per side for combos (verified `review_option_order` fee). A keeps its `fee_per_contract`.

## 4. Architecture

```
engine.py (book A path unchanged)
   |  hooks: on_warmup(hist) · on_bar(bar) · on_trade(px) · on_second(now) · kill/flatten · halt_all · watchdog · snapshot
   v
books/host.py  BookHost ── AccountRisk (books/account.py): paper balance, buying power, open-risk cap, global halt
   ├── BookA adapter (read-only view of engine.open / engine.risk)
   ├── Book "B" ── IronFly strategy      (books/iron_fly.py)
   ├── Book "C" ── OrbBullPut strategy   (books/orb_bull_put.py)
   └── Book "D" ── IronCondor strategy   (books/iron_condor.py)
         each Book: BookRisk (trades/day, one open position, day P&L), ComboPositions, journal tag
   orders: books/fills.py  ComboExecutor (mid -> natural reprice loop) -> PaperComboBroker
                                            (+ shadow: review_option_order via RobinhoodMCP.call, never place)
   quotes: books/legs.py  LegQuotes (one batched get_option_quotes per combo; validation, fail-closed)
```

### Units

- **`books/base.py`**: `Leg(right, strike, side, ratio)`; `OrderIntent(legs, qty, direction, limit_rule, reason)`; `ExitIntent(reason, urgent)`; `Strategy` protocol: `on_bar(tf, bar, ctx)`, `on_clock(now, ctx)`, `on_quote(pos, cq, now, ctx)` returning `OrderIntent | ExitIntent | None`. `ctx` is a read-only `MarketContext`: spot, VWAP, session date, blackout list, prior VIX close, crew size multiplier, book's own risk state.
- **`books/combo.py`**: `ComboPosition` (legs with contracts, qty, entry price as credit or debit, max loss, marks, fills with mid/natural at each fill, `last_quote_ts` for the watchdog), `ComboQuote` (mid, natural, per-leg quotes, `valid` + reason). Max loss: credit structures = (widest wing width − credit) × 100 × qty.
- **`books/legs.py`**: fetches all leg quotes for a combo (one batched Robinhood call when the source is Robinhood, per-leg otherwise). **Rejects** crossed, zero, stale (> 5 s) quotes and any leg whose spread > 25% of its mid. An invalid quote means: no entry; for an open position, no exit decision that tick (the watchdog halts after `quote_stale_sec` without a valid quote, same as A).
- **`books/fills.py`**: `ComboExecutor.work(intent, qty)`: limit starts at mid, steps toward natural by `reprice_step_c` per attempt up to `max_reprices`, last attempt at natural; urgent exits start at natural. One `ref_id` (uuid) per logical order, kept across reprices and logged. `PaperComboBroker` fills per Q5. A partial or unconfirmed fill blocks new orders for that book until reconciled (paper fills are all-or-nothing, so this path is exercised in tests with a fake broker).
- **`books/account.py`**: `AccountRisk`: `paper_balance` ($10,000), realized P&L, open max-loss sum (A's open debit counts), `open_risk_cap`, `per_position_max_loss`, `buying_power()`, `can_open(book, max_loss)`, global `halt(reason, flatten)`.
- **`books/book.py`**: `Book`: name, config, strategy, `BookRisk` (trades today, one open position, entry window, day P&L, halted), positions, closed trades, journal tag. Applies blackouts, the `flatten 5 min before sellout` rule, early-close days (no entries after 11:20, flat 11:40) and the account checks before any entry.
- **`books/iron_fly.py` (B)**: 08:45 CT, skip if a high-impact event is scheduled before 14:00 CT or the VIX1D flag is set (Q6). Sell ATM call + put at `round(spot)`, buy ±$5 wings, 4-leg credit. TP when closing debit ≤ 50% of credit; stop when closing debit ≥ 2× credit (Q2); close 14:30 CT. 1 lot. One entry per day.
- **`books/orb_bull_put.py` (C)**: 5m bars, 09:00–13:30 CT (10:00–14:30 ET) signal window. Opening range = 08:30–09:00 CT high. Fresh breakout (close > ORH and prior close ≤ ORH), close > VWAP, EMA20 > EMA20 three bars ago, RSI(14) 55–72, green candle, volume ≥ 0.8× rolling 20-bar median (prior bars, across days), max 2 trades/day, one position at a time, no re-entry signal until the position is closed (the backtest's `k = j + 1`). Short put = `floor(spot)` (or spot − 1 when spot is a whole number), long put 2 lower, credit order. Size per Q3. Exits on SPY price, first wins: −0.18%, +0.45%, 5m MACD cross below signal, 45 minutes, 14:15 CT. Indicators (EMA20, RSI14, MACD on 5m) are the existing `indicators.py` classes, warmed from history like the backtest's continuous series.
- **`books/iron_condor.py` (D)**: 09:00 CT (10:00 ET). Quiet filter per Q1 (on by default): 08:30–09:00 CT range / spot < trailing 14-day median of the same measure, and |spot / VWAP − 1| ≤ 0.12%. Strikes per section 3. TP 50%, stop `stop_debit_x_credit` 2.0, close 14:25 CT. 1 lot.

### Engine touch points (small, additive)

`engine.py` gets a `self.books` host (None when no B/C/D book is enabled) and one-line calls from: `_warmup` (hand over history), `_on_bar` (bar close), `_on_trade` (price for C's stops), `_on_second` (clock + managing open combos on the same `quote_poll_ms` cadence), `_new_day`, `kill`, `flatten`, `_trip` (global halt), `_watchdog` (combo positions' quote age), `snapshot`. The order path (`_submit`, `_work_order`), A's entry/exit logic, `_guard`, `_reconcile` and the halt conditions themselves don't change. Combo positions are paper-filled, so they're excluded from the live position reconciliation (they never exist in the account).

`__main__.build` wires the host and enforces the live-mode rule in section 3.

### Journal

- `trades` gains `book TEXT` (existing rows and book A default `'A'`) plus `legs TEXT` (JSON) and `max_loss REAL`, added with the same `ALTER TABLE ... ADD COLUMN` pattern already used for `l2`. Combo trades store entry/exit credit, and every fill's mid and natural, in `fills`.

### Dashboard

- A book switcher (All / A / B / C / D) in the header, a per-book P&L strip (day P&L, trades, W/L per book), the book letter on chart markers and in the trades table, and the office monitors showing the active book's position. Positions card shows combos as legs with credit, mark, TP and stop. Rebuild the demo with `tools/build_demo.py`.

### Config (proposed diff)

```yaml
books:
  account: {paper_balance: 10000, open_risk_cap: 1500, per_position_max_loss: 300, fee_per_leg: 0.04}   # Q3, Q4
  fills:   {model: mid_offset, cents_per_leg: 1, reprice_step_c: 1, max_reprices: 4, max_quote_age_s: 5, max_leg_spread_pct: 0.25}   # Q5
  B_iron_fly:     {..., stop_debit_x_credit: 2.0, skip_event_before_ct: "14:00", vix1d_gap_skip: 3.0}    # Q2, Q6
  C_orb_bull_put: {..., window_ct: {start: "09:00", end: "13:30"}, stop_pct: 0.0018, target_pct: 0.0045, max_hold_min: 45, close_ct: "14:15"}
  D_iron_condor:  {..., stop_debit_x_credit: 2.0, quiet_window_min: 30, quiet_lookback_days: 14, vwap_max_pct: 0.0012, em_rth_mult: 0.80}   # Q1, Q2
```

Existing keys and values stay. The crew's `TWEAKS` whitelist doesn't include any `books.*` key, and a test pins that.

## 5. Testing (superpowers test-driven development: each test written and seen failing before its code)

Section 9's required tests, plus the integration ones:

1. C: fresh-breakout detection (close above ORH only counts when the prior close was at or below it) and duplicate-signal suppression (no second entry while a position is open; max 2/day).
2. Max-loss sizing for B, C, D, including the $300 cap and the account open-risk cap and buying-power block.
3. Credit/debit `direction` on multi-leg orders (B, C, D are credit), and the leg list's sides/position effects for open and close.
4. Take-profit, stop and time exits for B, C, D (each on a scripted quote/price path).
5. Stale / crossed / zero / wide-leg quotes fail closed: no entry, no exit decision, watchdog trip after `quote_stale_sec`.
6. Kill switch and a safety trip flatten every book; A's daily-loss halt leaves B/C/D running.
7. The proposals whitelist can't touch `books.*` size or loss fields.
8. `paper_only` books never reach `place_option_order` in any mode; live mode refuses when no book is promoted.
9. D: strike formula from VIX, quiet filter uses only data known at 09:00 CT, no-VIX skip.
10. B: event-before-14:00 skip, VIX1D flag skip.
11. **No cross-contamination:** a full seeded sim day runs A–D; book A's trades are identical with B/C/D on and off; every journal row carries the right book; per-book P&L sums to the account P&L.
12. The existing 38 tests keep passing unchanged.

## 6. Risks and open points

- Robinhood rate budget: each open combo adds one batched quote call per second on top of A and the recorder. Thread 3 measures the ceiling; the combo poll interval is a config value.
- Index-history argument shapes (`get_indexes`, index quotes/history) are matched with `fit_args` against the live schema and still need one `rh-inspect` run on the Mac.
- Paper fills are a model. The recorded quotes (phase 7) are the real check.
