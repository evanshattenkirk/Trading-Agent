# Book E (pre-earnings IV run-up) and the IV recorder: design

Date: 2026-09-29. Status: **draft, waiting for Evan's approval in the "Book E and IV recorder" project thread.**
Source of truth: HANDOFF v3 section 7E (rules), 9 (build spec), 10 step 5 (acceptance), 11 (sizing), 12 (safety).
Already decided by Evan: paper only, $10,000 paper balance, E1 max debit $500, E2 max debit $250, fills at mid
−1¢ per leg (never worse than natural), $1,500 account open-risk cap, $0.04 fee per leg.

This document decides how E and its recorder plug into the code. Section 2 lists the places where HANDOFF is silent
or conflicts with a later decision; each one needs Evan's OK before code is written.

**Acceptance (HANDOFF 10.5):** the earnings screen lists candidates daily (already true: the Earnings desk, PR #14)
and `iv_history` grows every trading day.

## 1. What this delivers

- `agentdesk/iv.py`: shared read-only option-chain helpers (expirations, listed strikes cached on disk, batched quotes
  with IV) and the expiry pickers used by both the recorder and book E.
- IV recorder: an end-of-day pass inside the standalone quote recorder (`record-quotes`, launchd) that writes
  `iv_history`. Also `python -m agentdesk iv-snapshot` for a one-off manual run.
- `agentdesk/books/earnings_iv.py`: E's rules as pure functions (legs, windows, exit day, take profit, stop, filters,
  sizing). No I/O, fully unit-tested.
- `agentdesk/books/e_host.py`: `EHost`, E's own host next to `BookHost` (B/C/D/G) and `FHost` (F), joined through
  `HostGroup`. It reads the Earnings desk's screen, prices entries, works paper fills through the existing
  `ComboExecutor`, manages open positions and persists them across restarts.
- Journal: `iv_history`, `e_positions` (open positions that survive a restart) and `e_decisions` (every candidate
  evaluated, traded or skipped, with its reason). Closed E trades go into `trades` with `book='E'`.
- Config: `books.E_earnings_iv` gains the fields in section 5; `recorder.iv` gains the recorder's settings.
- Dashboard: E shows up in the existing book strip and positions list (no new card).
- Tests: every E item in HANDOFF 9's required list (take profit / stop / time exits, never hold through earnings),
  plus the recorder's pacing and read-only guard.

Out of scope: an E backtest on ThetaData quotes (HANDOFF 10.7d; a follow-up once this is merged), any live path for E.

## 2. Decisions that need Evan's OK

