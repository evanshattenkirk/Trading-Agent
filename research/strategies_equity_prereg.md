# Single-name candidates for the purchased data: DRAFT pre-registration (2026-09-29)

**Status: draft for Evan's approval. Nothing here is built, run or traded.** Evan asked (2026-09-29) to put F1 and F2
on paper at once and buy data to backtest them and to look for other strategies. This file lists the other
candidates before any of that data is seen. When Evan approves a candidate, its rules below are frozen by that commit,
its script is written and tested, and only then is it run. A changed rule after a result is a new variant with its
own entry here.

## Common rules

- **Data:** Alpaca SIP daily and 1-minute bars (free history, older than 15 minutes); ThetaData US equity options
  1-minute NBBO quotes, pulled only for signal days (the `research/fetch_thetadata_equity.py` pattern).
- **Costs:** shares 2 bp per side. Options are judged at taker fills (natural), with mid and 35% mid-to-natural shown
  beside them; $0.04 per contract per leg per side.
- **Pass bar:** PF ≥ 1.1, a positive mean in both halves (2018–2021 and 2022–2026; shares tests on 2016–2026 split
  2016–2020 / 2021–2026), and a day-clustered t above **2.50**, the Bonferroni bar for the four new candidates (X1–X4)
  at 5% two-sided. Sensitivity rows are reported and never selected from.
- **Holdout first where the idea came from our own data:** X3 and X4 restate effects found on the 505 S&P names in
  `research/strategy_f_daily.md`. They're tested first on names that study never saw (below) with shares, on free
  data. The option version is run only if the shares holdout passes.
- **Broad holdout universe:** US common stocks from the NASDAQ Trader symbol directory (`strategy_f_intraday.py
  --universe broad`), prior close ≥ $10, 14-day average volume ≥ 1M, ATR14 ≥ $0.50, 20-day dollar volume ≥ $25M,
  **minus** every name in the S&P list the daily study used. Survivorship: today's listings only; the report says so.
- **Earnings dates:** X1, X2 and book E need a report-date history (symbol, date, before-open or after-close) for
  2018–2026. Nothing in the repo has one; Robinhood's calendar is forward-looking. Candidate sources are the Nasdaq
  earnings calendar by date (free, unofficial; history depth to be checked) or a paid feed. The scripts will take a
  CSV `symbol,date,timing`.

## Queued (rules already frozen elsewhere)

| Test | Rules | Data |
|---|---|---|
| F1 v2 on the broad universe | `research/strategy_f1_prereg.md` | SIP 1-minute bars (free) |
| F2-C premise: the stock's drift after the breakout | `research/f2c_drift_prereg.md` | cached shares bars (free) |
| F2 C and P on real quotes | `research/strategy_f2_prereg.md` section 6 | SIP bars + ThetaData |
| Book E (E1 straddle T−3, E2 calendar T−10..T−8) | `docs/superpowers/specs/2026-09-29-book-e-design.md` | ThetaData + earnings dates |

## X1: earnings IV crush, defined risk (short iron fly through the report)

- **Idea:** implied moves before earnings tend to exceed the realized move (the earnings variance premium). Book E
  owns the run-up and exits before the report; X1 sells what E leaves behind, with wings.
- **Universe:** book E's 30 names (`crew.earnings.universe`).
- **Entry:** 15:45 ET on the last session before the report (the report day itself for after-close reporters, the
  prior session for before-open reporters). Sell the ATM call and put in the first expiry after the report; buy the
  wings at the listed strikes nearest ATM ± 1.5 × the ATM straddle mid. Skip if any leg's bid/ask > max($0.05, 8% of
  mid), if the prior VIX close > 30, or if the credit is below 25% of the wing width.
- **Exit:** 10:00 ET on the first session after the report, whatever the price. No stop (the wings are the stop).
- **Size (for the replay):** one lot; returns on max loss (wing width − credit).
- **Sensitivity:** exit at 09:45 and 15:45 ET; wings at 1.0× and 2.0× the straddle.

## X2: post-earnings drift with debit spreads

- **Idea:** post-earnings announcement drift (the stock keeps moving in the direction of a big report reaction).
  Weaker in large caps since the 2000s, and the repo's momentum rows show no multi-day drift after high-volume up days
  in general, so this is a long shot; it is here because it's the best-documented single-name anomaly.
- **Signal:** the first session after a report, the open gaps ≥ 4% from the prior close, the day's volume ≥ 3 × the
  20-day average, and the close is on the gap's side of the open (close > open for up gaps, < open for down gaps).
- **Entry:** 15:45 ET that day, a debit spread in the gap's direction: long ATM, short the strike nearest ATM ± the
  straddle mid (F2's structure code), first expiry 10–20 calendar days out, debit ≤ 60% of the width.
- **Exit:** take profit at 2 × the debit or 80% of the width, stop at 0.5 × the debit, else 15:45 ET on the fifth
  session after entry.
- **Shares check first (free):** the same signal, bought or shorted at the 15:45 ET price and held five sessions,
  excess over the equal-weight universe. If its mean is ≤ 0 in either half, the option replay isn't run.

## X3: high-volume up-day reversal on names the study never saw (F2-P's premise)

- **Idea:** F2-P rests on one cell of the daily study (RVOL ≥ 2, up ≥ 3%, next day −19.5 bp, t −3.89, 505 S&P names).
  The same study's normal-volume control also reverses (t −5.0), so the effect may be plain short-term reversal. X3
  asks whether it holds out of sample.
- **Test (shares, free daily bars):** broad holdout universe; a close ≥ 3% above the prior close on volume ≥ 2 × the
  20-day average; short at the close, cover at the next close; excess over the equal-weight universe. Both the RVOL ≥ 2
  row and the RVOL < 1.5 control are reported; the RVOL ≥ 2 row is the one judged.
- **Options:** only if the shares row passes, F2-P's put spread on the holdout names with option quotes.
- **Reading:** even a pass is about 20 bp a day. A put debit spread keeps roughly its net delta of that, so the option
  version must clear its own costs on real quotes; the shares result alone doesn't justify it.

## X4: gap-up fade on names the study never saw

- **Idea:** buying S&P names that gap up ≥ 2% loses from open to close (−15 bp, t −5.6, both halves). The mirror is a
  fade.
- **Test (shares, free 1-minute bars):** broad holdout universe; open ≥ 2% above the prior close with RVOL5 ≥ 2;
  short at the 09:35 ET price, cover at 15:55 ET; no stop; excess over SPY.
- **Options:** only if the shares row passes: a put debit spread in the first expiry 5–12 days out, long ATM, short
  one straddle-width down, entered at 09:35 and closed at 15:55 ET the same day.
- **Reading:** the edge per trade is small next to single-name option costs; the shares test is expected to be the
  decisive step.

## Considered and not proposed

- **Short premium on single names (put spreads, 30–45 DTE):** the index variance premium is the repo's one robust
  effect, but it is much smaller on single stocks, and the tails are single-name tails. B and D already harvest it on
  SPY.
- **Momentum holds after high-volume days (5–20 sessions):** 16 cells tested, none significant (largest positive t
  +0.49); the positive 2021–2026 half is an observation, not a signal.
- **Any filter chosen from F1/F2 paper results:** the observe-only tags (gap, RVOL, SPY vs VWAP, skew, IV/RV,
  catalyst) are there to be tested later, each as its own pre-registered variant on data after the tag was logged.
