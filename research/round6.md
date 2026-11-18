# Round 6 results: prediction markets (PM1-PM5)

Rules frozen in `round6_prereg.md` (commit 18a196a, 2026-11-18) before any script existed. Research only: no book,
rule or engine change. Everything ran in a cloud session on 2026-11-18 from Kalshi's public API (no account),
Coinbase's public candles, the Cleveland Fed nowcast file and round 5's XSP 0DTE quotes. Scripts:
`round6_fetch.py` (pulls), `round6.py` (rules, report), tests in `research/tests/test_round6.py`.
Result files: `round6_results.json` (frozen tests), `round6_posthoc.json` (diagnostics, never a verdict).

Fill: buy at the ask (NO at 1 − yes bid), Kalshi taker fee 0.07·P·(1−P) on a 100-lot rounded up to the cent, hold
to settlement. Returns are per contract on money spent; t is over events (an S&P day, a Bitcoin hour, a CPI release).
Bar: t ≥ 2.58 and a positive mean in both halves.

| Test | What | Events | Mean / contract | t | Verdict |
|---|---|---|---|---|---|
| PM1 | S&P daily range: buy the modal bracket at 12:00 ET | 912 days, 2022-12 to 2026-11 | −10.8% | −2.93 | **FAIL** |
| PM2 | S&P range markets vs XSP option-implied odds, edge ≥ 3¢ | 34 days traded (of 894 priced) | −6.1% | −0.34 | **FAIL** |
| PM3 | Bitcoin hourly above/below vs a 6-hour realized-vol model | 989 hours, 2024-12 to 2026-11 | −15.8% | −2.51 | **FAIL** |
| PM4 | Bitcoin hourly range: buy the modal bracket 15 min before | 1,470 hours, 2024-06 to 2026-11 | −4.0% | −1.38 | **FAIL** |
| PM5 | CPI m/m brackets vs the Cleveland Fed nowcast, edge ≥ 3¢ | 31 releases, 2021-12 to 2026-11 | −24.0% | −1.33 | **FAIL** |
| PM6 | PM4's bracket as a resting bid, filled from the trade tape (follow-up) | 1,194 filled of 1,467 hours | −5.3% | −1.40 | **FAIL** |

PM1's confirmation sample (Nasdaq-100, same rule) is also negative: −13.3%, t −3.25 (898 days).

## What the five say together

**Kalshi's mid is a good forecast; the spread is the cost.** On the S&P, Kalshi's mid and the probability implied by
XSP options agree to 1.6¢ on average over 4,328 priced markets on 894 days, and they forecast equally well (Brier
0.1165 vs 0.1164). Only 3% of markets differ by more than half Kalshi's spread plus 3¢, and trading those lost.
A model built from Bitcoin's own recent volatility and a public CPI nowcast both lose to Kalshi's prices when
they disagree. A Fed paper (FEDS 2026-010) found Kalshi's headline CPI forecast beats the Bloomberg consensus;
this agrees.

**No variance premium in the brackets.** PM1 asked whether the bracket where the index sits is cheap (as the 0DTE
straddle is rich). It is the other way round at noon: the modal bracket wins 43% of the time at an average price of
46¢, and buying it loses 10.8%. Buying NO on it doesn't work either (post-hoc row: −5.4%, t −1.79; Nasdaq −2.7%;
Bitcoin −16.1%). Both sides lose because the bracket's spread is about 4¢ on the indices and 7¢ on Bitcoin, plus
up to 1.75¢ fee.

**The one positive pattern was being the maker, and real fills remove it (PM6 below).** Bought at the bid with no fee, PM4's modal Bitcoin bracket makes
+18.3% a contract (t 4.93, both halves positive), and round 5's K1 favourites made +2.8%. Candles can't tell whether
a resting bid would have filled; a bid tends to fill when the price is moving against it. That needs its own test
with fills from the trade tape (below).

