# Book D Fill-Quality Study Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure what a resting-then-walking limit order pays per leg on book D's SPY 0DTE legs (ThetaData tick
trades with NBBO), then re-score D at that cost against the pre-registered pass rule.

**Architecture:** One research script, `research/d_fill_quality.py`, with pure functions (order walk, fill rules,
cost pooling, measured fill model, verdict) and three CLI subcommands (`pull-ticks`, `measure`, `score`). It reuses
`research/bd_real_quotes.py` for D's legs, spot, VIX and replay; the only change there is letting a fill model be a
callable. 1-minute quotes come from a fresh cloud pull with the existing `research/fetch_thetadata_spy0dte.py`.

**Tech Stack:** Python 3.12, pandas, numpy, pyarrow, `thetadata` client, pytest.

**Spec:** `docs/superpowers/specs/2026-11-09-d-fill-quality-design.md` (approved by Evan 2026-11-10).

## Global Constraints

- Research only: no change under `agentdesk/`, `config.yaml` or `tools/`. No order tool is ever called.
- Sessions 2016-01-08..2026-09-25, D rules exactly as `bd_real_quotes.py` (entry 10:00 ET, `legs_D`, TP 50%,
  stop at a 2x credit closing debit, close 15:25 ET, width $2, fee $0.04 a leg a side), no quiet filter.
- Walk: start at the price nearest mid on the passive side; hold H s (primary 60; also 15 and 300); then 1c toward
  the far side every 15 s; marketable at natural. Entry decision 10:00:00 ET; close decision 15:20:00 ET, cross at
  natural by 15:28:00 ET.
- Fill rules: queue (primary), queue x2, optimistic, strict, as defined in spec section 4.
- Cost in cents vs the decision-time mid, positive = worse than mid; pooled by window (entry/close), side
  (buy/sell), width bucket (1, 2, 3 = 3c or wider).
- Part 2: stop exits pay taker. Pass = mean > 0 with t >= 2.33, mean > 0 in 2016-2021 and in 2022-2026, and mean > 0
  under queue x2 (all at H = 60 s).
- ThetaData: one live session per account; run pulls one at a time. Data goes under `data/` (gitignored).
- Tests: `python -m pytest -q tests research/tests` must stay green.

## Review Focus

- A leg with no trades or no valid quote in its window: the order still resolves (crosses at the deadline's natural,
  or is dropped and counted if there is no quote at all), never crashes the run.
- Crossed or locked quotes (bid >= ask) and zero bids in the tape: ignored for decisions, never used as the mid.
- The market running away from a resting order: the walk keeps stepping and the deadline forces a fill at natural,
  so no order stays open forever.
- A width bucket or window with no observations in Part 1: Part 2 falls back to the nearest wider bucket for that
  side and window, and the report says so.
- Reproduction drift: the cloud re-pull must reproduce the verdict's D numbers (mid +0.81%, patient -1.45%,
  taker -1.97%) within 0.10 points before Part 2 is trusted.

---

### Task 1: Walk and fill rules on one tape

**Files:**
- Create: `research/d_fill_quality.py`
- Test: `research/tests/test_d_fill_quality.py`

**Interfaces:**
- Produces:
  - `TAPE_COLS = ["ts", "price", "size", "bid", "ask", "bid_size", "ask_size"]` (ts: tz-aware ET timestamps).
  - `start_price(bid: float, ask: float, side: str) -> float` (side "buy"/"sell"; rounds to $0.01).
  - `@dataclass Order: price: float | None; ts: pd.Timestamp | None; mid0: float; width_c: int; passive: bool; reason: str`
    (reason in "trade_through", "cross", "queue", "walked_to_natural", "deadline", "no_quote").
  - `simulate(tape: pd.DataFrame, t0: pd.Timestamp, side: str, hold_s: int, rule: str, deadline: pd.Timestamp, step_s: int = 15) -> Order`
    (rule in "queue", "queue2", "optimistic", "strict").
  - `cost_cents(o: Order, side: str) -> float | None` (fill - mid0 for buy, mid0 - fill for sell, in cents).

