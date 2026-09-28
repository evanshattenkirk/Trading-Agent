# New crew desks: Ops, Earnings, Post-mortem (design)

Date: 2026-09-28. Builds on draft PR #4 (08:15 catch-up, 08:25 huddle). Evan asked for "new team members"
as part of tonight's goal; the coordinator scoped it to 2–4 desks that only add information or reduce risk.
Evan's merge of the draft PR is the approval.

## Intent

- Three new desks, each with one job the current seven don't do.
- **They can only restrict or inform.** None can vote above 1.0x, pitch proposals, create event blackouts,
  lift a halt or place an order. Code enforces this; tests pin it.
- No strategy rule changes for books A–E. The only HANDOFF change beyond section 5 is who runs book E's
  earnings screen (Vol → Earnings), which Evan confirms by merging.

## The desks

| Desk | Key | When | Source | Vote |
|---|---|---|---|---|
| Ops | `ops` | 08:15 catch-up, 08:25 huddle, halts | Engine state, config, recorder files (deterministic) | 1.0, or 0.5 when 15m/5m history isn't warm |
| Earnings | `earnings` | 08:15 catch-up, 08:25 huddle | Robinhood `get_earnings_calendar` (read-only), else `crew.earnings.calendar` | Always 1.0 (information only) |
| Post-mortem | `postmortem` | 15:05 post-close | Today's closed trades, engine skips, risk state (deterministic) | Always 1.0 (day is over) |

### Ops (pre-flight, section 12)

Checks at the catch-up, each `{name, ok, detail}`:
1. Mode: paper/shadow/sim, or live with `live_enabled` (reported, never changed).
2. Every `books.*` entry has `paper_only: true`.
3. Risk limits armed: `max_daily_loss > 0`, `max_trades_per_day > 0`, watchdog keys present.
4. Not halted at startup (else reports the reason; Risk still owns the halt).
5. Signal history warm: 15m and 5m MACD have at least `crew.ops.min_warm_bars` (default 50) closed bars.
6. SPY price present (feed connected).
7. Robinhood connected for option quotes and Level 2 (skipped in sim).
8. Recorder: the previous session recorded option quotes (`~/.agentdesk/recorder/days.jsonl` from PR #2 if
   present, else `journal.option_quotes`). Skipped in sim.

Vote: 0.5 only when check 5 fails (MACD on too few bars is not the signal Evan trades). Everything else is
reported, never acted on: the engine watchdog and Risk already own halts.
At a halt huddle Ops joins Risk and Quant and re-runs checks 6–7 so the cause is visible.

### Earnings (book E's screen, plus SPY heavyweights)

- Pulls a 31-day `get_earnings_calendar` window (`filter: high_market_cap`). Verified response shape
  2026-09-28: `results[] = {symbol, report: {date, timing: am|pm|"", verified}, eps: {estimate, actual}}`.
- **E screen** (pure function `agentdesk/earnings.py:screen`): for each name in the E universe, trading days T
  from today to the announcement (weekdays minus `calendar.holidays`). Flags per HANDOFF 7E:
  E2 entry window T−10..T−8, E1 entry at T−3, exit due T−1 (am reporters) or T−0 (pm reporters).
  Unverified dates are marked tentative. It doesn't size, filter by spread or VIX, or trade: book E does that.
- **Heavyweights:** a SPY top-weight name that reported after yesterday's close or reports before today's
  open gets a note ("NVDA reported last night; expect a gap"). Information only.
- Earnings never become blackouts (earnings aren't in the HANDOFF blackout list): the desk emits no `events`.
- Universe: `crew.earnings.universe` (30 liquid large caps, a starting list the book E thread can replace).
- Offline/sim: `crew.earnings.calendar` entries `{symbol, date, timing}`; the simulator seeds two names so the
  demo shows an E1 and an E2 flag.

### Post-mortem (rule-adherence audit)

At 15:05 it audits each closed trade against the rules Evan set, then writes
`~/.agentdesk/postmortems/YYYY-MM-DD.md` for the weekly Quant report (not in sim):
- opened inside the entry window and outside every blackout;
- contracts ≤ `max_contracts` × `max_size_multiplier` (6);
- closed by `flatten_at`;
- loss no worse than the stop plus 10 points of slippage (e.g. −30% for a −20% stop);
- flags a round trip: peak ≥ +50% that closed at a loss.
Plus: skipped entries by reason, the day's size multiplier and who cut it.
A rule break is a possible engine bug, so the headline says so; the desk changes nothing.

## Schedule and huddles

- `PREMARKET_DESKS` becomes Macro, Rates, Fed, Vol, Ops, Earnings. Ops and Earnings prepare at 08:15 like
  the others; the 08:25 huddle still finishes before the open (offline everything is instant; online the
  existing "late desk uses its offline read" rule covers a slow calendar call).
- Post-close: Quant, Vol, Post-mortem. Halt: Risk, Quant, Ops.
- Templated roundtable adds: Ops → Risk (pre-flight result), Earnings → Vol (E screen), Post-mortem → Quant.
  The online roundtable sees the new briefs; any vote it returns for a new desk is clamped to ≤ 1.0.

## Enforcement (code)

`RESTRICT_ONLY = {"ops", "earnings", "postmortem"}` in `crew.py`:
- `_finish` clamps their `size_multiplier` to ≤ 1.0, drops `proposals`, and drops `events` for Earnings.
- Roundtable votes for them are clamped the same way; roundtable proposals attributed to them are dropped.
- They are not in `VOTERS_FOR_SIZE_UP`. A cut from them does block a size-up, via the existing
  "No desk voting down" check.

## Office and dashboard

Side columns get a third desk each (left: Macro, Rates, Earnings; right: Fed, Vol, Ops); Post-mortem joins the
bottom row. `app.js` desk list gets the three names and colors. Demo rebuilt with `tools/build_demo.py`.

## Tests (TDD)

`tests/test_crew_desks.py`: E screen windows and holiday counting; calendar parsing of the verified shape;
Ops checks and the 0.5 warm-up vote; Post-mortem rule checks on synthetic trades; restrict-only clamps on
brief, roundtable vote and proposals; new desks in the 08:15 catch-up and the huddle ends before 08:30;
no `books.*` or size key reachable.

## Not in scope

- A Gamma/dealer-positioning desk: needs open interest by strike, which nothing records yet. Revisit once the
  recorder or ThetaData has OI.
- Changing any vote rule, size-up check, blackout rule or book rule.