## PM6: the maker row with real fills (FAIL)

Evan picked the maker test after the five above. Rules frozen in `round6_maker_prereg.md` before any trade-tape row
was pulled; script `round6_maker.py`, results `round6_maker_results.json`. Same 1,486 hours and modal bracket as
PM4; at 15 minutes before the close, a YES bid rests at the best bid until the close. It counts as filled only if
Kalshi's trade tape (every trade, public) prints below the bid after it was posted. No fee (the series charges
takers only).

| | Orders | Filled | Mean per filled contract | t | P&L per order posted |
|---|---|---|---|---|---|
| PM6, trade-through fills | 1,467 | 81% | −5.3% | −1.40 | −1.75¢ |

**FAIL** (halves −14.2% and +3.7%). The +18.3% maker row came from assuming every bid fills. In practice the bids
that don't fill (19%) are the ones whose bracket was about to win, and the ones that fill late are losers: fills in
the last five minutes lose 31.0% (t −3.29), fills in the first five minutes make +3.2% (t 0.67). This is the same
adverse selection book D's fill study found on SPY options. Reported only: fill at the touch (front of queue)
−2.2%; posted 45 minutes before −3.6%; the neighbouring brackets −26.6% (t −5.83); a 0.0175 maker fee −6.2%.

## Sensitivities (reported, never selected)

- PM1: entry 10:00 −17.1% (t −4.00), 14:00 −5.4% (t −1.65), 15:00 −10.5% (t −3.91); modal plus neighbours
  −10.7%; at a 0.035 fee rate −9.2%. Maker +2.1% (t 0.48).
- PM2: edge ≥ 1¢ −11.0% (200 days), ≥ 5¢ +2.6% (18 days, t 0.09), entry 14:00 −51.0% (t −4.47), fee 0.035 −9.7%.
- PM3: entry 5 min before −32.0% (t −6.00), 45 min −17.2%; σ from 60 minutes −16.3%, from 1,440 −25.1%; edge 1¢
  −19.9%, 5¢ −18.5%. Maker +2.3% (t 0.30).
- PM4: entry 45 min before −7.1%; modal plus neighbours −19.3% (t −11.4).
- PM5: edge 1¢ −7.5%, 5¢ −32.0%; entry two weekdays before −44.3% (t −3.57). Maker +0.2%.

## Data notes

- Kalshi minute candles: 22,303 S&P markets, 19,188 Nasdaq range markets, 22,067 Bitcoin markets (the six nearest
  spot at each entry offset in 1,486 SHA-1-sampled hours, k = 9 of 13,457), 534 CPI markets. Legacy tickers (INX,
  INXD, NASDAQ100D, BTC, BTCD, CPI) are included; weekly and monthly index events are not daily markets and were
  left out. 59 legacy BTCD hours whose ticker carries no hour were left out.
- PM1/PM2 skip 137 days with fewer than five range markets (early 2022 S&P events were above/below only).
- PM2 prices only markets inside the quoted XSP strike range, about five per day near the money.
- PM5 traded on 31 of 65 releases: 414 market-entries had no quote within five minutes of 15:00 ET. Legacy
  2021-22 CPI markets carry the strike only in the subtitle ("Above 0.4%"), read as `greater`.
- Fee history is not modelled. From this network Kalshi answers about 35% of requests with HTTP 429.

## Not tested here, and why

- **Polymarket.** Polymarket US is CFTC-licensed and open to US residents, but its order book is thin and it has no
  public price history; US users can only close positions on the international site, which has the history.
- **Weather and sports.** A public study (Odds Reference, 2026) finds Kalshi's weather markets about efficient after
  fees; sports aren't a forecasting strength of this project, and several states contest them.
- **Execution.** Robinhood routes prediction-market orders to Kalshi, ForecastEx and Rothera, but its Agentic
  Trading MCP has no prediction-market tools, so the engine can't trade them. Live use would need a Kalshi account
  and its API.
