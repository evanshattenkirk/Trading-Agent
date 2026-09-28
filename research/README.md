# Research scripts

Reproduce the numbers quoted in the chat:

    ./fetch_data.sh                 # ~200 MB of public data into research/data
    python load_oanda.py            # builds data/spx_rth_1m.pkl (2,870 sessions, 2005-01..2020-05)
    python research.py              # 11 pre-registered intraday strategies: in-sample 2005-14, out-of-sample 2015-20, SPY 2025-26
    python vrp_spreads.py           # variance risk premium + naked call vs debit spreads on your MACD entries
    python vrp2.py                  # straddle / iron fly under two premium assumptions

Data: S&P 500 CFD 1-minute bars (Oanda, via github.com/FutureSharks/financial-data), VIX daily
(github.com/datasets/finance-vix), SPY 5-minute bars 2025-04..2026-03 (github.com/vivek-v-rao/Intraday-Vol).
Option prices in vrp_spreads.py / vrp2.py are Black-Scholes with VIX-implied vol; they are a model, not quotes.

Book F replication (docs/BOOK_F_HANDOFF.md section 5; Alpaca SIP 1-minute bars, 2016-2026, resumable, a few hours the first time):

    python research/strategy_f_intraday.py      # writes strategy_f_intraday.md / _results.json / strategy_f_equity.png

Book F daily study (HANDOFF v3.1 section 7F), rebuilt because the original strategy_f.py isn't in the repo; checked
against 7F's 2013-2018 table in strategy_f_daily.md. Any folder of per-symbol daily CSVs:

    python research/strategy_f_daily.py <folder> <out.json>
    python research/strategy_f_daily.py research/data/f_intraday/daily research/strategy_f_daily_results_2016_2026.json
