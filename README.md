# AgentDesk

> Start with **HANDOFF.md** (v3: status, all five strategy books A–E with backtests, team operations, build spec, phased plan) and **CLAUDE.md**.
>
> Strategy books, all paper-only and run side by side: **A** Evan's MACD 0DTE calls (built), **B** 0DTE iron fly, **C** 30-min ORB bull-put spread (bearish-puts variant disabled), **D** 10:00 ET iron condor, **E** pre-earnings IV run-up (straddle T−3 / calendar T−10, never held through earnings). B–E are specified in HANDOFF section 7 and scaffolded under `books:` in `config.yaml`.

Automated 0DTE SPY call trading on Robinhood's Agentic Trading MCP. The engine that decides entries and exits is fixed-rule Python. A research crew of Claude "desks" briefs it, argues in roundtables, votes on size (50–125%, with a size-up only when every conviction check passes), and pitches tweaks or new strategies. Anything beyond today's bounded tweaks waits for your approval. The dashboard shows every decision, and the office in the bottom-right acts it out.

```
 SPY prints (Alpaca IEX / SIP) ──► bars: 144t · 1m · 5m · 15m ──► MACD(12,26,9) + RSI(14)
                                                                       │
 Research crew (Claude + web search) ──► size cap / blackouts ──► Risk manager ◄── daily limits, cooldowns
                                                                       │
                                   strike picker (time of day + levels) ──► order: review ─► place
                                                                       │            (Robinhood MCP)
                                             exit plan: stop / scale / trail / cross-back / flatten
```