- [ ] **Step 1: Write the failing tests** (hand-made tapes, ET timestamps on 2024-06-12):
  - `test_start_price`: `start_price(1.00, 1.01, "sell") == 1.01`, `("buy") == 1.00`; 2c market `(1.00, 1.02)` -> 1.01
    both sides; 3c `(1.00, 1.03, "sell") == 1.02`, `("buy") == 1.01`.
  - `test_queue_fill_after_size_clears`: sell at ask 1.01 with ask_size 10 at t0; trades at 1.01 of size 6 then 5
    -> fills at 1.01 on the second trade under "queue", reason "queue"; under "queue2" (queue 20) no fill there.
  - `test_trade_through_fills_at_limit`: a trade at 1.02 while resting at 1.01 -> fill 1.01, reason "trade_through".
  - `test_cross_fills`: bid rises to 1.01 while selling at 1.01 -> fill 1.01, reason "cross".
  - `test_walk_reaches_natural`: 1c market, no trades, hold 60 -> at t0+60 s the limit steps to the bid and fills
    at the bid then (reason "walked_to_natural"), cost +0.5c for the sell.
  - `test_optimistic_vs_strict`: one trade at 1.01 of size 1 with ask_size 500: optimistic fills, strict and queue
    don't.
  - `test_deadline_forces_natural`: market runs away 1c every 10 s for a buy; at the deadline it fills at that
    moment's ask, reason "deadline".
  - `test_bad_quotes_ignored_and_no_quote`: rows with bid >= ask or bid <= 0 are skipped; a tape with no valid
    quote at or before t0 returns `Order(price=None, reason="no_quote")`.
  - `test_cost_signs`: buy filled at bid of a 1c market -> -0.5; sell filled at bid -> +0.5.
- [ ] **Step 2: Run** `python -m pytest -q research/tests/test_d_fill_quality.py` -> FAIL (module missing).
- [ ] **Step 3: Implement** the five names above. Walk loop over tape rows after t0: the limit at time t is
  `start` moved `k` cents toward the far side, `k = 0` before `t0 + hold_s`, then `+1` per `step_s`, never past the
  current natural; whenever the current natural is at or through the limit it fills at the natural ("cross" before
  any step, "walked_to_natural" after). Queue ahead = displayed size at the limit when the limit equals the
  passive-side quote at the moment the order (re)prices, else 0; "queue2" doubles it; trades at exactly the limit
  reduce it; a reprice resets it. At `deadline` an unfilled order fills at the last valid natural.
- [ ] **Step 4: Run** the same command -> PASS.
- [ ] **Step 5: Commit** `research: d_fill_quality order walk and fill rules (TDD)`.

### Task 2: Callable fill models in the D replay

**Files:**
- Modify: `research/bd_real_quotes.py` (`_value`)
- Modify: `research/d_fill_quality.py`
- Test: `research/tests/test_d_fill_quality.py`, `research/tests/test_bd_real_quotes.py` (must stay green)

**Interfaces:**
- Consumes: `bd_real_quotes.replay`, `_value`, `legs_D`, `QuotePanel`, `FEE`.
- Produces:
  - In `bd_real_quotes._value`: when `model` is callable, the leg price is `model(bid, ask, side, opening)`;
    string models are unchanged.
  - `Costs = dict[tuple[str, str, int], float]` keyed `(window, side, width_bucket)`, window "entry"/"close".
  - `measured_model(costs: Costs) -> Callable[[float, float, str, bool], float]`: price = mid + c (buy) or mid - c
    (sell), c from the key (opening -> "entry", closing -> "close"), clamped between the passive side and natural;
    a missing key falls back to the next wider bucket, then to taker.
  - `uniform_model(c_cents: float) -> Callable` (same clamp, same c for every leg).
  - `replay_d(p: QuotePanel, legs, model) -> dict | None`: `bd_real_quotes.replay(p, legs, D_ENTRY, D_CLOSE, model,
    width=2)`, then if `why == "stop"` reprices the exit at `exit_minute` with "taker" and recomputes `pnl`/`ret`.

