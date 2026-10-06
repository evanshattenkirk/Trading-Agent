# Book H (overnight 1DTE SPY iron fly): design

Date: 2026-10-06. Status: **draft for Evan's approval in the "Find a profitable strategy for a new book" project
thread. Nothing here is built until H4 passes its pre-registered real-quote test** (`research/book_h_candidates_prereg.md`
section 4, run on the Mac with `research/h4_overnight_fly.py`). If H4 fails, this spec is dropped.
Source of truth: HANDOFF v3 sections 2 (broker facts), 9 (multi-book spec), 10.8 (promotion), 11 (sizing), 12 (safety).
Already decided by Evan: paper only, $10,000 paper balance, $300 max loss per position, $2,500 account open-risk cap,
fills at mid −1¢ per leg (never worse than natural), $0.04 fee per leg.

## 1. The rule (exactly the one H4 tests; nothing added)

- **Nights:** every session d whose next trading session e has a listed SPY expiry, except a 13:00 ET close on d and
  the night before an SPY ex-dividend date (the third Friday of March, June, September and December). Since
  November 2022 SPY lists an expiry every session, so that is about 245 nights a year, Friday to Monday included.
- **Entry, 14:50 CT (15:50 ET) on d:** sell the round(SPY) call and put expiring on e, buy the call $5 above and the
  put $5 below. One 4-leg credit order, 1 lot.
- **Exit, 08:45 CT (09:45 ET) on e:** buy all four back. No take-profit, no stop: nothing trades overnight and the
  wings cap the loss.
- **Risk per lot:** 100 × ($5 − credit) + fees. At recent prices the credit should be roughly $3–4 (an estimate;
  the replay reports the real figure), so about $100–200 a lot, inside the $300 per-position cap.

Why it might work: options are priced in calendar time but the market moves far less overnight than during the
session, so short-dated options tend to lose more value overnight than the overnight move costs the seller (Jones &
Shemesh, JF 2018; Muravyev & Ni, JFE 2020). Unlike D, whose credit is about 18¢, the credit is large next to SPY's 1–2¢
spreads (about 8¢ per share in round-trip costs at mid −1¢ against an estimated $3–4 credit), so costs alone are
unlikely to erase it. H4 measures whether that
holds on 2016–2026 SPY quotes.

## 2. Decisions that need Evan's OK

| # | Question | Recommended (what gets built) | Why |
|---|---|---|---|
| H-Q1 | **HANDOFF section 2 says "never hold short legs into the close".** H holds two short legs overnight by design. | Allow it for book H only: the shorts expire the next session (not 0DTE), the $5 wings cap the loss, ex-dividend eves are skipped (a short in-the-money call is the early-assignment risk), and it is paper only. Book E's calendar already holds a short weekly overnight (E2) on the same footing. | Without this H can't exist. Nothing real is ever left open in paper. |
| H-Q2 | **Shutdown sells everything** (Evan, 2026-09-28) and the engine stops at 15:10 CT, 20 minutes after H enters. | H positions are **not** sold at shutdown: saved in `h_positions`, restored the next morning, exactly like E and F2. The kill switch, a safety halt, an H halt and the dashboard Flatten button still sell H. | Same reason as E-Q1. |
| H-Q3 | H enters after the engine's 14:40 CT `flatten_at`. | H is exempt from `flatten_at` (its contracts expire tomorrow). The per-contract rule "5 minutes before Robinhood's sellout" still applies on e. | `flatten_at` exists for 0DTE contracts. |
| H-Q4 | Engine down or quotes missing at 08:45 CT on e. | Exit at the first minute from 08:45 CT with all four legs quoted; from 09:00 CT work the exit as urgent (natural first). If the engine starts after 08:45 CT, it exits on its first quote. | The replay uses the first fully quoted minute up to 10:00 ET; past that the position is out of the tested rule. |
| H-Q5 | Size and limits. | 1 lot; no day-loss limit of its own (one position at a time; its max loss is the limit); counts toward the $2,500 open-risk cap overnight together with E and F2. | The crew can't change any of these (HANDOFF section 5). |
| H-Q6 | Crew. | No desk vote reaches H (like E and F). The shared entry gate still applies at 14:50 CT (halts, pause, blackouts active at that minute). | The research rule has no crew input; adding one would be an untested filter. |
| H-Q7 | Data for later checks. | Optional, separate small change: the quote recorder also saves the next expiry's ATM±10 quotes from 14:40 to 15:00 CT, so paper fills can be compared with the replay. | Lets the weekly Quant report score H on real quotes. |

## 3. Components

- `agentdesk/books/overnight_fly.py`: H's rules as pure functions (night check, ex-dividend dates, legs with
  `dte` = calendar days to the next session: 1 Monday–Thursday, 3 Friday, more over holidays, entry and exit times).
  No I/O, unit-tested.
- `agentdesk/books/h_host.py`: `HHost`, H's own host next to `BookHost`, `FHost`, `EHost` and `F2Host`, joined through
  `HostGroup`. It enters at 14:50 CT through the existing `ComboExecutor` / `PaperBroker.submit_combo` (mid −1¢ per
  leg, never worse than natural), quotes its open legs every 15 s itself (so it stays out of the engine's 10-second
  quote watchdog, as F2 does), exits at 08:45 CT, and persists across restarts (pattern: `F2Host`).
- `agentdesk/books/h_journal.py`: `h_positions` (every H position as JSON, open rows restored at startup). Closed H
  trades go to `trades` with `book='H'`.
- `lifecycle.py`: H joins E and F2 in the "kept at shutdown" list.
- Config: `books.H_overnight_fly: {enabled: true, paper_only: true, lots: 1, entry_ct: "14:50", entry_grace_min: 5,
  wings: 5, exit_ct: "08:45", urgent_after_ct: "09:00", skip_ex_div: true}`.
- Dashboard: H appears in the book strip and positions list (no new card); an open H position shows its exit time.
- Shadow mode: the entry is sent to `review_option_order` like every other book. One shadow day on the Mac confirms
  that Robinhood accepts a next-day 4-leg opening order at 15:50 ET (only same-day contracts are documented to stop
  opening at 15:30 ET).

## 4. Error handling

- Missing or stale leg quotes at entry, or a leg wider than the fill rule allows: skip the night and log the reason
  to the activity feed (no new table).
- Unreadable `h_positions` row at startup: halt book H only (as F2 does).
- A failed position check counts toward H's `max_consecutive_errors` like every book; H then halts and flattens itself.

## 5. Tests (written first)

- Night rules: weekday, Friday→Monday, holiday weekends, half-day skip, ex-dividend eve skip, no expiry listed.
- Legs and `dte` resolution; credit direction on the 4-leg order; max-loss sizing against the $300 cap.
- Entry at 14:50 CT inside the grace window only; one position at a time.
- Persistence: open position saved, restored after a restart, not sold at shutdown; sold by kill, halt and Flatten.
- Exit at 08:45 CT; the 09:00 CT urgent fallback; exit on the first quote when the engine starts late.
- Exempt from `flatten_at`, not from the sellout rule.
- A sim run spanning two sessions; the full suite still passes.

## 6. Acceptance and promotion

- Built: all tests pass, a two-session sim run opens and closes one H position, and one shadow review passes.
- Promotion (HANDOFF 10.8, Evan decides): at least 20 paper sessions and 100 trades, PF ≥ 1.1 at taker fills, and
  paper results inside the replay's range.
