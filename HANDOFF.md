# AgentDesk: final handoff (v3, 2026-09-27)

> **Historical build record.** This is the handoff the project started from (v3, 2026-09-27). Much of it has since been built and some of it superseded; current status lives in `README.md` and `CLAUDE.md`.

This is the complete record of what was built, verified, tested and decided in the claude.ai sessions, plus the build plan for Claude Code. Read all of it before editing code. `CLAUDE.md` is the short version.

**Evan's standing decisions**
- **Paper only for now.** He deposits more before any live trading.
- **Five paper books run side by side:** A, B, C, D, E. Only one book can ever be promoted to live, by Evan.
- **The bearish put book stays disabled** until it passes its own pre-registered test.
- **Data:** Robinhood (execution, option quotes, Level 2, history) plus the free Alpaca account (SPY trade stream). He upgrades to Alpaca Algo Trader Plus ($99/mo) before live, if the results justify it or if the data feed is the blocker.
- **Crew rules:** fixed and approved (section 5).

---

## 1. Status snapshot

| Piece | State |
|---|---|
| Engine, strategy A, exits, risk, crew, Level 2, proposals, dashboard, pixel office, simulator, backtester | Built. 25 tests pass. Verified end to end in the simulator and against the local server |
| Robinhood MCP client | Argument shapes match the live server's schemas. The engine has not yet signed in from the Mac (`rh-inspect`) |
| Agentic account | `limited_margin`, **Level 3**, a small cash balance. A 2-leg SPY credit spread passed `review_option_order` (section 2) |
| Strategy A (Evan's MACD calls) | Implemented and runs in paper |
| Strategies B, C, D, E | Spec'd and backtested (E from literature only). **Claude Code builds these as paper books (section 9)** |
| Real-quote recorders | 0DTE SPY calls and puts, ATM±10, every 10 s, into `journal.option_quotes` |
| Demo | Recorded sim day in the same UI: https://claude.ai/artifact/NtcjQSYRcETxvPqakVWGPL |

---

## 2. Broker facts (verified live on 2026-09-27)

- **Accounts.**
  - The Agentic account is the only one the agent can trade (`agentic_allowed` is true). Type `limited_margin`: it can reuse unsettled funds but can't borrow. `option_level_3`, a small cash balance.
  - The main (non-agentic) account is Level 3 but not agent-tradable.
- **Multi-leg works.** `review_option_order` on the Agentic account accepted this order with no `order_checks` alerts:
  - Order: SPY 2026-09-28 sell-to-open 760P / buy-to-open 758P, `direction:"credit"`, limit 0.30.
  - Fees came back as $0.08 in total, i.e. **$0.02 OCC + $0.02 ORF per contract per leg**.
  - Collateral: **$200 cash** (the width).

  So credit spreads, condors and flies are possible. The older help text saying agents can place only "long" orders is out of date for this account. Uncovered short legs (put ratio spreads, naked straddles) would need far more collateral (about $76k for one SPY short put), so they're out of scope.
- **Quote quality.** On Friday's 1-DTE quotes, 760P was 0.13/0.14 (IV 19.0%) and 758P 0.08/0.09. Penny-wide markets. `get_option_quotes` also returns IV and greeks.
- **0DTE timing.**
  - New same-day positions can be opened until 3:30 PM ET.
  - Robinhood begins closing expiring at-risk positions at 3:30 PM ET, or 3:45 PM ET for late-close products like SPY.
  - The API's `sellout_datetime` for SPY 0DTE is 19:45Z (14:45 CT).
  - Engine rules: book A flattens at 14:40 CT; B and D close by 14:30 / 14:25 CT; everything flattens 5 minutes before each contract's own `sellout_datetime`.
  - SPY options are American-style: short ITM legs can be assigned early. Never hold short legs into the close.
- **Level 2.** `get_equity_price_book` returns bid/ask levels for up to 4 symbols per call.
  - It's Robinhood's Nasdaq TotalView book, so it shows only Nasdaq participants. SPY is Arca-listed, so this is a partial view.
  - Evan's account shows `is_gold: true`. Whether the API needs Gold isn't documented.
- **History.**
  - `get_equity_historicals` gives real SPY bars at 1 minute and 15 seconds.
  - `get_option_historicals` returns **only gap-filled bars for past 0DTE contracts**, so there are no usable option price histories. That's why the recorders exist.
- **Transient errors.** Read calls occasionally return HTTP 500; the client retries once. `place_option_order` carries a `ref_id` UUID so a retry can't double-fill. Rate limits aren't published.

---

## 3. Data stack for paper (Robinhood + free Alpaca)

| Need | Source | Notes |
|---|---|---|
| SPY trade stream → 144t/1m/5m/15m bars, VWAP | Alpaca Basic, **IEX** websocket (free) | 1 connection, 30 symbols, 200 REST calls/min. IEX carries about **3.7% of SPY prints** (measured; see the note below), so the engine builds the "144t" series from **5 IEX prints** (`strategy.tick_bar_size_iex`), roughly equivalent in time. That's an approximation; time bars are unaffected |
| SPY 1m history for warm-up and backtests | Alpaca (SIP history older than 15 min is free) or `--source robinhood` | Robinhood history needs no data key |
| Option quotes (all books) | Robinhood `get_option_quotes` | Real NBBO-like bid/ask plus IV and greeks. Alpaca's free option feed is indicative only; don't use it for fills |
| Level 2 | Robinhood `get_equity_price_book` | Polled every 3 s (`l2.poll_ms: 3000`; was every second), observe mode |
| Earnings calendar, IV (book E) | Robinhood `get_earnings_calendar`, `get_option_quotes` | Record daily IV to build history |
| Research crew | Anthropic API (`ANTHROPIC_API_KEY`) with web search | Offline templated fallback without a key |

**When to upgrade to Algo Trader Plus ($99/mo):**
- Book A's SCALP results on the IEX-approximated 144t look materially different from a SIP replay of the same days (compare with `backtest --ticks` on SIP history, which is free once older than 15 min).
- Or before going live with book A.

Books B–E don't need the upgrade.

**Correction (2026-09-28): IEX vs SIP measured.** The "Check book A IEX vs SIP data" thread replayed Sept 14–25, 2026 on both feeds:
- IEX carried **3.7%** of SPY prints, not 4–6%.
- The 8-print "144t" approximation caught **55%** of the real 144t MACD crosses.
- Book A's P&L **flipped sign** between feeds: SIP −$690 (PF 0.42) vs IEX +$521. About a third of the SIP trades never happen on IEX.

So IEX paper results for book A are **not evidence** for or against A. Judge A with a weekly after-close replay on free SIP history instead, and buy Algo Trader Plus only when A is promoted to shadow or live. Evan approved both follow-ups on 2026-09-28: `tick_bar_size_iex` is now 5 (was 8; raises cross capture to about 75%), and the comparison is being rerun over 60 days.

---

## 4. System architecture (what exists)

```
feeds/ (sim | alpaca | massive) -> bars.py (144t, 1m/5m/15m, VWAP) -> strategy.py (signals) -> engine.py
engine.py -> strikes.py (strike choice) -> risk.py (limits, sizing) -> brokers/ (paper | robinhood review/place) -> exits.py
crew.py + proposals.py -> restrictions/votes/tweaks -> risk.py / engine config (never orders)
l2.py (book scoring) -> entry log + dashboard
journal.py (SQLite ~/.agentdesk/journal.db: trades, briefs, option_quotes)
server.py (FastAPI + websocket) -> web/ (dashboard, office.js pixel office, lightweight-charts)
backtest.py (replays history through the same Engine) | research/ (strategy studies)
```

**Modes** (`--mode`, config default `paper`):
- `sim`: synthetic SPY + Black-Scholes quotes. A demo; it carries no edge information.
- `paper`: real data and quotes, simulated fills.
- `shadow`: paper, plus every order is sent to Robinhood's `review_option_order`.
- `live`: needs `live_enabled: true`, `robinhood.account_number` set by Evan, and options approval.

**Commands:**
- `python -m agentdesk run [--mode sim|paper|shadow|live] [--speed N] [--seed N]`
- `rh-inspect`, `backtest`, `record-demo`, `l2-report`
- `pytest -q tests`

**Simulator.**
- `feeds/sim.py` runs a regime-switching random walk with a U-shaped trade rate.
- Each sim day has a seeded "mood" (risk-on / neutral / risk-off) that drives offline crew votes. About half of sim days carry a fake high-impact event, and the simulated order book has no predictive power.
- Use it for demos and plumbing tests only.
- `tools/build_demo.py` records a sim day into the single-file demo page.

**Dashboard.**
- **Header:** SPY, VWAP, day P&L, W/L, clock, pause/flatten/kill (flatten and kill need an inline confirm).
- **Chart** (144t/1m/5m/15m): candles, VWAP, levels, entry/exit markers, MACD and RSI panes.
- **Side cards:**
  - Signal gate matrix
  - Level 2 ladder with imbalance, microprice and walls
  - Position ladder showing stop / entry / targets / mark
  - Risk, including the size-up checklist
  - Research crew with votes
  - Crew proposals with Approve/Reject
  - The office
- **Bottom tabs:** trades and activity.

**Office (bottom right).** Pixel art on a 192×120 grid drawn at 2×.
- **The agent** sits at a three-monitor desk under a Chicago-style window whose sky follows CT time. The monitors show the 1m price, the 144t price and position P&L; an LED ticker scrolls SPY and P&L.
- **Desks:** Macro, Rates, Earnings (left wall), Fed, Vol, Ops (right wall), Quant, Tape, Post-mortem, Risk (front row), each labeled.
- **Huddles:** desks walk to spots around the agent, face whoever they're addressing, and talk in speech bubbles. The caption shows "RATES → MACRO: …".
- **Reactions:**
  - The agent types on orders, jumps with $ particles on wins, and slumps under a rain cloud on losses.
  - An alarm light flashes on the kill switch.
  - Lights dim after the close, with "Zzz / BACK AT 8:15" overnight.
  - Tape reads live depth on its monitor.

---

## 5. The team (research crew): how it operates (approved rules)

| Desk | Huddles | Brings |
|---|---|---|
| Macro | 08:15 catch-up, 08:25 huddle, 11:30 | Econ calendar and surprises; blackouts around CPI/NFP/ISM/PCE. Its one web call also covers the Fed and Treasuries (`fed` and `rates` sub-briefs). Vote reaches books A and C |
| Rates | 08:15 catch-up, 08:25 huddle, 11:30 | 2Y/10Y moves, curve, auctions, read from Macro's `rates` sub-brief. Information only (`INFO_ONLY`): no web call, vote fixed at 1.0, no events, no pitches |
| Fed Watch | 08:15 catch-up, 08:25 huddle, 13:15 | FOMC, Fed speakers, read from Macro's `fed` sub-brief. Information only, like Rates |
| Vol | 08:15 catch-up, 08:25 huddle, 11:30, 13:15, 15:05 | VIX from Robinhood `get_index_quotes` (prior close as fallback); expected move from the recorded 0DTE ATM straddle during the session, else from VIX. One premarket web call for VIX1D (`vix1d_flag`), bias and its vote; later reads keep that vote and are deterministic. Votes 0.75 when VIX > 28. Vote reaches books A, B, C, D and G |
| Quant | loss reviews, 15:05 | Our stats; per-day and permanent tweak proposals. Its vote, cooldown and pitches are rule-based; the LLM only adds notes. Vote reaches book A |
| Risk | loss reviews, halts | Deterministic risk manager's view |
| Tape | loss reviews, big walls | Level 2 read; comments on walls of at least 25k shares near price (at most once every 20 min) |
| Ops | 08:15 catch-up, 08:25 huddle, halts | Pre-flight (section 12): paper mode, books `paper_only`, loss limit and watchdog armed, not halted, 15m/5m history warm, SPY price, Robinhood connected, previous session's quotes recorded. Reports only; cuts size to 50% only when the 15m/5m MACD history is short |
| Earnings | 08:15 catch-up, 08:25 huddle | Robinhood `get_earnings_calendar` (31 days, large caps): **runs book E's screen** (T−10..T−8 E2, T−3 E1, exit T−1 / T−0) and notes SPY heavyweights that reported overnight. Information only |
| Post-mortem | 15:05 | Audits each closed trade against the rules (entry window, blackouts, contract cap, flat by the flatten time, loss within stop + 10 points) and flags round trips; writes `~/.agentdesk/postmortems/YYYY-MM-DD.md` for the weekly Quant report. Information only |

- **Premarket (Evan, 2026-09-28).** At 08:15 CT (`crew.schedule.arrive`) Macro, Rates, Fed, Vol, Ops and Earnings arrive and research at their own desks, in parallel. At 08:25 CT (`crew.schedule.premarket`) they huddle with the agent using those briefs, so the meeting ends before the 08:30 open. A desk still researching at 08:25 attends with its offline read and says so; the huddle never waits. If the engine starts after 08:25, the premarket huddle briefs in full as before.
- **Weekly event calendar (2026-09-29).** On the week's first session, at the 08:15 arrival, one web call saves the week's scheduled US events to `crew.calendar_path` (`~/.agentdesk/econ_calendar.json`). Each day its high-impact events become blackouts next to Macro's; blackouts are the union, since they only restrict. Each huddle compares the two and logs any disagreement.
- **Research.** With an Anthropic key, Macro researches with Claude plus web search at 08:25 and 11:30, and Vol once at 08:25, returning JSON briefs: headline, bias, confidence, events, vote, notes, proposals. That is about 3 web calls a day plus the weekly calendar (it was 10). Rates and Fed are derived from Macro's reply. Every desk prompt describes all the books and names the ones its vote reaches. Without a key, everything runs offline from config events and the data.
- **Roundtable.** After the briefs, desks talk **to each other**: Rates checks Macro against bonds, Vol prices Fed risk, Risk grills Quant after losses. Only desks in that huddle can revise their vote. With a key this is one extra LLM call per huddle; otherwise it's templated.
- **Size votes (0.5–1.25×).**
  - Any vote below 1.0 cuts size immediately, and the lowest vote wins, per book: each desk's vote reaches only the books in `crew.vote_books` (default Macro A, C; Vol A, B, C, D, G; Quant and Ops A; nothing reaches E or F). The risk manager keeps one multiplier per book.
  - **A size-up** (up to 125%, which lifts book A's contract cap from 5 to 6) happens only on a **book A SWING entry when every check passes**:
    - Macro and Vol each vote above 1.0 with confidence ≥ 0.6
    - Fed isn't hawkish (Macro's `fed.bias`)
    - No desk voting down
    - No high-impact event within 60 min
    - SPY above VWAP
    - 15m MACD histogram rising
    - Level 2 not against the trade
    - Green on the day with no loss streak
    - Before 13:30 CT

    The checklist is shown on every entry.
  - For books B–E, the crew can only restrict: blackouts, skipping a day, cutting lots. Never a size-up.
  - **Crew log and scorecard (2026-09-29).** Each huddle's final votes, per-book multipliers and roundtable revisions, every entry a crew blackout or the VIX1D flag stopped, calendar disagreements and the day's token cost go to `journal.crew_log`. Each trade stores its crew effect (`trades.crew`: quantity at 1.0× next to the actual quantity, the size-up, the desks that cut, and book A's active tweaks). The weekly Quant report has a crew scorecard.
  - **Ops, Earnings and Post-mortem are restrict-only** (`RESTRICT_ONLY` in `desks.py`): their votes are capped at 1.0, they can't pitch proposals or add blackouts, and they never lift a halt. A cut from any of them also fails the size-up check "No desk voting down".
- **Proposals (Evan's approved rules):**
  - **Next-trade or today changes apply automatically,** but only to a fixed list of settings within hard limits:
    - stop 10–25%
    - trailing stop (SWING 10–35%, SCALP 8–25%)
    - first profit target (SWING +15–50%, SCALP +10–30%)
    - RSI cap 60–75
    - stopping earlier
    - fewer trades
    - skipping a setup
    - how far out of the money (−1 to +2)

    Today's changes revert at the next session.
  - **Suggestions expire when their reason passes.** A standing change tied to an event (FOMC, CPI...) clears after that event; any other standing change after 5 days (`crew.proposal_ttl_days`); today's tweaks at the end of the day. An expired suggestion can't be approved.
  - **Size and loss limits can't be changed by the crew.**
  - **Permanent changes and new strategies wait for Evan's Approve button.** Permanent changes are written to `overrides.yaml`. An approved new strategy gets built and backtested first; nothing new trades on its own.
  - The whitelist and bounds live in `agentdesk/proposals.py`. Keep them in sync with this text.
- **Hard boundary.** The crew never places, cancels or sizes orders.

---

## 6. Level 2

- **Scoring** on each snapshot:
  - near-book imbalance (±$0.25 of mid)
  - microprice minus mid (cents)
  - walls (levels at least 4× the median size and at least 10k shares)
- **Mode is `observe`.** Every entry logs the book state and whether it *would* have blocked, and nothing is blocked. `enforce` (block calls when imbalance < −0.25 or an ask wall sits within $0.15) is available but off.
- Treat it as a Nasdaq-only liquidity hint (section 2).
- After about 50 trades in any book, run `l2-report`, extended per book. Enable `enforce` for a book only if the book state separates winners from losers.

---

## 7. The five strategies

**Testing conventions.** All option results below are **Black-Scholes modeled**; none are historical quotes.
- **Data:** S&P 500 1-minute data 2005–2014 (in-sample) and 2015–2020 (out-of-sample), plus SPY 5-minute data 2025-04 → 2026-03.
- **Re-basing:** every session is re-based to SPY ≈ 765, so $1 strikes and $2 wings mean what they mean today.
- **Implied volatility:** from the prior VIX close, with the remaining-RTH standard deviation set to 0.80× VIX-implied (0.60× as the "little variance premium" stress case).
- **Costs:** 1¢ half-spread, or 2¢ taker, per leg per side, plus $0.04/contract/leg in fees. Rules were frozen; nothing was fitted.
- **Scripts:** `research/strategies_bcd.py` and `research/research.py`, plus the `vrp_*` scripts.

### A. Evan's MACD 0DTE calls (built; paper book A)

**Entry:**
- 15m and 5m MACD(12,26,9) above signal on the live candle.
- 1m and 144t both above signal, and one of them crossed up within 120 s. A fresh 1m cross makes a **SWING**; a 144t-only cross makes a **SCALP**.
- RSI(14) strictly between 30 and 70 on 15m, 5m and 1m.
- Entry window 08:35–14:30 CT. One cross, one entry.

**Strikes** by CT time, $1 per strike:

| From | Default | Furthest allowed |
|---|---|---|
| 08:30 | 1 OTM | 2 OTM, if the next resistance clears the 2-OTM strike by $0.15 |
| 10:30 | 1 OTM | 1 OTM |
| 12:30 | ATM | ATM |
| 13:45 | 1 ITM | 1 ITM |

The nearest level below the chosen strike (PDH/L/C, opening range, HOD/LOD, VWAP, 5m pivots, $5 round numbers) steps the strike in. A spread wider than 12% steps toward ATM, or skips.

**Size:** `min(5, floor($500/ask))`.

**Fees:** $0.04 per contract per side ($0.02 OCC + $0.02 ORF, section 2), the same as the other books (`sizing.fee_per_contract`, $0.03 until 2026-10-07).

**Exits** (on premium mid):
- −20% hard stop (paper runs −35% since 2026-10-03, Evan's choice after the real-quote holdout; `research/book_a_variants.md` 4b).
- Scale-outs are fractions of the starting size, with a half rounding up and one contract always kept as the runner (2026-10-07): 5 contracts sell 3 at the first target and 1 at the second, 3 sell 2, 2 sell 1, and 1 contract doesn't scale.
- SWING: sell 50% at +25% and 25% at +50%; stop to breakeven; runner trails 25% off its peak; a 1m cross back sells everything before any scale. After a scale, the runner goes on the 1m cross back, unless "ripping" (5m histogram rising and price above VWAP), in which case it waits for the 5m cross back. 20-minute time stop at under +10%.
- SCALP: sell 50% at +15%; trail 15%; exit on the 144t cross back; 6-minute time stop at under +5%.
- Flatten at 14:40 CT, 5 minutes before sellout, before high-impact events, or on the kill switch.

**Risk:**
- −$400 day halts trading (book A only: A flattens and stops; the other books keep their own limits, Evan 2026-10-01).
- Profit lock: after +$600, giving back 40% halts trading (book A only).
- 12 trades a day, one position at a time.
- 2 straight losses start a 15-minute cooldown.
- Event blackouts from 10 minutes before to 20 minutes after.

**Evidence:**
- A 5m stand-in for the rules held 30 min: t = 0.23 / 1.69 / 0.39 (2005–14 / 2015–20 / 2025–26). No edge.
- Naked calls on those entries: −5% to −9% of premium per trade (modeled).
- The exact 1m/144t spec on real data is **untested**. Paper book A plus `backtest --ticks` will test it.

### B. Iron fly (Evan: keep as specified; paper book B)

**Rules:**
- Skip days with a high-impact event before 14:00 CT, and days when Vol flags VIX1D more than 3 points above VIX.
- 08:45 CT (09:45 ET): sell the ATM call and put, buy wings ±$5. One 4-leg credit order at mid or better.
- Take profit at 50% of credit; stop when the loss equals 1× credit; close by 14:30 CT.
- Paper size: 1 lot. It risks about $140–190 at current levels.

**Backtest** (1¢ costs, IV 0.80 → 2¢ taker → IV 0.60):

| Period | Avg/trade on risk | Win | PF | t | Avg $/1-lot | Worst |
|---|---|---|---|---|---|---|
| 2005–14 | +17.8% → +11.0% → −3.5% | 59% | 1.64 → 1.37 → 0.90 | +10.0 → +6.4 → −2.1 | +$25 → +$17 → −$5 | −$237 |
| 2015–20 | +14.2% → +8.3% → −7.3% | 58% | 1.50 → 1.28 → 0.79 | +5.2 → +3.2 → −2.9 | +$24 → +$17 → −$10 | −$214 |
| 2025–26 | +31.7% → +25.3% → +6.9% | 69% | 2.80 → 2.34 → 1.28 | +7.0 → +5.7 → +1.6 | +$48 → +$40 → +$15 | −$164 |

**Read:** the entire edge is the variance risk premium, meaning how rich real 0DTE premium is versus the move that follows. At IV 0.60 (little premium) it disappears.
- Vilkov's real-quote study (SPX 0DTE, 2016–2026, net of half-spreads + 0.5 bp) puts the short straddle/strangle at only **about +1.2 bp/day net**. Treat the table above as an upper bound.
  - **Correction (2026-09-28):** Vilkov's repo corrected its cost model in August 2026 (`KNOWN-ISSUES.md`; the half-spread had been charged at 1/100 of its true size). After the fix, a short iron fly/condor entered at 10:00 ET and held to settlement has Sharpe **−0.56 at mid** and **−2.67 net** on SPXW. The +1.2 bp/day figure above is out of date. SPY 0DTE quotes are about 1¢ wide (~0.13 bp of spot) versus ~1.7 bp for SPXW, so the cost drag doesn't transfer directly. The negative result at mid does carry over as a warning, though B uses take-profits, stops and early closes. The real-quote check (section 10, step 7a) decides B; see `historical-option-data.md` in the claude.ai project files (`/mnt/project-files/research/`; it isn't in this repo).
- **The go/no-go number:** the real opening ATM straddle cost vs the realized move afterwards, computed from `option_quotes` after 4+ weeks of paper.

### C. ChatGPT's bullish 30-min ORB → $2 bull-put credit spread (paper book C, as requested)

**Rules** (frozen from `CLAUDE_CODE_HANDOFF.md`, the ChatGPT version):
- **Entry**, all on 5m bars, 10:00–14:30 ET:
  - close above the 9:30–10:00 high, with the prior close at or below it (fresh breakout)
  - above VWAP
  - EMA20 above EMA20 three bars earlier
  - RSI(14) 55–72
  - green candle
  - volume at least 0.8× the rolling 20-bar median
  - at most 2 trades a day
- **Structure:** short put ≈ 1 strike below spot, long put 2 lower; size from max loss, `floor(min($400 budget, $300 per-position cap) / maxLoss)`, so the lower limit wins (`books/account.py` `size`).
- **Exits** (on the underlying, first one wins): −0.18%, +0.45%, 5m MACD cross below signal, 45 minutes, and 15:15 ET at the latest.

**15-year backtest** (the ChatGPT doc had 60 sessions and 24 trades):

| Period | Trades | Underlying avg | t | $2 bull-put (1¢) | ATM call (1¢) | Bull-put at 2¢ taker |
|---|---|---|---|---|---|---|
| 2005–14 | 1,066 | −3.3 bp | −5.75 | −3.5%/trade on risk, PF 0.57 | −6.3%, PF 0.59 | −6.6%, PF 0.34 |
| 2015–20 | 366 | −3.5 bp | −3.76 | −4.0%, PF 0.53 | −7.8%, PF 0.53 | −7.1%, PF 0.32 |
| 2025–26 | 102 | −3.0 bp | −1.96 | −3.0%, PF 0.58 | −7.7%, PF 0.46 | −6.2%, PF 0.32 |

Every expression lost in every period, including the $2 call debit spread and 1-OTM calls (in the script output). ChatGPT's positive 24-trade result doesn't reproduce. The research brief's own kill rule (profit factor below 1.1 at natural fills) fails.
- **Paper book C still runs as requested;** expect it to lose.
- Log its trades against the recorded quotes to confirm on real prices.

**Bearish put mirror** (break below the opening-range low, below VWAP, EMA falling, RSI 28–45, red candle, same exits): the underlying averages −0.1 to −1.7 bp, t −0.1 to −0.9.
- ATM long puts come out −0.1% / +0.8% / −4.1% per trade at 1¢ and IV 0.80.
- Bear-call credit spreads and put debit spreads are negative in every period.
- **It stays disabled** (`books.C_bear_puts.enabled: false`). To revive it, pre-register a filter set before looking at any results, e.g. negative gap, below prior close, VIX up on the day, falling VWAP. Apply a Bonferroni penalty per filter tried, and require out-of-sample positive expectancy after taker costs, with 2008, 2020 and 2022 reported separately.

### D. 10:00 ET iron condor, capped-risk short strangle (new; paper book D)

**Rules:**
- **Entry** at 09:00 CT (10:00 ET): short call and put at ±0.9× the remaining-session expected move from VIX, wings $2 beyond. One 4-leg credit order.
- **Quiet filter (on by default), as built:** the 08:30–09:00 CT range (9:30–10:00 ET, known at entry) is below its trailing 14-day median of the same window, and price is within 0.12% of VWAP at entry.
  - *As first written and backtested (look-ahead):* the first-hour range, 9:30–10:30 ET, which ends 30 minutes after the 10:00 ET entry, so it can't be traded. The quiet columns in the table below use this version.
- **Exits:** take profit at 50% of credit; stop when the closing debit reaches 2× the credit; close 14:25 CT.
- **Size:** 1 lot, about $150 risk and about $45 credit.

**Backtest** (the quiet columns are the look-ahead 9:30–10:30 ET filter; the built filter's numbers follow the table):

| Period | No filter (1¢, IV 0.80) | Quiet filter, look-ahead (1¢, IV 0.80) | Look-ahead quiet at 2¢ taker | Look-ahead quiet at IV 0.60 |
|---|---|---|---|---|
| 2005–14 | +4.7%, PF 1.57, t +9.2 | +8.4%, PF 2.58, t +12.1 (n=769) | +4.0%, PF 1.58 | +0.2%, PF 1.02 |
| 2015–20 | +3.1%, PF 1.34, t +3.7 | +7.3%, PF 2.29, t +7.2 (n=342) | +3.5%, PF 1.50 | −1.0%, PF 0.90 |
| 2025–26 | +8.5%, PF 2.59, t +6.7 | +12.6%, PF 6.91, t +9.0 (n=91) | +10.1%, PF 5.12 | +5.9%, PF 2.13 |

**The built filter** (08:30–09:00 CT range vs its 14-day median, plus price within 0.12% of VWAP; `research/d_quiet_check.py`), per trade on risk for 2005–14 / 2015–20 / 2025–26: +7.5% / +5.7% / +12.0% at 1¢ and IV 0.80, +3.3% / +1.5% / +9.0% at 2¢ taker fills, and 0.0% / −3.7% / +4.1% at IV 0.60. These are the numbers to quote for book D; they predate the 2026-10-07 research data fixes (`research/README.md`).

Worst day: about −$90 to −$135 per lot. Averages are small in dollars: about +$5 to +$19 per lot per trade.
- **It carries the same caveat as B:** it's variance-premium dependent. The quiet filter was also taken from the ChatGPT doc's condor idea, so it isn't an independent out-of-sample discovery.
- Its value is that it's the **most robust modeled book**: positive in all 3 periods under both cost levels.
- It still needs the real-quote check.

### E. Pre-earnings IV run-up swing trades, 1–3 weeks (new; paper book E)

**Evidence** (literature; no local backtest was possible, because historical equity option IV isn't available here):
- **Gao, Xing & Zhang (JFQA 2018, 1996–2010, 30,000+ announcements):**
  - Straddles bought before earnings and sold at the announcement earned +3.00% over 3 days (t 26), +2.22% over 5 days, and +2.30% for 1 day.
  - After paying the full quoted spread, only **short-dated straddles (4–10 DTE) stayed positive** (+0.61–1.64%/day). Medium and long maturities turned negative.
  - Returns were larger for smaller firms, less-covered firms, and names with volatile past earnings reactions. They were lower when VIX was high.
- **BSIC (S&P 500, 2011–2021, T−3 to T+1, i.e. held through the announcement):** +1.17% gross, **−9.07% after costs**. The anomaly has decayed and costs dominate.
- **IV really does run up into earnings** (IBM front-month example: 30% to over 70%, then about 20% after). The published profitable window is the **last 3–5 trading days**, not 2–3 weeks.
- **Double-calendar practitioner backtest** (30 Dow stocks): 87% win rate. It **holds through** the announcement, so it's a different trade and not peer-reviewed.

**Verdict: viable only in a narrow form, as a paper experiment.**
- A 1–3 week long straddle pays about 2 weeks of theta and wider spreads for IV gains that arrive mostly at the end.
- Your "spread combo" idea is the right fix for the theta problem: a **long calendar**, which sells a weekly expiring *before* earnings and buys the weekly expiring *just after*. The short leg decays while the long leg's earnings IV inflates.
- Evidence for the calendar version is practitioner-only.

**Rules (paper):**
- **Universe:** about 30 liquid large caps with weekly options and an ATM spread no wider than 5% of mid. Not SPY. Screen daily with `get_earnings_calendar` (31-day window) at the 08:15 CT catch-up; the Earnings desk owns it (`agentdesk/earnings.py`, list in `crew.earnings.universe`, holidays in `calendar.holidays`).
- **E1, short-dated straddle:** buy the ATM straddle in the first expiry **after** earnings with 4–10 DTE, at the close **3 trading days before** the announcement. Sell at the close before the announcement: the day before for pre-market reporters, the same day for after-close reporters. Take profit +20%, stop −30%.
- **E2, calendar (the spread combo):** at **T−10 to T−8** trading days, sell the ATM weekly expiring before earnings and buy the same-strike weekly expiring after, as one 2-leg debit order. Exit at T−1 (T−0 for after-close reporters). Take profit +15%, stop −30%.
- **Both:**
  - **Never hold through the announcement.**
  - Skip if VIX > 30, or if the earnings-expiry IV is already above its recorded 80th percentile, once history exists.
  - Max debit per position $500 for E1 and $250 for E2 (`books.E_earnings_iv.max_debit`, `max_debit_e2`; this said $250 for both when written); max 3 open; at most one per sector.
- **Data:** record daily ATM IV (front, earnings expiry and ~30-day) for the universe from `get_option_quotes`. For a real backtest, buy **ThetaData** ($40–80/mo, US equity options NBBO history) or Databento, and replay 2018–2026.

**Promotion gate:** at least 100 events, mean > 0 after taker costs, t > 2, and no single month more than 40% of P&L.

---

## 8. Research summary

**11 published intraday strategies** (pre-registered; significance bar adjusted for multiple tests: |t| > 2.86 in 2005–14, and |t| > 1.96 with the same sign in 2015–20):

| Strategy | t 2005–14 | t 2015–20 | t 2025–26 |
|---|---|---|---|
| First 30 min → last 30 min | **3.75** | 0.56 | −1.31 |
| Noise-boundary momentum | **4.07** | 1.34 | −0.99 |
| Opening range breakout | 1.94 | 0.94 | −0.17 |
| Evan's MACD rules (5m stand-in) | 0.23 | 1.69 | 0.39 |

The directional edges decayed after publication. The only effect that is modeled-positive in every period is premium selling.
- Real-quote literature: Vilkov, SSRN 4641356, as accessible on 2026-09-27. Net of costs: put ratio spread Sharpe 0.93, top-3 basket 0.82, short straddle/strangle about +1.2 bp/day, directional spreads negative. The claim that no 0DTE strategy survives costs could not be verified.
  - **Correction (2026-09-28):** after Vilkov's August 2026 cost fix, **no structure keeps a positive net Sharpe** on SPXW 0DTE (10:00 ET entry, held to settlement, 2016–2026):

    | Structure | Sharpe at mid | Net, as first published | Net, corrected |
    |---|---|---|---|
    | Put ratio spread | +1.06 | +0.84 | **−0.61** |
    | Long strangle/straddle | −0.27 | −0.51 | **−0.97** |
    | Iron butterfly/condor (short vol) | −0.56 | −0.96 | **−2.67** |

    The conditional put ratio (0.93) is now −0.75, and the top-3 basket (0.82) is now −0.82. So "no 0DTE strategy survives costs" now holds for SPXW. For SPY, whose spreads are about 10× tighter, it is open until the real-quote replay. This strengthens the case for the real-quote checks in section 10, step 7, before any promotion.
- Beckmeyer, Branger & Gayda: retail 0DTE traders lost about $241k/day on average. Multi-leg, premium-collecting trades did better than single-leg debits.
- Low-turnover references on S&P 2005–2020 (price only): buy and hold Sharpe 0.44 (max DD −57%); 200-day trend 0.45 (−22%); VIX-scaled exposure 0.63 (needs leverage).

**House pass bar (2026-10-07).** New studies use this bar unless their pre-registration says why not, before any result:
1. In-sample mean P&L > 0 at patient fills (mid ∓ 1¢ per leg).
2. Out-of-sample (or holdout) mean daily P&L t ≥ 2.33 at patient fills: one-sided p ≈ 0.01, which is 0.05 split over up to five candidates. With more candidates in one file, use 0.05 / k one-sided.
3. Out-of-sample mean P&L > 0 at taker fills.
4. The in-sample period must be one in which the trade could actually be placed (expiries listed, data known at entry). A modeled pass still needs a real-quote replay before paper promotion.

Studies already run keep the bar they froze; changing a frozen bar after seeing results would be fitting. What each used:

| Study | Bar |
|---|---|
| `research.py`, 11 intraday strategies (2026-09-27) | \|t\| > 2.86 in-sample (Bonferroni) and \|t\| > 1.96 out-of-sample, same sign |
| `strategies_bcd.py`, books B, C, D (2026-09-27) | no common frozen bar: sign and t in every period, at both cost levels and IV 0.60; C also against its brief's kill rule (PF below 1.1 at natural fills) |
| `strategies_new.py`, F1–F4 (book G is F3) | in-sample t > 2.86 at mid − 1¢, mean > 0 at natural fills out-of-sample, premium-scale break-even ≤ 0.85, 1 lot under $300. No out-of-sample significance, and the in-sample period predated the expiries F3 needs, which is how F3 passed; its real-quote replay failed (2026-10-06) |
| `book_a_variants.py` (2026-10-02), `book_h_candidates.py` stage 1, `vix_carry.py` | the house bar (1–3) |
| `book_a_holdout.py` round 2 | t ≥ 2.33 at mid ± 1¢ and mean > 0 at taker, on real quotes |
| Book H holdout (stage-1 survivors only) | t ≥ 1.65 at mid ∓ 1¢ and mean > 0 at taker |
| Book F replication, F1 v2, `f2c_drift.py` | PF ≥ 1.1, day-clustered t > 2, mean > 0 in both halves |
| Book F2 (`strategy_f2_prereg.md` section 5) | per setup: PF ≥ 1.1 at taker, day-clustered t > 2, at least 20 paper sessions and 100 trades, and a positive mean in both halves of the real-quote backtest |
| X1 and the `strategies_equity_prereg.md` drafts | at taker: PF ≥ 1.1, mean > 0 in both halves, clustered t > 2.50 (Bonferroni over X1–X4) |
| Book E promotion (section 7E) | at least 100 events, mean > 0 after taker costs, t > 2, no month above 40% of P&L |

Section 10's paper promotion minimums are separate and apply on top of whichever bar a book's research used.

---

## 9. Multi-book build spec (Claude Code implements)

- **`agentdesk/books/` package:**
  - `Book` (name, config, own `RiskManager`, positions, P&L, journal tag)
  - `Strategy` interface: `on_bar(tf, bar)`, `on_clock(now)`, `on_quote(leg_quotes)`, returning `OrderIntent(legs=[{contract, side, position_effect, ratio}], qty, limit_rule)` and `ExitIntent`
- **Book A:** wrap the existing `SignalEngine` + `ExitPlan` + `choose_strike` unchanged.
- **Books B, C, D, E:** new modules implementing sections 7B–7E exactly. Tests first.
- **Multi-leg paper fills:** fill at natural (sell legs at bid, buy legs at ask) or at `mid ± k¢` per config, with a reprice loop.
  - Reject crossed, zero or stale (> 5 s) quotes, and any leg spread > 25% of mid.
  - Reconcile partial fills before any new order.
- **Live and shadow:** `order_args()` already supports multi-leg with `direction`; review every order; one `ref_id` per logical order.
- **Global risk:** kill switch across all books; account-level open-risk cap; B, C and D must not open while the account's buying power is below the combined max loss.
- **Crew hooks:** blackouts apply to every 0DTE book; size-up applies to book A only; E's universe screen is run by the Earnings desk.
- **Dashboard:** book switcher; per-book P&L strip; a book A–E label on markers and trades; the office monitors show the active book.
- **Journal:** add a `book` column to `trades`; add an `iv_history` table for E.
- **Recorders:** extend `OptionQuoteRecorder` for E's watchlist (EOD snapshot, 15:00 CT).
- **Required tests:**
  - fresh-breakout detection and duplicate-signal suppression (C)
  - max-loss sizing (B, C, D)
  - credit/debit direction on multi-leg
  - take-profit / stop / time exits (B, C, D, E)
  - never-hold-through-earnings (E)
  - stale-quote fail-closed
  - kill switch across books
  - the proposals whitelist can't touch `books.*` size or loss fields

---

## 10. Phased plan with acceptance criteria

1. **Setup (Mac).**
   ```
   cd ~/Desktop/"Trading Agent"/agentdesk
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   pytest -q tests
   python -m agentdesk run --mode sim
   ```
   Done when the tests pass and the sim dashboard loads.
2. **Connectivity.**
   - Create a free Alpaca account and put the keys in `.env`.
   - Run `python -m agentdesk rh-inspect`: it signs in, confirms the Agentic account is Level 3, and runs the review simulation.
   - Done when `rh-inspect` prints a review with no errors and the IEX stream shows SPY prints.
3. **Book A paper.** `python -m agentdesk run --mode paper` during market hours. Done after 1 full session with bars, signals, L2 logs and `option_quotes` recorded, and no errors in the log.
   - *Note (2026-09-28):* this gate proves the plumbing only. Book A's results on the IEX feed don't count toward promotion (see section 3). Its evidence is the weekly SIP replay.
4. **Multi-book framework plus B, C, D** (section 9). Done when all required tests pass and a sim day runs all books without cross-contamination.
5. **Book E and the IV recorder.** Done when the earnings screen lists candidates daily and `iv_history` grows.
6. **Four or more weeks of paper, all books.** Weekly, the Quant desk writes a report per book: trades, net after taker costs, PF, t, worst day, L2 split.
7. **Real-quote analyses.**
   - (a) B and D: opening straddle and condor credit vs realized move.
   - (b) A: naked calls vs debit spreads on recorded quotes.
   - (c) C: confirm the negative expectancy.
   - (d) Optionally buy ThetaData to backtest B, D and E on 2018–2026 quotes.
8. **Promotion (Evan decides; one book only).** Minimums:
   - at least 20 paper sessions and 100 trades (or events, for E)
   - PF ≥ 1.1 at taker fills, and the paper result within its backtest range
   - no unresolved order-state bugs

   Then: deposit, choose sizing (section 11), switch to SIP if the book is A, run shadow for 1 week, then go live at 1 lot / 1 contract.

---

## 11. Sizing (paper now; live after the deposit; Evan picks)

| Account | A: max per trade / contracts | A: daily loss | B, C, D: max loss per position | E: max debit |
|---|---|---|---|---|
| $500 | $100 / 1–2 | $30 | not viable (one condor risks about $150) | $100 |
| $2,500 | $375 / 3 | $125 | $125 (1 lot) | $150 |
| $5,000 | $500 / 5 | $250 | $250 | $250 |
| $10,000+ | $500 / 5 (normal) | $400 | $300 | $400 |

The rule behind the table: max per trade ≈ 15% of the account, capped at $500; daily loss ≈ 5%; option-book risk per position 3–5%. The crew can never change these.

---

## 12. Safety rules

- Claude Code never calls `place_option_order`, `cancel_option_order` or `exercise_option` directly. Orders go only through the engine, in live mode, with `live_enabled: true` and an Evan-set account number. Keep MCP permission prompts on for those tools.
- Paper is the default and must survive restarts. Promotion needs an explicit config change plus Evan's confirmation.
- Stops live in the engine process; no multi-leg stop orders exist at Robinhood. Monitor the process health; halt on connector errors, stale quotes, a position mismatch or an ambiguous order state. If the engine task itself crashes, the process sells what is open and exits with status 1. Robinhood calls time out after `robinhood.call_timeout_sec`, and a dropped session reconnects.
- Never hold short legs into 3:30 PM ET, or into earnings for E.
- Don't change strategy rules without Evan. Propose, backtest, then apply. Don't add filters after seeing results without calling it a new, separately tested variant.

## 13. Open items for Evan

- Choose live sizing at deposit time (section 11).
- Approve or reject crew proposals as they come in (dashboard).
- Decide on ThetaData ($40–80/mo) once paper data shows which books are worth a deeper quote backtest.

## Sources

- Robinhood: [Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/), [Trading with your agent](https://robinhood.com/us/en/support/articles/trading-with-your-agent/), [Options trading hours](https://robinhood.com/us/en/support/articles/options-trading-hours)
- [Alpaca market data plans](https://docs.alpaca.markets/us/docs/about-market-data-api)
- Vilkov, 0DTE Trading Rules: [SSRN 4641356](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4641356), [replication repo](https://github.com/vilkovgr/0dte-strategies)
- [Gao, Xing & Zhang, Anticipating Uncertainty: Straddles around Earnings Announcements (JFQA 2018)](https://quantpedia.com/www/Anticipating_Uncertainty-Straddles_Around_Earnings_Announcements.pdf)
- [BSIC, Straddling outside and into earnings, Part II](https://bsic.it/straddling-outside-and-into-earnings-part-ii-2/)
- [IBKR Quant, Pre-earnings IV behavior](https://ibkrcampus.com/campus/ibkr-quant-news/the-unique-behavior-of-pre-earnings-announcement-implied-volatility/)
- [Double calendar earnings backtest](https://optionstradingiq.com/double-calendar-earnings-trade/)
- Data: [FutureSharks/financial-data](https://github.com/FutureSharks/financial-data), [datasets/finance-vix](https://github.com/datasets/finance-vix), [vivek-v-rao/Intraday-Vol](https://github.com/vivek-v-rao/Intraday-Vol/blob/main/SPY.csv)
