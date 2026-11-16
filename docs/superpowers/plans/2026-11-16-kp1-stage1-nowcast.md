# KP1 Stage 1 (historical nowcast check) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure, out of sample, how closely public data predicts seven KPI-binary KPIs (HOOD, MARA, COIN) before each report, and apply the spec's Stage 1 gate.

**Architecture:** A frozen pre-registration first. Then hand-typed reported values and HOOD monthly data (small committed CSVs with a source URL per row), fetched on-chain and exchange data (cached under `research/data/kp1/`), pure nowcast and scoring functions, and one script that writes a results JSON and a Markdown report. Research only: nothing in `agentdesk/`.

**Tech Stack:** Python 3.12 stdlib only (`urllib.request` for fetches; scipy and requests aren't in requirements.lock), pytest. The Student-t CDF with 4 degrees of freedom has a closed form: T4(t) = 1/2 + t(t² + 6) / (2(t² + 4)^1.5).

**Spec:** `docs/superpowers/specs/2026-11-16-kpi-binaries-public-data-design.md` (approved 2026-11-16). Stage 2 (live quotes) is out of scope for this plan; it gets its own plan after launch.

## Global Constraints

- Paper and research only. Never call `place_*`, `cancel_*` or `exercise_*` tools. No code in `agentdesk/`.
- No paid data. Sources: Robinhood releases (mirror `mondovisione.com`, or WebFetch of investors.robinhood.com; curl from the cloud gets 403), MARA IR (`ir.mara.com`, reachable), Coinbase releases (WebFetch/mirror; SEC only from Evan's Mac), `mempool.space` API, `api.exchange.coinbase.com` candles.
- Information rule: a quarter's nowcast uses only data published before its report date (all three report after the close; a monthly release counts if its publication date is earlier than the report date). Parameters for quarter q are fit only on quarters before q.
- Contract scales (Cboe Day-1 list v3): KCHOD 1e6, KBHOD 1e9, KDHOD 1, KCMAR 1, KFMAR 1e3, KBMAR 1e6, KCCOI 1e6. Strike interval on the scaled value: 0.01 below 10, 0.10 below 100, 1.00 below 1,000, 10 below 10,000, 100 above.
- Error model: e = ln(actual / nowcast). A quarter is scored once at least 4 earlier nowcast errors exist. Predictive P(KPI >= K) = 1 − T4((ln(K / nowcast) − mean(e)) / sd(e)), sd with ddof 1, floored at 1e-4.
- Gate (per KPI): median |e| <= 0.5 × the naive baseline's median |e|, and the 80% interval (10th-90th percentile of the predictive t) covers 60%-95% of scored quarters.
- Settlement values are the highest-precision printed figure in the earnings press release.
- `python -m pytest -q research/tests` must pass; run the full `tests research/tests` once at the end.

## Review Focus

1. A monthly release published on or after the report date leaking into that quarter's nowcast: expect it excluded (Task 4 test).
2. Fiscal quarters and time zones: MARA Pool blocks just either side of a quarter boundary at 00:00 UTC; expect the boundary rule from the prereg, tested both sides (Task 3).
3. A revenue line missing in the prior quarter (e.g. HOOD event contracts before they existed): expect a 0 contribution, not a crash or NaN (Task 4).
4. A Coinbase product with no candles for some days (listed later, or delisted): expect those days to add 0 volume, not raise (Task 3).
5. A KPI with fewer than 4 earlier errors: expect "not scored", never a score from 1-3 errors (Task 5).

---

### Task 1: Freeze the pre-registration

**Files:**
- Create: `research/kp1_kpi_prereg.md`
- Modify: `research/README.md`: a KP1 entry with the rerun command and outputs of each new script (`kp1_inputs.py`, `kp1_fetch.py`, `kp1_kpi_nowcast.py`); `research/tests/test_research_readme.py` fails if any `research/*.py` is missing from it, so add each script's line in the task that creates it

- [ ] **Step 1:** Write the prereg, copying from spec sections 3, 5 and 6 exactly, plus the decisions below, which the spec left to the builder and which must be fixed before any data is fetched:
  - Samples: HOOD from the first quarter with two monthly releases before its report, through 2026Q3. MARA 2021Q3 through 2026Q3. COIN 2021Q2 through 2026Q3.
  - Quarter boundaries are UTC calendar quarters for MARA Pool blocks and Coinbase candles.
  - COIN volume = sum of daily USD volume (volume x close) over BTC-USD, ETH-USD, SOL-USD, XRP-USD, DOGE-USD, LTC-USD, ADA-USD, LINK-USD, AVAX-USD and USDT-USD. A product with no candle on a day adds 0. COIN ratio = mean of the last 4 prior quarters' (transaction revenue / volume).
  - HOOD drivers: month-3 flows = mean daily volume over published months x month-3 trading days (NYSE calendar for equities and options, calendar days for crypto and event contracts). The prior quarter's drivers are estimated the same way, so rates are consistent. Net interest = prior net interest x (mean of published month-end margin book + cash sweep this quarter / the same mean for the prior quarter). Other transaction revenue and other revenues carry at the prior quarter's level. A line absent in the prior quarter contributes 0.
  - MARA ratio (reported / on-chain) = mean over all prior quarters. Naive network share = MARA Pool blocks / all blocks, prior quarter. All blocks in a quarter = height at its end − height at its start (`mempool.space` blocks/timestamp). The network's average reward per block is approximated by the mean reward of MARA Pool's own blocks that quarter.
  - Synthetic strikes: centre = nowcast rounded to the nearest interval, strikes at centre + k x interval for k = −3..3. Brier is averaged over the 7 strikes and the scored quarters, for both model and naive baseline (the naive baseline gets its own error history and t model).
- [ ] **Step 2:** Add a line saying the prereg is committed before any KPI value, monthly figure or on-chain count has been typed or fetched, then commit.

```bash
git add research/kp1_kpi_prereg.md research/README.md
git commit -m "research: KP1 pre-registration (KPI binaries on public-data KPIs)"
```

### Task 2: Typed inputs and their validator

**Files:**
- Create: `research/kp1_inputs/actuals.csv`, `research/kp1_inputs/hood_monthly.csv`
- Create: `research/kp1_inputs.py`
- Test: `research/tests/test_kp1_inputs.py`

**Interfaces:**
- Produces: `load_actuals(path) -> dict[tuple[str, str], dict[str, float]]` keyed `(company, quarter)` such as `("HOOD", "2024Q3")`, each mapping a kpi name to its value in raw units, plus `report_date(company, quarter) -> date` via `load_report_dates(path) -> dict[tuple[str, str], date]`.
- Produces: `load_hood_monthly(path) -> list[MonthRow]`, where `MonthRow` is a frozen dataclass `(month: str "YYYY-MM", published: date, funded_customers: float, equity_notional: float, options_contracts: float, crypto_notional: float, event_contracts: float, margin_book: float, cash_sweep: float, sec_lending_revenue: float)`, raw units (customers in persons, $ in dollars).

CSV columns. `actuals.csv`: `company,quarter,report_date,kpi,value,printed,source_url`. KPI names: HOOD `funded_customers, revenue, arpu, rev_options, rev_equities, rev_crypto, rev_event_contracts, rev_other_transaction, net_interest, other_revenues`; MARA `blocks_won, bitcoins_mined, revenue`; COIN `transaction_revenue`. `hood_monthly.csv`: the MonthRow fields + `source_url`.

- [ ] **Step 1: Write the failing tests:** `test_actuals_rejects_duplicate_kpi_rows`, `test_actuals_report_date_consistent_per_quarter` (two rows for one quarter with different dates raise `ValueError`), `test_monthly_parses_units` (a "28.7" in millions with `printed` "28.7 million" stays as typed raw 28_700_000), `test_hood_revenue_lines_sum_to_total_within_1m` (run on the real `actuals.csv`: for every HOOD quarter the lines add to `revenue` within $1M), `test_every_row_has_https_source`.
- [ ] **Step 2:** Run `python -m pytest -q research/tests/test_kp1_inputs.py`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement the loaders. Then type the data: every HOOD monthly release in the sample and every quarterly figure in the KPI list, one row each, with the printed text and URL. Release dates come from the release's own dateline.
- [ ] **Step 4:** Run the tests. Expected: PASS. Any HOOD quarter whose lines don't sum is a typing error: fix the row, never the test.
- [ ] **Step 5:** Commit: `git commit -m "research: KP1 typed KPI history and HOOD monthly data"`

### Task 3: Fetch on-chain and exchange data

**Files:**
- Create: `research/kp1_fetch.py`
- Test: `research/tests/test_kp1_fetch.py`

**Interfaces:**
- Produces: `fetch_mara_blocks(get) -> list[tuple[int, int, int]]` returning (height, unix_ts, reward_sats), paging `/api/v1/mining/pool/marapool/blocks/{height}` downward until heights fall below 685000 (May 2021). `height_at(get, ts: int) -> int` via `/api/v1/mining/blocks/timestamp/{ts}`. `fetch_coinbase_daily(get, product, start: date, end: date) -> list[tuple[date, float]]` returning (day, usd_volume = volume x close), in 300-day windows. `get(url) -> object` is injected; `main()` uses `urllib.request` (User-Agent "agentdesk-research", no personal details) with a 1 s pause and writes `research/data/kp1/mara_blocks.csv`, `network_heights.csv` and `coinbase_daily.csv`, plus a copy in `/mnt/project-files/research/kp1/`.
- Produces: `quarter_bounds(q: str) -> tuple[int, int]` as unix seconds [start, end), UTC.

- [ ] **Step 1: Write the failing tests** with a fake `get`: `test_quarter_bounds_utc` (`quarter_bounds("2024Q1") == (1704067200, 1711929600)`), `test_mara_paging_stops_and_dedupes`, `test_block_at_boundary_goes_to_new_quarter` (ts == start of Q2 counts in Q2), `test_coinbase_windows_and_missing_days` (a product with gaps returns only the days present, and the volume is volume x close).
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement. Then run `python research/kp1_fetch.py` once.
- [ ] **Step 4:** Run the tests. Expected: PASS. Sanity check: the MARA block count for 2024Q4 is between 500 and 1,500.
- [ ] **Step 5:** Commit the code only (`research/data/` is gitignored).

### Task 4: Nowcasts and naive baselines

**Files:**
- Create: `research/kp1_kpi_nowcast.py` (nowcast section)
- Test: `research/tests/test_kp1_kpi_nowcast.py`

**Interfaces:**
- Consumes: Task 2 loaders and Task 3 CSVs.
- Produces: `nowcasts(company: str, quarter: str, data: Inputs) -> dict[str, tuple[float, float]]` mapping kpi → (model, naive), with `Inputs` a dataclass holding actuals, report dates, monthly rows, MARA blocks, network heights and Coinbase volumes. Helpers: `published_months(monthly, quarter, report_date) -> list[MonthRow]` (published < report_date and month inside the quarter), `trading_days(month: str) -> int` (NYSE weekdays minus a fixed 2021-2026 NYSE holiday list in the module).

- [ ] **Step 1: Write the failing tests** on synthetic inputs: `test_monthly_release_on_report_day_is_excluded`; `test_funded_nowcast_is_last_month_plus_its_change` (months 27.0m then 27.2m gives 27.4m); `test_hood_revenue_lines_scale_with_drivers` (double every driver with rates fixed: transaction lines double, net interest scales by the base ratio); `test_missing_prior_line_contributes_zero`; `test_arpu_uses_average_customers` (rev 1.0e9, start 25m, end 27m gives 4e9/26e6 ≈ 153.85); `test_mara_ratio_uses_prior_quarters_only`; `test_mara_naive_uses_prior_share_times_network_blocks`; `test_coin_ratio_is_mean_of_last_four`; `test_naive_growth_baseline` (prior 110 after 100 gives 121).
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement each nowcast and baseline exactly as in prereg section 5 and Task 1's decisions.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "research: KP1 nowcasts and baselines"`

### Task 5: Walk-forward scoring, gate and report

**Files:**
- Modify: `research/kp1_kpi_nowcast.py` (scoring + `main()`)
- Test: `research/tests/test_kp1_kpi_nowcast.py`
- Create (by running): `research/kp1_kpi_nowcast_results.json`, `research/kp1_kpi_nowcast.md`

**Interfaces:**
- Produces: `strike_interval(scaled: float) -> float`, `prob_at_or_above(k: float, nowcast: float, errors: list[float]) -> float`, `score_kpi(rows: list[tuple[str, float, float, float]]) -> dict` where rows are (quarter, actual, model, naive) in time order. Returns per-quarter errors, `scored` count, `median_abs_err` for model and naive, `coverage80`, `brier` for model and naive, and `passes_gate: bool`.

- [ ] **Step 1: Write the failing tests:** `test_strike_interval_tiers` (9.99 gives 0.01, 10 gives 0.1, 999 gives 1, 1000 gives 10); `test_t4_cdf_known_values` (T4(0) = 0.5, T4(2.132) ≈ 0.95 within 1e-3); `test_prob_is_half_at_bias_corrected_centre`; `test_fewer_than_four_errors_not_scored`; `test_gate_fails_when_no_better_than_half_naive`; `test_gate_fails_on_overconfident_intervals` (coverage below 60%); `test_errors_never_use_the_scored_quarter` (changing quarter q's actual leaves q's p unchanged).
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement scoring and `main()`. The report has one table per KPI: scored quarters, model and naive median |e| (as % and in strike intervals), 80% coverage, model and naive Brier, gate. Below that, a per-quarter appendix and the information rule and samples copied from the prereg. Wording follows earlier research reports (`research/r1_retail_search.md`): plain verdict per KPI, then caveats (small n, survivorship none, HOOD month-3 unobserved).
- [ ] **Step 4:** Run `python -m pytest -q research/tests`, then `python research/kp1_kpi_nowcast.py`. Expected: tests PASS and both outputs written.
- [ ] **Step 5:** Run `python -m pytest -q tests research/tests` once. Expected: all pass.
- [ ] **Step 6:** Commit the code, tests and outputs, push the branch to PR #72, update the PR body with the per-KPI verdicts, and copy the report to `/mnt/project-files/research/kp1/`.
