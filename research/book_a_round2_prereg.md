# Book A round 2: stop and strike variants, pre-registration (frozen 2026-10-02, before any holdout result)

Round 1 (`book_a_variants.md`) found that A's −20% premium stop ends 42% of trades, many inside the first minute:
a 1-OTM 0DTE call loses 20% on a ~0.04% SPY move. These three variants came from seeing those results, so under
round 1's rule (section 4 of `book_a_variants_prereg.md`) they are tested only on the holdout, never on
2005-2020. Script: `book_a_holdout.py`. Nothing here changes book A's live rules; a variant that passes is a
proposal for Evan.

## Variants (one change each from `config.yaml` on main)

| Variant | Change | Idea |
|---|---|---|
| r2a_stop35 | `exits.stop_loss_pct: 0.35` | a stop wider than one minute's noise |
| r2b_spy_stop | no premium stop (`stop_loss_pct: 0.99`); sell everything when SPY trades 0.10% below the SPY price at entry (above it for a put). Breakeven and trailing stops after a scale are unchanged | stop on the chart, not on the option's leverage |
| r2c_itm1 | strikes: 1 ITM all day (`schedule: [{from: "08:30", base: -1, max: -1}]`) | a contract that moves less in % per SPY tick |

Every other rule (entries, scale-outs, trail, cross-back exits, time stop, risk limits) is as built.

## Where and how

Same as round 1's holdout (`book_a_variants_prereg.md` section 5), in the same Mac run:
- `quotes`: SPY SIP 1-minute bars from 2020-06-01 with ThetaData's real SPY 0DTE quotes, 1m trigger only.
- `ticks`: the 60 cached SIP sessions (2026-07 to 2026-09) with real 144-print bars, SWING and SCALP.
- Quotes from the previous minute's ThetaData row (never from the future). Fills: mid ±1c and taker.

## Pass bar (both, in `quotes` mode; `ticks` is reported as a check)

1. Mean daily P&L t-stat ≥ 2.33 at mid ±1c (one-sided p ≈ 0.01; 0.05 split over round 2's three variants plus
   v5, rounded to the same bar as round 1).
2. Mean daily P&L > 0 at taker fills.

A variant that beats the baseline but stays negative fails. Nothing here is tuned after the run: a different
stop size, SPY distance or strike is a round 3 that would need new data.
