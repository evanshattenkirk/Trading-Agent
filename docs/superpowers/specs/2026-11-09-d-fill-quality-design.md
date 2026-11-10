# Book D fill-quality study: design and pre-registration

Status: draft for Evan's review, 2026-11-09. Thread "Improve fills and trim no-edge books". Research only: no engine,
config or order change. Nothing here calls an order tool.

## 1. Why

Book D (10:00 ET SPY 0DTE iron condor, shorts at 0.9x the expected move, $2 wings, TP 50%, stop 2x credit, close
15:25 ET) was replayed on real 1-minute NBBO quotes for 2016-01-08..2026-09-25 (`research/bd_real_quotes.py`,
verdict in the project's `bd-real-quotes-verdict.md`):

| Fill model | Mean per trade (% of max risk) | t |
|---|---|---|
| mid | +0.81% | 2.87 |
| patient (mid -1c a leg, never worse than natural; the paper rule) | -1.45% | -5.04 |
| taker (bid/ask) | -1.97% | -6.98 |

D's credit is about $18 on a $2 condor, so the whole verdict turns on about one cent a leg. Every research round so
far has assumed one of these three fill models. None was measured. This study measures what a resting limit order
that works toward the far side actually gets, and re-scores D with that cost.

Evan scoped it to D only (2026-11-09). B and C were turned off the same day (PR #66).

## 2. What we already know

- ThetaData `option_history_trade_quote` returns every option trade with the NBBO (and displayed sizes) at that
  moment. A probe on 2026-11-06, 09:45-09:50 ET, all SPY 0DTE strikes: 51,177 trades; 85% printed while the spread
  was 1c wide, 13% at 2c.
- On a 1c market, mid -1c is the natural price, so "patient" there equals taker. The only possible improvement is
  to rest on the passive side (sell at the ask, buy at the bid) and get hit. About 46% of prints on 1c markets are at
  the bid and 48% at the ask, but the displayed size at that price is often 500-2,000 contracts, so queue position
  decides whether a resting order fills.
- The paper journals are not evidence about fills: `PaperBroker.submit_combo` computes every fill from the quote, so
  they hold no information about whether a real order would have filled. They are not used.

## 3. Question

Pre-registered: **at the per-leg execution cost a realistic resting-then-walking limit order achieves, does book D
make money?**

## 4. Part 1: measure the cost per leg

**Events.** For each D session in the sample (section 6), the four D legs (strikes from the existing replay's
`legs_D`, so the same contracts the verdict traded), in two windows:
- entry: decision time 10:00:00 ET, sell the two shorts and buy the two wings;
- close: decision time 15:20:00 ET, buy back the shorts and sell the wings, finished by 15:28:00 ET at the latest.

Each leg is one order for one contract, worked on its own. (Real execution would be a 4-leg combo order; see
section 8.)

**Order policy (the walk).** At the decision time, the limit starts at the price nearest the mid on the passive side
(1c market: the passive side; 2c: the mid; 3c or wider: the mid rounded toward the passive side). It holds there for
H seconds, then moves 1c toward the far side every 15 s. Once it reaches the natural price it is marketable and fills
there. The primary policy is **H = 60 s**. H = 15 s and H = 300 s are also reported. In the close window, anything
unfilled at 15:28:00 crosses at the natural price.

**Fill rule while resting at limit L** (shown for a sell; a buy is the mirror image). Market data has no record of
our order, so a fill is inferred from the real tape:
- *Queue (primary):* when the order rests at L, the queue ahead of it is the displayed size at L if L equals the
  current ask, and 0 if L is inside the spread. It fills when (a) a trade prints above L, or (b) the bid rises to L or
  higher, or (c) the total size traded at exactly L since the order arrived exceeds the queue ahead of it. A reprice
  resets the queue. A doubled-queue version is part of the pass rule (section 7).
- *Optimistic:* any trade at L or better fills it.
- *Strict:* only (a) or (b). On a 1c market this is close to always paying the spread, so it is a floor, not a
  test.

**Cost.** Cost per leg is measured in cents against the mid at the decision time: fill - mid for a buy, mid - fill
for a sell. It includes any drift while the order waits, so adverse selection is counted. Costs are pooled by window
(entry/close), side (buy/sell) and spread width at the decision time (1c, 2c, 3c+). The study also reports fill rate
before the walk reaches natural, time to fill, and the cost against the mid at the moment of the fill.

## 5. Part 2: re-score D

Run `bd_real_quotes.replay` for D unchanged, except with one new fill model, **measured**. Each leg's price at each
minute is mid ± c, using c from Part 1 (queue rule, H = 60 s) for that leg's window, side and spread width. The price
is capped at the natural price and can't be better than the passive side. Entry legs use the entry costs. Take-profit
and 15:25 time exits use the close costs. **Stop exits pay taker** (a stop is urgent). Everything else is as in the
verdict: same sessions, rules, fees ($0.04 a leg a side), no quiet filter.

Also reported: D at a uniform cost c (-0.5c to +1.0c a leg in 0.25c steps), giving the break-even cost; the
doubled-queue, optimistic and strict fill rules; H = 15 s and 300 s; and D with the tradeable quiet filter (09:30-10:00 range).

## 6. Sample

All D sessions from the verdict replay (2016-01-08..2026-09-25) if the tick pull for both windows finishes within
12 hours of pull time. If it doesn't, the sample is a seeded random draw (seed 20261109) of 500 sessions, stratified
by year in proportion to each year's D sessions. Part 2 always runs on every session, using Part 1's pooled costs.

Data: ThetaData Options Standard from the cloud session (`THETADATA_API_KEY`; one live session per account, so
nothing else may use the key during the pull). For the 1-minute quotes and strikes, either the Mac's
`data/thetadata/spy_0dte` (approved folder) or a fresh cloud pull with `fetch_thetadata_spy0dte.py`; the plan picks
whichever is faster.

## 7. Pass rule (fixed before any run)

**PASS** only if all three hold for D at the measured cost (queue rule, H = 60 s, stops at taker, all sessions):
1. mean return per trade > 0 with t >= 2.33 (one-sided 1%, the bar used for book A round 2);
2. mean > 0 in both halves, 2016-2021 and 2022-2026;
3. mean > 0 when the queue ahead of each resting order is doubled (the queue is inferred, so D must not depend on
   guessing it right).

Anything else is **FAIL**. The optimistic rule, other H values and the quiet filter are information only and can't
turn a FAIL into a PASS.

What follows:
- FAIL: recommend turning D off in paper (Evan decides; config PR as for B and C).
- PASS: recommend keeping D on, and changing D's paper fill rule from mid -1c to the measured costs so paper P&L
  matches what live would get (Evan decides; separate spec). Any move toward live still goes through the promotion
  ladder in HANDOFF section 10.8, including a week in shadow.

## 8. Limits, stated up front

- **Combo vs legs.** Robinhood would send D as one 4-leg order to the complex order book. Public data can't show
  how often a combo resting near mid gets filled. The study measures legged execution, which a Level 3 account can't
  do exactly (a short call can't be sold before its wing is held). It is an estimate of what a patient order can get,
  not a simulation of Robinhood's routing.
- **Routing.** Robinhood routes options orders to market makers for payment. Fills that market makers give
  internally still print on OPRA, so they are in the tape, but which orders they choose to fill isn't visible.
- **Queue position** is inferred from displayed size at trade times, not from the full order book.
- One contract per leg; D trades 1 lot, so size impact is ignored.
- The verdict's other caveats still apply (1-minute exit checks, VIX-based strikes).

## 9. Deliverables

- `research/d_fill_quality.py` (Part 1 pull + simulation, Part 2 re-score), with tests in
  `research/tests/test_d_fill_quality.py` written first (TDD): the walk's price path, each fill rule on hand-made
  tapes, cost signs for buys and sells, the measured fill model in the replay.
- `research/d_fill_quality.md` (results and verdict) and `research/d_fill_quality_results.json`.
- One PR, research only; CLAUDE.md and README research lines updated with the verdict.
