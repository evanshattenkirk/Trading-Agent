# Round 6 follow-up: PM6, a resting bid on the modal Bitcoin bracket, filled from the trade tape (pre-registration)

Status: **FROZEN 2026-11-18.** Evan picked "Maker test with real fills" on the thread's decision card (2026-11-18
21:35Z) after round 6 (`round6.md`, PR #73). This file is committed before any trade-tape row is pulled or any PM6
outcome is computed; a later change is an amendment with its own date. Research only: no book, rule or engine
change. The engine can't trade event contracts (Robinhood's MCP has no prediction-market tools), so a pass is
information for Evan.

**Why.** PM4 bought the modal Bitcoin hourly bracket at the ask and lost 4.0% a contract. Its maker row, the same
bracket bought at the bid with no fee, made +18.3% (t 4.93, both halves positive), but candles can't say whether a
bid would have filled, and a resting bid tends to fill when the price moves against it (book D's fill study,
`d_fill_quality.md`). PM6 asks whether the bid still earns once fills are taken from the trade tape. Because PM6
exists only because of a reported-only row, it keeps round 6's 2.58 bar rather than a single-test bar.

What was looked at before this file was frozen: everything in `round6.md` and `round6_posthoc.json` (including
PM4's maker row); the trade-tape fields of three markets (no fill rule evaluated); the series' current fee type
(`quadratic`: no maker fee).

## Rule

- **Hours and bracket:** PM4's exactly: the 1,486 sampled `KXBTC` hours (`research/data/round6/btc_sample.json`,
  paired from `KXBTCD`), entry E = close − 15 min, the modal bracket among the six `between` markets whose
  midpoint is nearest the Coinbase spot at E, from the yes bid/ask at E (round 6's `quote_at`).
- **Order:** at E, a limit buy of YES at the yes bid b (b in [0.02, 0.98]), resting until the market's close.
- **Fill (trade-through):** filled at b if any trade on that market printed after E and at or before the close
  at a yes price strictly below b. A trade below the bid can only happen after a resting bid at b has been
  filled (price priority), whatever the queue at b. Trades at exactly b don't count (queue position unknown).
  The tape is `/markets/trades` (live set) or `/historical/trades` (archived), `ticker`, `min_ts` = E,
  `max_ts` = close.
- **Return per filled contract** = (payout − b) / b, payout 1 or 0, held to settlement, no fee (the series'
  `quadratic` fee type charges takers only; fee history isn't modelled). Unfilled orders are no trade.
- **Inference:** one order per hour, so the event mean is the order's return; t over filled hours; halves by time.

**PM6 passes only if all hold:** t ≥ **2.58** over filled hours; a positive mean in each half; and the average
P&L per order posted (unfilled = 0, in cents per contract) is positive.

**Reported, never selected:** fill rate; fill at the touch (a trade at ≤ b whose taker sold YES, i.e.
`taker_side` = `no`), which assumes front of queue; E = close − 45 min; the modal bracket's two neighbours
(each its own order); a hypothetical 0.0175 maker fee rate (`quadratic_with_maker_fees` series charge it);
returns by fill time (first vs last 5 minutes).

## Order of work

1. This file committed on branch `claude/project-thread-e7nhpk`.
2. `research/round6_maker.py` (orders, tape pull into `research/data/round6/trades/`, report) with synthetic tests.
3. Results appended to `research/round6.md` as a PM6 section, pass or fail.
