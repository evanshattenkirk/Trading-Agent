# Round 5, sixth test: WX, W's iron fly on XSP held to cash settlement (pre-registration)

Status: **FROZEN 2026-10-30.** Evan asked for it in the round 5 thread on 2026-10-30 at 19:18Z ("Yes preregister that
and test it"), after W failed and its settlement row was reported. This file is merged to main before any XSP quote is
pulled and before any WX script exists; any later change is an amendment with its own date. Research only: no book,
rule or engine change. The common rules of `round5_prereg.md` section 1 (fill models, "sensitivities are never
selected from", what a pass means) apply unless this file says otherwise.

## 1. Why, and what has already been seen

W (`round5_prereg.md` section 2) chose the 13:00 ET entry with wings 2.0 straddles out by its discovery rule and
failed at its 15:40 ET exit: SPY patient −0.34% of max loss per trade 2016-2021 (t −0.30), −0.37% pooled from 2022.
W's prereg listed one reported-only row, "value at settlement", for exactly this question: whether a cash-settled XSP
version is worth a pull. On SPY, at the chosen cell with patient opening fills and the legs valued at intrinsic
against the 15:59 ET parity spot (no closing cost), that row is **+6.70% of max loss per trade, t 5.49** (1,880
trades, 2016-2026) and +6.44%, t 3.98 from 2022. At mid, the same trades closed at 15:40 make +4.37% (2016-2021,
t 3.74). So the premium is still there at 15:40, and the last 20 minutes plus the closing spread take it.

SPY can't be held to the close: Robinhood's SPY chain has `sellout_time_to_expiration` 1800 s, the engine flattens
at 14:40 CT, and SPY settles in shares. XSP (Mini-SPX, 1/10 of SPX) is cash-settled and PM-settled on the SPX close.
A read-only check of Robinhood on 2026-10-30 gave `sellout_time_to_expiration` 0 for the XSP chain, a
`sellout_datetime` of 16:00 ET on an XSP contract (2026-11-02 expiry), $0.01 ticks at all prices, and daily
expirations. A position can stay open to settlement there.

**Contamination.** The idea came from SPY's settlement row, which was seen on 2026-10-30. Only the chosen cell's
settlement rows were looked at (patient and the 2022 split). The other eight cells' settlement values sit in W's
`trades.parquet` but have not been looked at. SPX and SPY follow the same path (SPY ≈ SPX/10 less dividends), so
**an XSP result is not independent evidence of the premium.** It tests whether XSP's quotes and Robinhood's index fees
leave the SPY premium intact. Fresh evidence on the premium comes from QQQ and IWM (W's pull, still running; nothing
from either has been looked at when this file is frozen), and later from paper days.

**Prior against.** HANDOFF section 8, after Vilkov's August 2026 cost fix: a short SPXW iron fly or condor entered at
10:00 ET and held to settlement has Sharpe −0.56 at mid and −2.67 net (2016-2026). Our SPY rows disagree at mid, and
SPXW quotes are about 10 times wider than SPY's relative to spot. XSP may sit closer to SPXW than to SPY. A fail here
would agree with Vilkov.

## 2. Data

- **XSP quotes:** ThetaData 1-minute NBBO, all strikes, both rights, expiry = the session date, 09:30-16:00 ET,
  every session from 2016-01-01 with a same-day XSP expiry
  (`fetch_thetadata_spy0dte.py --symbol XSP` -> `data/thetadata/xsp_0dte`). The pull runs in the cloud session
  holding the ThetaData key; the account allows one live session. A session counts only if its expiry has quotes
  after 13:00 ET (a PM-settled expiry; an AM-settled one stops trading the day before). Sessions that close at
  13:00 ET are dropped.
- **Settlement value:** the SPX official close from FRED series SP500 (from 2016-10-31), divided by 10. A session
  outside FRED's range, or with a missing value, uses W's rule (the 15:59 ET parity spot) and is counted. A data check
  is reported: the median absolute gap between FRED/10 and the 15:59 parity spot on sessions that have both.
- **QQQ, IWM:** W's pull (`data/thetadata/qqq_0dte`, `iwm_0dte`). **SPY:** W's `trades.parquet` for side rows.
- **VIX:** `research/data/vix_cboe.csv`, prior close, for the buckets only.

## 3. Rule (one lot a session)

W's chosen cell, unchanged except the exit, the fees and the settlement value:
- At 13:00 ET: spot S by put-call parity (`bd_real_quotes.spot`). K = the strike listed for both calls and puts
  nearest S (ties to the lower). M = call mid + put mid at K.
- Sell the K call and the K put. Buy the call at the listed strike nearest K + 2.0·M and the put at the listed strike
  nearest K − 2.0·M (ties to the farther strike), each at least one listed strike from K.
- **Skip** if any leg has no quote at 13:00, a short leg has a zero bid, or any leg is wider than 25% of its mid and
  more than $0.02 (W's rule).
- **Hold to expiry.** No exit trade, no take-profit, no stop. Each leg pays its intrinsic value against the settlement
  value of section 2.
- **Fees:** Robinhood's index option fee, $0.50 a contract, plus $0.04 regulatory, on each leg at the open:
  **$0.54 a leg, $2.16 a lot.** Cboe's XSP customer fee is $0 under 10 contracts. Nothing is charged at cash
  settlement.
- **Per lot:** P&L = 100 × (credit − the fly's intrinsic value at settlement) − 4 × $0.54. Max loss = 100 × (the wider
  wing's width − credit) + 4 × $0.54. **Return = P&L / max loss**, under the fill model being scored. A credit at or
  above the width is skipped and counted (W's rule).
- Fill models as in `round5_prereg.md` section 1: mid, patient (sell at max(bid, mid − $0.01), buy at
  min(ask, mid + $0.01)), taker.

## 4. Bars

WX is a sixth test in round 5. Its t bar is **2.64** (Bonferroni, 5% two-sided over six tests). The five
pre-registered tests keep their 2.58. The report also says whether WX clears 2.58 and 2.33, and whether E1 and E2
clear 2.64; that changes no verdict. XSP trades one lot a session, so the trade t is the day t.

**WX passes only if all five hold:**
1. XSP at patient, every session on disk: mean > 0 and t ≥ **2.64**.
2. XSP at taker: mean > 0.
3. XSP at patient: mean > 0 in each half, split at the median trade date.
4. XSP at patient, only sessions whose max loss per lot is ≤ $300 at entry (the account's per-position cap a book
   would apply): mean > 0.
5. Fresh paths: QQQ and IWM pooled by day (the mean of the underlyings that traded that day), every session on disk,
   the same cell valued at settlement (W's settlement row: 15:59 ET parity spot, $0.04 a leg), at patient: mean > 0
   and t ≥ 1.65.

If QQQ or IWM quotes can't be had, bar 5 uses whichever exists. If neither exists, WX is reported on bars 1-4 and
labelled provisional: it can't be eligible for a book until bar 5 is met on fresh data.

## 5. Reported, never selected

- XSP at mid; XSP at $0.75 a leg (a fee stress); XSP closed at 15:40 ET under W's exit rule with $0.54 a leg each way.
  That isolates the cost of the last 20 minutes on XSP itself.
- All nine W cells held to settlement on XSP; the same nine on SPY (W's cached settlement rows), side by side.
- By year; sessions by weekday; the daily-expiry era (from 2022-11-14) against before.
- Prior VIX close < 15 / 15-20 / 20-30 / > 30.
- XSP against SPY on the same sessions: correlation of per-trade returns and the mean gap.
- Leg spreads at 13:00 (median per leg, XSP against SPY); share of sessions skipped and why.
- Max loss per lot (median, 90th percentile, share above $300); the worst five sessions.
- The FRED-against-parity settlement check from section 2.

## 6. If it passes

A pass makes WX eligible for a book spec, which needs Evan's approval. That covers sizing and two HANDOFF exceptions:
section 12 says short legs are never held into 3:30 PM ET, and the engine flattens every 0DTE book at 14:40 CT.
Before any paper book, a read-only `review_option_order` on a 4-leg XSP iron fly would confirm that Robinhood accepts
the order and show its fees. Nothing trades until then.

## 7. Order of work

1. This file merged to main.
2. The XSP pull in the ThetaData session; FRED SP500 saved to `research/data/sp500_fred.csv`.
3. `research/round5_wx.py`, written test-first on synthetic chains, reusing `round5_w.py`'s strike, skip and fill
   functions.
4. The run, with the verdict added to `research/round5.md` next to the other five, pass or fail.
