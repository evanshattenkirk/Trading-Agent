# Weekly Quant report

HANDOFF section 10, phase 6: every Friday after the close, one report per book.

```
python -m reporting.weekly_quant --journal ~/.agentdesk/journal.db --out ~/.agentdesk/reports
python -m reporting.weekly_quant --week-ending 2026-10-02      # a specific week (Friday)
```

Writes `quant-<friday>.md` and `quant-<friday>.json` and prints the markdown. Standard library only, and it opens
the journal read-only, so it is safe to run while the engine and the recorder are writing.

Per book, this week and since the start:
- trades, net P&L as filled and at taker (every fill re-priced at the natural), win rate with a Wilson CI,
  profit factor, mean and median per trade, bootstrap CI of the mean on risk, t and p, IQR outliers (kept)
- worst day, max drawdown on cumulative daily P&L, per-session t (trades in one session are not independent)
- fill quality: cents given up vs mid; fills with no quote to price against are counted
- the L2 split (observe mode): would-block vs pass
- promotion gates, HANDOFF 10.8 (E: the 7E gate), with the modeled backtest range as a sanity check

Plus the B/D go/no-go number from `option_quotes`: the 08:45 CT 0DTE ATM straddle mid vs the SPY move to 14:30 CT.

Five books are tested at once, so the report reads significance at p < 0.01 (Bonferroni), not 0.05. Only
`paper` and `shadow` trades count; `sim` never does.
