# X1: short iron fly through earnings reports — frozen pre-registration

Date: 2026-10-06, thread "Find a profitable strategy for a new book", round 3. Evan chose X1 on 2026-10-06 22:26Z.
X1 was drafted in `research/strategies_equity_prereg.md` on 2026-09-29; this file freezes those rules and fills in
the details the draft left open. It is committed **before any earnings-date history or single-name option quote for
X1 has been downloaded or looked at** in this repo. Nothing below changes after a result is seen; a change made later
is a new, separately labelled variant.

## 1. Why

Implied moves before an earnings report usually exceed the move that follows (the earnings variance premium; Dubinsky,
Johannes, Kaeck & Seeger 2019, *RFS*; Gao, Xing & Zhang 2018, *JFQA*). Book E owns the run-up before the report and
leaves before it; X1 sells what E leaves behind, with wings so the loss is capped. Round 1 and 2 showed that SPY's
premium is smaller than four legs of costs. The earnings premium is several times larger per trade (an earnings
straddle is often 5-10% of the stock), so the costs are a smaller share of it.

## 2. The rule (X1)

- **Universe:** book E's 30 names as `config.yaml` lists them today (`crew.earnings.universe`). META's options traded
  as FB before 2022-06-09; the fetcher maps it. Survivorship: these are today's large caps; the report says so.
- **Report times:** from SEC EDGAR, each company's Form 8-K filings that list Item 2.02 (Results of Operations), with
  the filing's acceptance time in US Eastern time. 8-K/A amendments are ignored. A 2.02 filing less than 45 calendar
  days after the previous kept one is dropped (pre-announcements, restated results); dropped filings are listed.
  - Let R be the first session whose 09:30 ET open comes after the acceptance time. A filing accepted between 09:30
    and 16:00 ET on a session day is a during-market report: **skipped** and counted.
  - The **entry session** is the last session before R; the **exit session** is R.
- **Expiry:** the first listed expiry on or after the exit session.
- **Entry, 15:45 ET on the entry session:**
  - Spot from put-call parity on that expiry (the median of the five strikes where |call mid − put mid| is smallest,
    `bd_real_quotes.spot`). K = the strike listed for both calls and puts nearest the spot (ties to the lower).
  - Straddle S = call mid + put mid at K.
  - Sell the K call and the K put. Buy the call at the listed strike nearest K + 1.5 × S and the put at the listed
    strike nearest K − 1.5 × S (ties to the farther strike); each wing at least one listed strike away from K.
  - **Skip** when: any of the four legs has no quote, a zero bid, or a bid/ask wider than max($0.05, 8% of its mid);
    the prior session's VIX close is above 30; the mid credit is below 25% of the wider wing's width.
- **Exit:** buy the fly back at 10:00 ET on the exit session, whatever the price. No stop; the wings are the stop.
  A leg with no quote at 10:00 uses its last quote earlier that session; none at all means "no exit quote" (counted).
- **Size:** one lot. Return per trade = P&L / (max loss × 100), with max loss = wider wing width − credit, each under
  the fill model being scored.

## 3. Fills, fees

- Fill models per leg: **mid**; **mid1** (sell at mid − $0.01, buy at mid + $0.01, never past the bid or ask);
  **mid_frac** (35% of the way from mid to the bid or ask, the paper broker's F2 model); **taker** (sell at the bid,
  buy at the ask).
- $0.04 per contract per leg per side: $0.32 per lot per round trip.

## 4. Data

- ThetaData US equity options, 1-minute NBBO (Evan's Options Standard plan): the chosen expiry, all strikes, both
  rights, 15:30-16:00 ET on the entry session and 09:30-16:00 ET on the exit session.
- SEC EDGAR company submissions (`data.sec.gov/submissions`), CIKs from `sec.gov/files/company_tickers.json`.
- CBOE VIX daily closes (`data/cboe/VIX_History.csv`, already on the Mac from V1).
- **Coverage check first (reported, not part of the bar):** for five names, list expiries and fetch one 15:45 quote
  in each year 2018-2026. If the plan's history starts after 2018-01-01, the sample starts at the first month with
  quotes for most names, and the report says so. The halves in section 5 stay as dated.
- **Date sanity check (reported, not part of the bar):** the median absolute move from the entry spot to the exit
  spot (both by parity) on event pairs vs. the same names' median overnight move on 50 random non-event session
  pairs. Event moves should be several times larger; if they are not, the dates are wrong and the run is invalid
  until the date source is fixed (the data, never the rule).

## 5. Periods, statistics, pass bar

- **Sample:** reports from 2018-01-01 to 2026-09-30. Halves: 2018-2021 and 2022-2026 (by entry session).
- **Statistics per fill model:** trades, win rate, mean return on max loss, profit factor, t of the mean clustered by
  entry session, P&L per lot, worst trade, by-year table, skips by reason.
- **Pass bar (from the 2026-09-29 draft, all at taker fills):**
  1. Profit factor ≥ 1.1 over the whole sample.
  2. Mean return > 0 in both halves.
  3. Clustered t > 2.50 over the whole sample (the Bonferroni bar for the four drafted candidates X1-X4).
- mid, mid1 and mid_frac are reported beside it and never rescue a taker fail.

## 6. Sensitivities (reported, never selected from)

Exit at 09:45 and at 15:45 ET on the exit session; wings at 1.0 × and 2.0 × the straddle; the VIX filter off.

## 7. Caveats known in advance

- Early assignment of a short ITM call before an ex-dividend date isn't modelled (the hold is one night).
- An 8-K accepted after the press release can mislabel the timing. A release during market hours with an 8-K filed
  after 16:00 would put the entry after the news; the sanity check in section 4 is the guard.
- Single-name 1-minute NBBO at 15:45 is thinner than SPY's; taker fills are the honest price.

## 8. What follows

- **Pass:** a spec for a paper book (X1 beside E, same universe and earnings desk, entries at 15:45 ET, positions
  kept overnight like E and F2). Short legs are held through a report and overnight, which HANDOFF's "never hold short
  legs into the close" rule forbids for 0DTE; this needs Evan's explicit OK in the spec. Size and loss limits left to
  Evan.
- **Fail:** dropped, with the result committed.