| # | Question | Recommended (what gets built) | Why |
|---|---|---|---|
| E-Q1 | **Shutdown sells everything** (Evan, 2026-09-28), and the daily paper engine stops at 15:10 CT. E holds 1–8 trading days by design, so selling at shutdown would close every E position the day it opens. | E positions are **not** sold at shutdown. They are paper rows persisted in `e_positions` and reloaded the next morning. Book A and B/C/D/F/G are still sold at shutdown as today. The kill switch, a safety halt, an E halt and the dashboard Flatten button still sell E. | Without this E can't run at all. E has no live path, so nothing real is ever left open. |
| E-Q2 | **E2's short leg usually expires before E2's exit.** HANDOFF: short the weekly expiring before earnings, exit at T−1. Most names list Friday weeklies and report Tue–Thu, so that weekly expires at T−2 to T−4. | E2 exits at the **earlier** of its T−1 (T−0 for after-close) exit and the short leg's expiry day. On a short leg's expiry day E2 closes by **14:15 CT**, before Robinhood's 14:30 CT at-risk closeout. | Keeps both hard rules: never hold through the announcement, never hold a short leg into expiry. Alternative: skip E2 whenever no weekly expires between T−1 and the announcement, which skips most events. |
| E-Q3 | "At the close" has no clock time; the engine runs until 15:10 CT and equity options stop at 15:00 CT. | E1 entries and all normal exits at **14:45 CT**; half-days (`calendar.early_close`) at **11:20 CT**. | 15 minutes before the close, same idea as the other books' cutoffs. |
| E-Q4 | HANDOFF puts the IV snapshot at 15:00 CT, which is the options close; quotes after it go stale. | The quote pass starts at **14:50 CT** (chain listings are warmed from 13:30 CT). | Same end-of-day intent with live quotes. |
| E-Q5 | "Earnings-expiry IV above its recorded 80th percentile, once history exists": percentile of what? | Today's earnings-expiry ATM IV ranked against the same symbol's earnings-expiry IV recorded at the **same T** in earlier earnings cycles. The filter turns on per symbol once **4 earlier cycles** exist (about a year); until then it is off and each decision logs "IV filter: n/4 cycles". | IV climbs every day into earnings, so only the same T is comparable. |
| E-Q6 | "ATM spread no wider than 5% of mid" is a universe rule. | Checked on **every leg at entry**: (ask − bid) ≤ 5% of mid. Open positions only need fresh, uncrossed quotes (as B/C/D/G). | The universe is the config list; this makes liquidity a live check. |
| E-Q7 | Robinhood marks some dates unverified. | Unverified dates are traded. The screen is re-read every day; if the date moves later the exit moves with it; if it moves earlier E exits at the next check. | Most dates are unverified at T−10, so skipping them would starve E2. |
| E-Q8 | Do SPY macro blackouts (CPI, FOMC) block E? HANDOFF 9 applies them to "every 0DTE book". | No. The dashboard Pause, kill switch, halts and crew size cuts do apply. | E isn't a 0DTE book. |

Sizing note (no decision needed): at $500, E1 can trade only a straddle priced ≤ $5.00, about a stock near $100 with a
5% implied move. Most of the 30 names will skip E1 for cost. Every skip is logged in `e_decisions` with its debit, and
the recorder keeps those straddle prices, so a later analysis can still score them.

## 3. Rules as built (HANDOFF 7E, with section 2's defaults)

**Candidates.** `crew.earnings.universe` (30 names). The Earnings desk's screen (`earnings.screen`) flags each name
daily: `E2` at T−10..T−8, `E1` at T−3, `exit` at T−1 (T−0 for after-close, T−1 when timing is unknown). EHost reads the
same cached calendar the desk loaded (`desks.load_calendar`), so there is no extra Robinhood calendar call.

**Expiries** (from the listed expirations, announcement date D):
- pre-announcement expiry: am or unknown timing: last expiry < D; pm: last expiry ≤ D.
- post-announcement expiry: am: first expiry ≥ D; pm or unknown: first expiry > D.
- Strike: the listed strike nearest spot that exists in every leg's expiry.

**E1, straddle.** At T−3, 14:45 CT: buy the ATM call and put in the post-announcement expiry, which must be 4–10
calendar days out on the entry day (else skip). Debit order, lots = floor($500 / debit×100), skip if 0.
Take profit when the mid value ≥ entry × 1.20, stop when ≤ entry × 0.70. Exit on the exit day at 14:45 CT.

**E2, calendar.** On T−10, T−9 or T−8 at 14:45 CT (first day that fills; a skip retries the next day in the window):
sell the ATM call in the pre-announcement expiry, buy the same-strike call in the post-announcement expiry, one 2-leg
debit order. The short leg must expire after the entry day. Lots = floor($250 / debit×100). Take profit +15%, stop
−30% on the mid value. Exit at the earlier of the exit day 14:45 CT and the short leg's expiry day 14:15 CT (E-Q2).
Calls are used for the calendar; HANDOFF names no right, and one right keeps it a 2-leg order.

**Filters (both).** Skip if the prior VIX close > 30. Skip if the earnings-expiry IV percentile > 80% (E-Q5). Skip a
leg quote that is stale (> 5 s), crossed, zero-bid or wider than 5% of mid (E-Q6).