- [ ] **Step 1: Write the failing tests:**
  - `test_measured_model_clamps`: 1c market (1.00/1.01), cost -0.5 for an entry sell -> 1.01; cost +2 -> 1.00 (natural).
  - `test_measured_model_fallback`: missing 2c key uses the 3c key; missing everything -> taker price.
  - `test_uniform_zero_is_mid`: `uniform_model(0)` gives mid on a 2c market.
  - `test_callable_model_matches_string_models`: on a small synthetic `QuotePanel`, `replay` with
    `uniform_model(0)` equals `model="mid"`, and a callable returning taker equals `model="taker"`.
  - `test_replay_d_stop_pays_taker`: synthetic day where the debit jumps past 2x credit -> `why == "stop"` and pnl
    equals credit minus the taker closing debit plus fees.
- [ ] **Step 2: Run** `python -m pytest -q research/tests/test_d_fill_quality.py research/tests/test_bd_real_quotes.py` -> new tests FAIL.
- [ ] **Step 3: Implement** the `_value` change (three lines) and the four names.
- [ ] **Step 4: Run** the same command -> PASS.
- [ ] **Step 5: Commit** `research: measured and uniform fill models for the D replay`.

### Task 3: Pooling and verdict

**Files:**
- Modify: `research/d_fill_quality.py`
- Test: `research/tests/test_d_fill_quality.py`

**Interfaces:**
- Produces:
  - `width_bucket(width_c: int) -> int` (1, 2, or 3 for 3c+).
  - `pool_costs(events: pd.DataFrame, rule: str, hold_s: int) -> Costs` from event rows with columns
    `window, side, width_c, rule, hold_s, cost_c` (mean `cost_c` per key; `cost_c` NaN rows dropped).
  - `verdict(primary: pd.DataFrame, queue2: pd.DataFrame) -> dict` with keys `pass` (bool), `t`, `avg`,
    `avg_2016_2021`, `avg_2022_2026`, `avg_queue2`, `checks` (three bools); inputs are D trade rows with `year`
    and `ret`; uses `bd_real_quotes.summ`; thresholds t >= 2.33.

- [ ] **Step 1: Write the failing tests:** `test_width_bucket` (1->1, 2->2, 3->3, 7->3); `test_pool_costs_means_by_key`;
  `test_verdict_pass` (synthetic returns with mean 1%, sd 2%, n 400, both halves positive, queue2 positive -> pass);
  `test_verdict_fails_one_half` (2016-2021 mean negative -> pass False, `checks[1]` False);
  `test_verdict_fails_queue2`.
- [ ] **Step 2: Run** -> FAIL. **Step 3: Implement.** **Step 4: Run** -> PASS.
- [ ] **Step 5: Commit** `research: D fill study cost pooling and pre-registered verdict`.

### Task 4: Data pull and reproduction gate

**Files:**
- Modify: `research/d_fill_quality.py` (CLI `pull-ticks`)
- Test: `research/tests/test_d_fill_quality.py`

**Interfaces:**
- Consumes: `fetch_thetadata_spy0dte.py` output `data/thetadata/spy_0dte/YYYY-MM-DD.parquet`; CBOE VIX history at
  `data/vix.csv` (`curl -L https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv`).
- Produces:
  - `d_legs_for_day(p: QuotePanel, d: date, vix: pd.Series) -> list[tuple[str, float, int]] | None` (spot at
    `D_ENTRY`, `legs_D`; None when spot or VIX is missing).
  - `normalize_ticks(raw: pd.DataFrame) -> pd.DataFrame` -> `TAPE_COLS` (from ThetaData `trade_timestamp`, `price`,
    `size`, `bid`, `ask`, `bid_size`, `ask_size`; timestamps converted to America/New_York).
  - CLI: `python research/d_fill_quality.py pull-ticks --quotes data/thetadata/spy_0dte --vix data/vix.csv --out data/thetadata/d_ticks`
    writes `data/thetadata/d_ticks/YYYY-MM-DD_{R}{K}_{entry|close}.parquet` for each D leg (entry window
    09:59:00-10:10:00, close window 15:19:00-15:28:00), resumable (skips files on disk), one client, retries
    transient gRPC errors like `fetch_thetadata_spy0dte.fetch_day`, logs failures to `d_ticks_failures.csv`.

