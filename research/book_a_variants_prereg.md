# Book A variants: pre-registration (frozen 2026-10-02, before any result)

Thread "Improve book A strategy" (Evan, 2026-10-02: "see how we could improve A's strategy to actually make more
trades and make money"; he approved this list the same day). Script: `research/book_a_variants.py`. Nothing here
changes book A's live rules or `config.yaml`; a variant that passes is a proposal for Evan.

## 1. Why these variants

The 60-session SIP replay (`research/iex_vs_sip_out_60d_clean`, model option prices) shows book A takes about 5.6
trades a day on the real tape and loses $2,303 (PF 0.71). 129 trades closed inside a minute lost $3,406, mostly on
the 144t cross back (143 trades, 14% winners); trades held 10+ minutes made +$2,164. On 2026-10-02 A took no trade:
the RSI cap on 15m/5m blocked the morning's crosses, then the 5m MACD stayed below signal. So the variants either
add entries (1-3) or slow the exits (4), one change each.

| Variant | Change (everything else as in `config.yaml` on main e43dc96) | Aim |
|---|---|---|
| baseline | none | reference |
| v1_rsi_no_cap | `rsi_check_timeframes: [1m]`: no RSI band on 15m/5m. While their MACD is above signal the 30 floor practically never binds, so this removes the 70 cap | more trades |
| v2_puts | `allow_puts: true`: puts mirror every rule (exits mirrored in the script, section 2) | more trades |
| v3_filter_15m | `filter_timeframes: [15m]`: drop the 5m MACD filter (5m RSI check stays) | more trades |
| v4_slow_exits | `exit_on_cross_back: 5m` for SWING and SCALP: before a scale the trade leaves on the −20% stop, the time stop or a 5m cross back; after a scale the runner leaves on the trail or a 5m cross back | fewer losing churn exits |
| v5_swing_only | drop SCALP (144t trigger and exits) | fewer churn trades |
| v6_combo | every single variant (v1-v4) whose in-sample mean daily P&L beats the baseline's at mid1 fills; chosen on 2005-14 only | both |

v5 needs real SPY prints for the 144t bars, so it runs only on Evan's Mac (section 5). In the 1-minute replays every
entry is a SWING already (the backtest's default mode), so v5 equals the baseline here.

## 2. Method

- **Engine:** the unchanged live `Engine` (strategy, strikes, sizing $500 / 5 contracts, exits, risk limits:
  −$400 day halt, profit lock, 12 trades, 1 open, 2-loss cooldown), crew off, 1m trigger only (no 144t).
- **Data:** S&P 500 CFD 1-minute bars (Oanda), 2005-01 to 2020-05, sessions with at least 370 RTH minutes (half days
  and gaps dropped). Each session and its 5-day warm-up are re-based so the prior close is 765, so $1 strikes mean
  what they mean on SPY today.
- **Option prices:** the backtest's Black-Scholes model with its 0DTE skew. IV for a session = prior VIX close ×
  0.80 (HANDOFF section 7's remaining-RTH scale). Only information known at entry.
- **Fills:** `mid1` buys at mid + 1c (never above the ask) and sells at mid − 1c (never below the bid); `taker` buys
  at the ask and sells at the bid. The model's spread is 1c under $1, 2c under $3 and 3c above, so the two match
  for most trades. Fees as the engine charges them.
- **Puts:** the engine only sends down crosses to exit plans, so a put would never get its cross-back exit. The
  script mirrors it for v2: put plans ignore down crosses and get up crosses; "ripping" is mirrored (5m histogram
  falling, price below VWAP, 5m MACD below signal). This is an engine gap to fix if puts are ever adopted.
- **Statistic:** P&L per session (0 on sessions with no trade), mean and t-stat over sessions. Paired difference
  with the baseline per session, same fill model.

## 3. Pass bar (all three)

1. In-sample (2005-01 to 2014-12) mean daily P&L > 0 at `mid1`.
2. Out-of-sample (2015-01 to 2020-05) mean daily P&L t-stat ≥ 2.33 at `mid1` (one-sided p ≈ 0.01 = 0.05 / 5
   single-change variants).
3. Out-of-sample mean daily P&L > 0 at `taker`.

Trades per day is reported for every variant but is not part of the bar: more trades only help if the expectancy
is positive. A variant that beats the baseline but stays negative fails; it may still be reported as "loses less".

## 4. What is not changed after seeing results

Variant definitions, periods, IV scale, fills and the bar above. Any idea that comes from the results (another
exit, a time-of-day filter, a different stop) is a new variant for a new pre-registration, tested on the holdout
in section 5 and never on 2005-2020 again.

## 5. Holdout on Evan's Mac (after this run)

Variants that pass, plus v5, re-run on data that this run never touches:
- SPY SIP 1-minute bars 2020-06 to 2026-09 (`data/spy_1m`) with ThetaData's real SPY 0DTE 1-minute NBBO quotes
  (`data/thetadata/spy_0dte`, daily 0DTE since Nov 2022), same pass bar on the real-quote sessions.
- The 60-session SIP tick replay (`research/iex_vs_sip.py` engine path) for v5 and for any passing variant, so the
  SCALP side and the 144t exits are tested on real prints.