**Limits (both).** Max 3 E positions open, at most one per sector (GICS map in config), one per symbol. Every entry also
passes `AccountRisk.can_open` (the $1,500 open-risk cap across all books, buying power). E's own max debit replaces
`per_position_max_loss` ($300, a B/C/D number) for E. A crew size cut (< 1×) rounds lots down, never below 1; no size-up.

**Never through the announcement.** On the exit day the position is closed at the exit time, retried every poll until
filled; an urgent close works from natural. If the engine starts after the exit time on or past the exit day and
before the announcement, E closes at once. If it starts after the announcement has already happened (the engine was
down), E closes at the first usable quote, marks the trade `held through announcement (engine down)` and logs an error.

**Management.** Open E legs are quoted in one batched `get_option_quotes` call every 15 s during 08:30–15:00 CT. Take
profit and stop use the combo mid and only fire on fresh, uncrossed quotes on every leg.

## 4. IV recorder

Runs inside `RecorderDaemon` (the launchd `record-quotes` job), so it records every weekday whether or not the engine
runs, with the recorder's own Robinhood grant. It stays read-only: `get_earnings_calendar` joins `READ_ONLY_TOOLS`,
and every other call it makes is already on that list.

- **13:30 CT, listing warm-up** (≤ 0.5 calls/s): per symbol, expirations (`get_option_chains`) and the listed
  strikes of each needed expiry (`get_option_instruments`), cached on disk under `~/.agentdesk/cache/iv/`. An expiry's
  strike list rarely changes, so after the first day only new weeklies are listed.