- [ ] **Step 1: Write the failing tests:** `test_normalize_ticks_columns_and_tz` (a 2-row raw frame shaped like the
  2026-11-06 probe -> `TAPE_COLS`, ET tz); `test_d_legs_for_day` (synthetic panel + VIX 16 -> four legs, shorts
  -1, wings 2 beyond).
- [ ] **Step 2: Run** -> FAIL. **Step 3: Implement.** **Step 4: Run** -> PASS. **Step 5: Commit**
  `research: D fill study tick pull`.
- [ ] **Step 6: Pull 1-minute quotes** (background, resumable):
  `python research/fetch_thetadata_spy0dte.py --start 2016-01-08 --end 2026-09-25 --workers 2`.
  Probe timing: about 5 s a day, so about 1.5-3 h.
- [ ] **Step 7: Reproduction gate:** `python research/bd_real_quotes.py run --quotes data/thetadata/spy_0dte --out data/thetadata/bd_results --vix data/vix.csv`.
  Expected: D (all sessions) mid +0.81%, patient -1.45%, taker -1.97% each within 0.10 points. If not, stop and
  report the gap to Evan before Part 2.
- [ ] **Step 8: Pull ticks** with the CLI above (about 0.2 s a request, about 15,000 requests, under 12 h, so
  every session is used, per spec section 6).

### Task 5: Measure, score, report

**Files:**
- Modify: `research/d_fill_quality.py` (CLI `measure`, `score`)
- Create: `research/d_fill_quality.md`, `research/d_fill_quality_results.json`
- Modify: `CLAUDE.md` (Research list), `README.md` (Evidence table), `research/README.md` (usage lines)
- Test: `research/tests/test_d_fill_quality.py`

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces:
  - CLI `measure --ticks data/thetadata/d_ticks --out data/thetadata/d_fill` -> `events.parquet` with one row per
    (session, leg, window, rule in 4 rules, hold_s in {15, 60, 300}): `date, window, side, right, strike, width_c,
    rule, hold_s, price, mid0, cost_c, reason, secs_to_fill, cost_vs_mid_at_fill_c`. Entry: buy wings and sell
    shorts at 10:00:00, deadline 10:10:00. Close: buy shorts and sell wings at 15:20:00, deadline 15:28:00.
  - CLI `score --quotes data/thetadata/spy_0dte --vix data/vix.csv --events data/thetadata/d_fill/events.parquet --out research`
    -> D rows for: measured (each rule x each H), uniform c in -0.50..+1.00 step 0.25, plus mid/patient/taker
    for reference; writes `research/d_fill_quality.md` and `research/d_fill_quality_results.json` with the cost
    tables, fill rates and reasons, time to fill, the D table, the break-even c, per-year rows, and the verdict.

- [ ] **Step 1: Write the failing test:** `test_measure_and_score_end_to_end` on a 2-session synthetic fixture (tiny
  quote panels + tapes in `tmp_path`): `measure` writes events with all 4 rules x 3 holds for every leg/window;
  `score` writes the md and json, the json has `verdict.pass` and a `uniform` table with 7 rows.
- [ ] **Step 2: Run** -> FAIL. **Step 3: Implement.** **Step 4: Run** -> PASS. **Step 5: Commit**
  `research: D fill study measure and score`.
- [ ] **Step 6: Run** `measure` then `score` on the full data. Write the verdict paragraph into
  `research/d_fill_quality.md` (the numbers come from the json; the pass rule is not changed).
- [ ] **Step 7: Update** CLAUDE.md, README.md and research/README.md with one line each on the result.
- [ ] **Step 8: Full suite** `python -m pytest -q tests research/tests` -> all pass. Commit, push, open the PR
  (research only), merge after green CI under Evan's standing merge OK, and post the verdict in the thread.