## Quick start (simulator, no keys)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m agentdesk run --mode sim      # opens http://127.0.0.1:8765, synthetic day at 30x speed (config default is paper)
python -m agentdesk run --mode sim --speed 120
python -m pytest -q tests               # 25 tests
```

## Your rules, as the engine runs them

| Your rule | What the engine does | Config |
|---|---|---|
| 15m + 5m are the filter | Both MACD lines above signal on the live candle | `strategy.filter_timeframes` |
| 1m + 144t are the triggers | Both above signal on the last closed bar, and one of them crossed up within 120s. A fresh 1m cross makes a **SWING**; a 144t-only cross makes a **SCALP** | `strategy.trigger_timeframes`, `confirm_window_sec` |
| RSI 14, 30 / 70 | RSI inside 30–70 on 15m, 5m, 1m. Oversold is blocked too, so it doesn't catch falling knives | `strategy.rsi` |
| 1 OTM, 2 OTM early if levels allow, ITM late | 08:30 +1 (up to +2 if the next resistance clears the +2 strike), 10:30 +1, 12:30 ATM, 13:45 1 ITM. Steps in a strike if PDH / ORH / VWAP / a 5m pivot caps the strike | `strikes.schedule` |
| 4–5 contracts, $300–500 | `floor($500 / ask)`, capped at 5 contracts | `sizing` |
| Stop around 20% | Hard stop at −20% of premium (on mark; exits at the bid) | `exits.stop_loss_pct` |
| Take profit, let some ride | SWING: sell 50% at +25% and 25% at +50%, stop to breakeven, runner trails 25% off peak. SCALP: 50% at +15%, runner trails 15% | `exits.swing`, `exits.scalp` |
| Exit on MACD cross back | Before any scale-out, a cross back on the setup's timeframe (1m SWING / 144t SCALP) exits everything. After one, it exits the runner | `exit_on_cross_back` |
| "When it's ripping" | If the 5m histogram is rising and price is above VWAP, the runner ignores the 1m cross back and waits for a 5m cross back (the trail still protects it) | `ripping_hold` |
| Not held overnight | No entries after 14:30. Everything is flattened at 14:40 CT, and 5 minutes before each contract's Robinhood sellout time (14:45 CT for SPY 0DTE). Half-days: entries stop 11:20, flatten 11:40 | `strategy.entry_window`, `exits.flatten_at`, `calendar.early_close` |

Risk limits: −$400 daily loss halts trading. After +$600, giving back 40% of the peak halts trading. 12 trades a day max, one position at a time. Two straight losses start a 15-minute cooldown. No entries from 10 minutes before to 20 minutes after a high-impact event, and positions go flat 5 minutes before one. You can also pause, flatten, or kill from the dashboard.

## Robinhood account prerequisites

- Options must be approved **on the Agentic account itself** (options levels are per account). Your Agentic account (••••6452) was approved for Level 3 on 2026-09-27, which allows spreads; `review_option_order` accepted a 2-leg credit spread. Shadow and live modes refuse to start if the level is missing.
- Live mode also requires `robinhood.account_number` in `config.yaml`. The engine never picks an account to trade on its own.

## Going live: the ladder

Each rung uses the same engine and only swaps the data feed and the broker.

1. **Simulator** – `python -m agentdesk run`. Tests the plumbing only. It says nothing about edge.
2. **Backtest on real SPY** – put Alpaca keys in `.env`:
   - `python -m agentdesk backtest --days 60` runs on 1m bars. This tests the 15m/5m filter, the 1m trigger and the SWING exits. You can't build 144t bars from 1m data.
   - `python -m agentdesk backtest --days 5 --ticks` downloads every print and runs the full spec, including 144t and SCALP. It's heavy: about 1M prints a day on SIP. Downloads are cached in `~/.agentdesk/cache`.
   - `--source robinhood` pulls SPY 1-minute bars from your Robinhood connection instead of Alpaca. No data key is needed.
   - Option P&L defaults to Black-Scholes on the replayed spot (`--iv 0.16`). `--options alpaca` uses real option bars instead. Read `backtest/summary.json`: expectancy, profit factor, max drawdown, and results by setup, hour and exit reason. **Don't go past step 3 unless expectancy is positive after the fees and slippage modeled here.**
3. **Connect Robinhood** – on a desktop, run `python -m agentdesk rh-inspect`.
   - It opens Robinhood's OAuth page. Approve it and create the Agentic account when prompted.
   - It then prints every tool's schema to `rh_tools.json`, finds your Agentic account, quotes today's first OTM SPY call, and calls `review_option_order` on it. That call is a simulation; nothing is placed.
   - If any required field isn't mapped, the error names it. Pin it in `robinhood.arg_overrides`.
4. **Paper** – `python -m agentdesk run --mode paper`. Real SPY feed and real Robinhood option quotes, simulated fills. Run it for 2 weeks.
5. **Shadow** – `--mode shadow`. Every order is fully built and sent to Robinhood's `review_option_order`, but fills are still simulated. This proves the order path end to end.
6. **Live** – set `live_enabled: true` and `sizing.max_contracts: 1`, then run `--mode live`. Raise size only after 20+ live trades that track the paper results.

## Keys (`.env`)

```
ALPACA_API_KEY_ID=...        # data. Algo Trader Plus ($99/mo) = full SIP + OPRA. Free = IEX only (144t bars form far slower)
ALPACA_API_SECRET_KEY=...
MASSIVE_API_KEY=...          # alternative data vendor (formerly Polygon.io); set data.provider: massive
ANTHROPIC_API_KEY=...        # research crew with web search; without it, crew runs offline from config.events
```

Robinhood auth is OAuth. Tokens are cached at `~/.agentdesk/rh_oauth.json` with mode 0600. No password is stored.

## The research crew

| Desk | Huddles | Brings |
|---|---|---|
| Macro | 07:45, 11:30 | Econ calendar, data surprises, blackouts around CPI / NFP / ISM |
| Rates | 07:45, 11:30 | 2Y / 10Y moves, curve, auctions |
| Fed Watch | 07:45, 13:15 | FOMC timing, Fed speakers |
| Vol | 07:45, 11:30, 13:15, 15:05 | VIX, expected move, trend vs chop |
| Quant | after 2 straight losses, 15:05 | Our own stats; per-day and standing tweaks |
| Risk | loss reviews, halts | Reports the deterministic risk manager |
| Tape | loss reviews, big walls | Level 2 book read |

**Roundtables.** In each huddle the desks brief, then talk to each other: Rates checks Macro's read against bonds, Vol prices Fed risk, Risk grills Quant after losses. Desks can revise their votes after hearing the others. With an Anthropic key, this is one extra model call per huddle. Offline, it's templated from the briefs.

**Size votes.** Each desk votes 0.5–1.25×.
- Any vote below 1.0 cuts size right away, and the lowest vote wins.
- A size-up (up to 125%, which also lifts the contract cap from 5 to 6) only happens on a **SWING** entry when **every** check passes:
  - Macro, Rates and Vol each vote above 1.0 with confidence ≥ 0.6.
  - Fed isn't hawkish.
  - No desk votes down.
  - No high-impact event in the next 60 minutes.
  - SPY is above VWAP.
  - The 15m MACD histogram is rising.
  - Level 2 isn't leaning against the trade.
  - You're green on the day with no loss streak.
  - It's before 13:30 CT.

  The dashboard shows the checklist on every entry.

**Proposals.** Desks can pitch changes:
- *Next trade* or *today* tweaks apply automatically, but only to whitelisted parameters inside hard bounds. Examples: stop 10–25%, trail, first target, RSI ceiling, an earlier cutoff, fewer trades, skipping a setup, capping strike distance. Size and the loss limits are not on the list. Today's tweaks revert at the next session.
- *Standing changes* wait for your **Approve** in the dashboard. Once approved, they're written to `overrides.yaml`.
- *New strategies* arrive as written rules plus evidence. Approving one queues it to be built and backtested; nothing new trades by itself.

Briefs, trades, Level 2 snapshots and proposals are all logged under `~/.agentdesk/`.

## Level 2

The engine polls Robinhood's `get_equity_price_book` for SPY every second. On each snapshot it computes:
- near-book imbalance
- microprice vs mid
- walls (levels at least 4× the median size and at least 10k shares)

**Default mode is `observe`.** Every entry logs the book state and whether it *would* have blocked the trade, but nothing is blocked. There's no historical Level 2 to backtest against. After a few weeks of paper or live trades, run `python -m agentdesk l2-report` to see win rate by book state. Flip `l2.mode` to `enforce` only if the numbers support it.

## Real option quotes, recorded forward

Robinhood's intraday history for past 0DTE contracts comes back gap-filled, so it can't be used for option P&L. In paper, shadow and live modes the engine therefore records real 0DTE quotes (calls and puts, ATM−10 through ATM+10) every 10 seconds into `journal.option_quotes`. A few weeks of that settles naked calls vs debit spreads, and the iron fly / condor credits, on real prices.

## Things to know before real money

- **Stops live in this process, not at Robinhood.** If your Mac sleeps or loses its connection, open positions are unmanaged. Run it on a machine that stays awake (`caffeinate -dims python -m agentdesk run --mode live`). On startup, the engine refuses to trade if the Agentic account already holds option positions.
- **Robinhood hasn't published rate limits.** The engine's order arguments match the tool schemas the MCP server reported on 2026-09-27. Load: Level 2 is polled every second, the quote recorder every 10 seconds, and each open contract once per second. Entries and exits are marketable limit orders with up to 2 reprices. Each `place_option_order` carries an idempotency `ref_id`, so a retry can't double-fill.
- **Robinhood frames Agentic Trading around AI agents.** Here, a Python process places the orders, with Claude supervising. Confirm that fits their terms before running live.
- **Tick charts depend on the feed.** 144 prints on a consolidated SIP feed form in seconds. On the free IEX feed (about 4–6% of SPY volume) the engine uses 8 IEX prints as the 144t approximation (`strategy.tick_bar_size_iex`), so SCALP is only approximate until you upgrade to SIP.

## Layout

```
agentdesk/  engine.py strategy.py indicators.py bars.py strikes.py levels.py exits.py risk.py crew.py
            brokers/{paper,robinhood}.py  feeds/{sim,alpaca,massive}.py  backtest.py server.py web/
tests/      test_core.py
research/   strategy study + spread/VRP modeling scripts (see research/README.md)
tools/      build_demo.py  (records a sim day into the single-file demo page)
```