- **14:50 CT, quote pass** (≤ 1 call/s): one `get_earnings_calendar` (31 days, the desk's args), one
  `get_equity_quotes` for all 30 spots, then one `get_option_quotes` per symbol for the ATM call and put in up to four
  expiries: `front` (nearest after today), `pre` and `earn` (the pre- and post-announcement expiries, when a report is
  within 31 days) and `d30` (closest to 30 calendar days). About 32 calls, roughly 40 s.
- **Rate budget:** the SPY 0DTE poll uses 2 calls per 10 s (0.2/s). With the IV pass capped at 1/s the process stays
  at or below 1.2 calls/s, under the shared ~2 calls/s budget. Both run in one loop, so they never burst together.
  Every call is metered into `rh_calls` with tag `iv`.
- A failed symbol is retried until 15:00 CT; the day's row count goes into `days.jsonl`. Rows are keyed
  (day, symbol, kind), so a rerun replaces rather than duplicates.
- IV comes from `get_option_quotes` (HANDOFF 2 says it returns IV and greeks). If the field is missing, IV is stored as
  null next to bid/ask, so it can be computed later.

`iv_history` columns: `day, ts, symbol, kind, expiry, dte, strike, spot, call_bid, call_ask, call_iv, put_bid,
put_ask, put_iv, atm_iv, earnings_date, earnings_timing, T`.

Deploying it needs the recorder reinstalled on the Mac (`tools/install_recorder.sh`), sent to Evan as one Terminal
command after merge. Two response shapes (`get_option_chains` expirations and the quote IV field name) haven't been
seen yet; the first run logs them and the parser accepts the likely field names. Nothing trades on the recorder.

## 5. Config

```yaml
books:
  E_earnings_iv: {enabled: true, paper_only: true, max_debit: 500, max_debit_e2: 250, max_open: 3,
                  structures: [straddle_t3, calendar_t10], never_hold_through_announcement: true,
                  entry_ct: "14:45", exit_ct: "14:45", expiry_day_exit_ct: "14:15", half_day_ct: "11:20",
                  e1_dte: [4, 10], take_profit: {straddle_t3: 0.20, calendar_t10: 0.15}, stop_pct: 0.30,
                  vix_max: 30, iv_pct_max: 0.80, iv_min_cycles: 4, max_leg_spread_pct: 0.05, poll_sec: 15,
                  sectors: {tech: [AAPL, MSFT, NVDA, AVGO, ORCL, CRM, ADBE, AMD, INTC, QCOM],
                            comm: [GOOGL, META, NFLX, DIS], discretionary: [AMZN, TSLA, HD],
                            financials: [JPM, V, MA, BAC], health: [UNH, LLY, JNJ], energy: [XOM],
                            staples: [COST, WMT, PG, KO, PEP]}}   # GICS; a name missing here can't trade
recorder:
  iv: {enabled: true, list_from_ct: "13:30", quote_ct: "14:50", stop_ct: "15:00", list_calls_per_s: 0.5,
       quote_calls_per_s: 1, cache_dir: ~/.agentdesk/cache/iv}
```

The crew's proposals whitelist already can't touch `books.*`; a test confirms it covers the new E fields.

## 6. Engine integration

- `HostGroup` takes a list of hosts instead of exactly two, with one shared `AccountRisk` and open risk summed over
  all of them (A + B/C/D/G + F + E). No behavior change for the existing books.
- `EHost.flatten(reason)` skips when `reason == "shutdown"` and logs "book E keeps N paper positions overnight";
  `lifecycle._held` leaves persisted E positions out of the "still open" error. Every other flatten sells E.
- `e_positions` stores each open position (legs, contracts with symbol/expiry/strike/broker id, entry, qty, fees,
  event date and timing, exit plan). It's written on open, fill and close; startup restores open rows. Sim mode uses
  an in-memory journal, so nothing persists there.
- E always fills through its own `PaperBroker` (as `BookHost` does), never `engine.broker`, so E stays paper even
  when book A is promoted and the process holds a live Robinhood broker. In shadow the reviewer only calls
  `review_option_order`. `build_e` refuses to start unless `paper_only: true`.
- Restore before trading: `EHost.start` loads `e_positions` before the first hook runs, so restored positions count in
  the account open-risk cap and the max-3/sector limits before any new entry. Paper fills return synchronously, so a
  row is written only after a known fill (no uncertain order state). A partial fill blocks new E entries until the
  position is reconciled, as in B/C/D. A row that can't be parsed halts E and is logged.
- In sim mode E stays idle and says so ("no single-stock option data in sim"); tests drive E with fake quotes.
- E's hooks run under `EHost.run` with its own error counter (as F); `max_consecutive_errors` in a row halts and
  flattens E only.

## 7. Tests (written first)

- Expiry pickers for am, pm and unknown timing; E1's 4–10 DTE check; strike present in both expiries.
- E1 and E2 legs, debit direction, sizing from each max debit, skip when one lot is too expensive.
- Windows: E2 on T−10..T−8 with retry, E1 only at T−3, the exit day for am/pm/unknown, E2's short-expiry exit at
  14:15 CT, half-day times.
- Take profit and stop per structure; nothing fires on a stale or crossed leg.
- Filters: VIX > 30, IV percentile over 80% with ≥ 4 cycles, off below 4 cycles, 5% leg spread.
- Limits: max 3 open, one per sector, one per symbol, account cap and buying power, crew cut floor.
- Never through earnings: exit on the exit day; restart after the exit time; restart after the announcement closes
  and flags the trade; a moved date moves the exit.
- Shutdown keeps E and still sells the other books; kill switch and Flatten sell E.
- Persistence round trip through `e_positions`; restored risk blocks an entry that would pass the cap without it.
- E never calls `engine.broker`, even with a live broker attached (A promoted).
- Recorder: picks front/pre/earn/d30, writes `iv_history` idempotently, paces ≤ 1 call/s and ≤ 0.5 calls/s in the
  listing phase (fake clock), refuses non-read-only tools, retries a failed symbol.
- The existing test suite still passes.
