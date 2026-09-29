# Book F2 (single-name call and put debit spreads): pre-registration (2026-09-29)

Written before any F2 result exists. F2 goes to paper right away (Evan, 2026-09-29) and gets a real-quote backtest
once equity option history is bought (section 6). The rules don't change after results are seen; a new idea is a new
variant with its own pre-registration.

## 1. Idea and evidence

Evan's idea: after a fast opening run-up, out-of-the-money calls on the name get more expensive, so a call debit
spread (buy near the money, sell further out) suits the move better than shares or naked calls. Selling the wing
sells some of that richer volatility back, cuts the cost and caps the loss.

What the repo's research says before any option data:

| Finding | Source | Consequence for F2 |
|---|---|---|
| High-volume up days reverse the next day: RVOL ≥ 2 and up ≥ 3% → −19.5 bp, t −3.89, negative in both halves | `research/strategy_f_daily.md` | Setup P: put debit spreads fading that day, held one day. The only significant effect in F's research |
| Chasing gaps loses (≥ 2% gap, open to close: −15 bp, t −5.6); momentum holds show no edge (best t +0.49) | same | Setup C (calls) is the weakest-evidence leg; it runs because it is Evan's hypothesis, and paper decides |
| Single-name option spreads cost far more than the 2 bp per side the shares tests used | cost facts | F2 trades only names whose options quote penny- or nickel-wide, and paper fills 35% of the way from mid to natural |
| A debit spread caps the right tail that makes breakouts pay | payoff shape | The short strike sits about one straddle-width out (about 1 standard deviation to expiry), so the cap rarely binds within the hold |

## 2. Universe

34 names with liquid weekly options (`books.F2_debit_spreads.universe`: book E's option-liquid list less four
low-volatility staples, plus MU, PLTR, SMCI, ARM, MRVL, COIN, UBER, SHOP). Every leg must also pass a live quote check
at entry: fresh, a nonzero bid, and bid/ask ≤ max($0.05, 8% of mid).

## 3. Setups

**C, call debit spread (continuation):**
- Signal: F1's 09:35 ET scan rows on F2's names with a green first 5-minute candle and RVOL5 ≥ 2.0; the top 3 by
  RVOL5 are armed. No F1 scan (a macro-event skip) means no C signal.
- Entry: the first time the stock trades above the opening-range high before 10:30 ET.
- Exit: take profit, stop, a first-day stop if the stock trades at or below the opening-range low, or the time exit
  at 15:30 ET on the third trading day (the entry day is day 1).

**P, put debit spread (reversal):**
- Signal, at 15:40 ET: a name up ≥ 3.0% on the day versus the prior close, with volume so far ≥ 1.8 × its 20-day
  average daily volume (approximating the study's full-day RVOL ≥ 2). The top 2 by the day's move. No P scan on
  half-days.
- Exit: take profit, stop, or the time exit at 15:40 ET on the next trading day.

## 4. Structure, sizing and exits (both setups)

| Item | Rule |
|---|---|
| Expiry | The nearest listed expiry 5–12 calendar days out (so ≥ 2 trading days remain after the hold); none → skip |
| Long leg | The at-the-money strike (listed strike nearest the trigger price) |
| Short leg | The listed strike nearest long ± the ATM straddle mid for that expiry (up for calls, down for puts), at least 2 strike steps from the long strike |
| Structure check | Skip if the debit is above 60% of the width |
| Earnings | Skip if the name reports between the entry day and the exit day. No earnings calendar → no entry |
| Size | lots = floor($750 ÷ (debit × 100)); skip below 1 |
| Limits (Evan, 2026-09-29; first proposed at $200, raised before any F2 result) | $750 max debit per spread, at most 3 open, one per name, at most 3 new spreads a day, a −$300 realized day blocks new F2 entries. The shared $2,500 open-risk cap applies (debit = max loss) |
| Take profit | Spread mid ≥ 2 × the debit, or ≥ 80% of the width |
| Stop | Spread mid ≤ 0.5 × the debit |
| Never held into expiration | A leg expiring today forces the exit (the expiry rule makes this a guard only) |
| Paper fills | 35% of the way from mid to natural on the whole spread; the natural price is logged for taker P&L |

The bought leg is listed first, so a shadow-mode `review_option_order` goes out as a debit.

**Observe-only, logged on every candidate in `f2_decisions`** (never gate or size): each leg's IV, the ratio of the
short leg's IV to the long leg's (Evan's skew thesis), ATM IV ÷ 20-day realized volatility, debit ÷ width, the gap,
RVOL5 and the day's move.

## 5. What counts as working

Each setup (C and P) is judged separately: PF ≥ 1.1 at taker fills, t > 2 clustered by day, at least 20 sessions and
100 trades on paper (HANDOFF 10.8), and, once the real-quote backtest exists, a positive mean in both of its halves.

## 6. Backtest (after the data purchase)

There is no historical single-name option data in the repo. With ThetaData's US equity options history,
`research/fetch_thetadata_equity.py` pulls 1-minute quotes only for the (date, name, expiry, strikes) where these
signals fired in 2018–2026, and `research/f2_real_quotes.py` replays both setups with the rules above. Until then, the
free shares data can test only the stock's own drift after each signal (`research/f2c_drift.py`).

Replay details, fixed before any quotes are pulled:
- Signals come from Alpaca SIP 1-minute bars. C's RVOL5 uses a 14-session base of the same 09:30–09:35 SIP volume; C
  enters at max(OR high, the breakout bar's open) on the first bar before 10:30 ET whose high clears the OR high.
  P uses the bars before 15:40 ET and the 20-day average daily volume from SIP daily bars.
- Strikes, the straddle, the structure check and the exits come from `agentdesk/books/f2_spreads.py`, the paper
  book's own code. Exits are checked every minute on the spread mid; C's first-day stop fires on the first 1-minute
  bar whose low is at or below the OR low. Exit days and 12:45 ET half-day exits use the NYSE calendar
  (`research/nyse_calendar.py`).
- Fills on the whole spread: taker (natural, the judged model), mid_frac 35% (the paper book's) and mid. Fees are
  $0.04 per contract per leg per side. One lot per signal; returns are on the debit paid.
- Halves: 2018–2021 and 2022–2026. Earnings holds are skipped only when a report-date file is supplied
  (`--earnings`); otherwise the report says they are in the sample.
