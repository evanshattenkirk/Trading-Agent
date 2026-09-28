**FAIL**: book F replication, 2016-01-04 to 2026-09-25, primary spec after 2 bp costs. F still runs in paper, but only as a logging experiment. Not tuned to pass.

Pass bar: PF >= 1.1, t > 2 (clustered by day), and positive in both 2016-2020 and 2021-2026.

- Primary: 5020 trades, win 8.3%, mean R -0.412, mean -9.8 bp, PF 0.66, t -10.45, ann -4.1%, Sharpe -2.12
- 2016-2020: 2243 trades, win 8.7%, mean R -0.407, mean -8.8 bp, PF 0.64, t -6.95, ann -3.7%, Sharpe -2.19
- 2021-2026: 2777 trades, win 8.0%, mean R -0.416, mean -10.6 bp, PF 0.67, t -7.80, ann -4.5%, Sharpe -2.10
- AI/memory list, 2023-2026: 309 trades, win 8.7%, mean R -0.128, mean -1.2 bp, PF 0.99, t -0.62, ann -0.0%, Sharpe -0.02
- Worst day 2020-04-07 $-42; worst month 2023-10 $-132

By year:

- 2016: 373 trades, win 10.2%, mean R -0.268, mean -7.0 bp, PF 0.70, t -1.67, ann -2.4%, Sharpe -2.03
- 2017: 451 trades, win 7.5%, mean R -0.491, mean -8.8 bp, PF 0.52, t -3.97, ann -3.8%, Sharpe -3.87
- 2018: 501 trades, win 9.4%, mean R -0.363, mean -8.2 bp, PF 0.64, t -2.60, ann -4.0%, Sharpe -2.35
- 2019: 448 trades, win 9.2%, mean R -0.420, mean -8.6 bp, PF 0.64, t -3.78, ann -3.5%, Sharpe -2.83
- 2020: 470 trades, win 7.4%, mean R -0.472, mean -11.1 bp, PF 0.68, t -3.90, ann -4.8%, Sharpe -1.76
- 2021: 486 trades, win 7.0%, mean R -0.553, mean -14.5 bp, PF 0.47, t -5.36, ann -6.4%, Sharpe -5.01
- 2022: 400 trades, win 8.8%, mean R -0.398, mean -11.6 bp, PF 0.68, t -3.15, ann -4.3%, Sharpe -1.61
- 2023: 529 trades, win 8.9%, mean R -0.348, mean -9.8 bp, PF 0.64, t -2.66, ann -4.5%, Sharpe -3.01
- 2024: 512 trades, win 5.1%, mean R -0.682, mean -14.7 bp, PF 0.49, t -6.22, ann -6.7%, Sharpe -2.96
- 2025: 490 trades, win 9.8%, mean R -0.208, mean -5.1 bp, PF 0.86, t -1.35, ann -2.0%, Sharpe -0.87
- 2026: 360 trades, win 8.9%, mean R -0.257, mean -7.0 bp, PF 0.85, t -1.70, ann -2.5%, Sharpe -0.95

Sensitivity (reported, never selected from):

- rvol5_min=1.5: 7590 trades, win 9.8%, mean R -0.330, mean -7.6 bp, PF 0.74, t -10.06, ann -4.8%, Sharpe -1.90
- rvol5_min=3.0: 2468 trades, win 6.6%, mean R -0.509, mean -11.4 bp, PF 0.58, t -9.16, ann -2.4%, Sharpe -1.81
- top_n=3: 3862 trades, win 7.6%, mean R -0.453, mean -10.6 bp, PF 0.64, t -10.34, ann -3.4%, Sharpe -1.93
- top_n=10: 6197 trades, win 8.6%, mean R -0.405, mean -9.4 bp, PF 0.66, t -10.92, ann -4.9%, Sharpe -2.28
- stop_atr_frac=0.05: 5020 trades, win 1.9%, mean R -0.861, mean -11.2 bp, PF 0.31, t -22.14, ann -4.8%, Sharpe -4.32
- stop_atr_frac=0.2: 5020 trades, win 21.2%, mean R -0.118, mean -5.4 bp, PF 0.88, t -3.69, ann -2.3%, Sharpe -0.80
- slip_bp=5: 5020 trades, win 6.4%, mean R -0.699, mean -16.4 bp, PF 0.48, t -20.30, ann -7.0%, Sharpe -3.73

Caveats:

- Survivorship bias: today's S&P 500 list is used for every year.
- No macro-event scan skip (no historical event calendar).
- Half-days detected from the data (last bar before 15:00 ET).
- Fills on 1-minute bars: 2 bp slippage per side, no commission; entry and stop in one bar = stop.

---

Run by Evan on his Mac, 2026-09-28 (`.venv/bin/python research/strategy_f_intraday.py`, 18,881 Alpaca SIP calls). Copied from
the script's printed report. matplotlib wasn't installed, so there is no equity-curve PNG; the results JSON stays on the Mac.

**Reading:** every year and every sensitivity loses. The same-bar rule (entry and stop inside one 1-minute bar count as
stopped) is the brief's own and is pessimistic for a 0.10 x ATR stop, but the 0.20 x ATR run, which it affects less, still
loses (PF 0.88, t -3.7), so it does not explain the result. The daily rerun (`strategy_f_daily.md`) agrees: nothing to buy
after a high-volume up day, and the gap chase still loses.
