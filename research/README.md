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

Book F1 v2 on the broad universe (rules in strategy_f1_prereg.md; free SIP history, a larger pull):

    python research/strategy_f_intraday.py --universe broad      # writes strategy_f1.md / strategy_f1_results.json

Book F2 (rules in strategy_f2_prereg.md). The C premise on the shares data already cached, then the real-quote replay
once ThetaData's equity options history is bought (resumable; only signal days are pulled):

    python research/f2c_drift.py                                 # f2c_drift.md / f2c_drift_results.json (f2c_drift_prereg.md)
    python research/f2_real_quotes.py pull                       # SIP 1-minute bars for F2's 34 names (Alpaca keys)
    python research/f2_real_quotes.py signals                    # research/data/f2/signals.csv
    python research/fetch_thetadata_equity.py --smoke            # one signal, printed; then without --smoke for all
    python research/f2_real_quotes.py run --earnings reports.csv # f2_real_quotes.md / .json

Other single-name candidates for the purchased data are drafted in strategies_equity_prereg.md, for Evan's approval
before any script is written. nyse_calendar.py holds the NYSE closures and early closes the F2 scripts use.

Book F daily study (HANDOFF v3.1 section 7F), rebuilt because the original strategy_f.py isn't in the repo; checked
against 7F's 2013-2018 table in strategy_f_daily.md. Any folder of per-symbol daily CSVs:

    python research/strategy_f_daily.py <folder> <out.json>
    python research/strategy_f_daily.py research/data/f_intraday/daily research/strategy_f_daily_results_2016_2026.json

Book H candidates (rules in book_h_candidates_prereg.md, frozen before any run). H1-H3 (shares) on the 2005-2020 data
above, then the 2020-2026 SIP holdout on the Mac for any stage-1 survivor; H4 (overnight 1DTE iron fly) on ThetaData
quotes only:

    python research/book_h_candidates.py                                      # book_h_candidates.md / _results.json
    python research/book_h_candidates.py --coverage-min-minutes 300           # coverage check, not judged
    .venv/bin/python research/book_h_candidates.py --spy-1m data/spy_1m --holdout       # Mac: book_h_holdout.md
    .venv/bin/python research/fetch_thetadata_spy_next.py                    # Mac: next-expiry quotes (also feeds book G's check)
    .venv/bin/python research/h4_overnight_fly.py --quotes data/thetadata/spy_0dte \
        --next-quotes data/thetadata/spy_next --out data/thetadata/h4_results

Round 2, V1 short VIX futures carry (rules in vix_carry_prereg.md, frozen before any data). Mac only, since CBOE's
site isn't reachable from the cloud sessions; public CBOE data plus SVXY closes from Alpaca for the validation check:

    .venv/bin/python research/fetch_cboe_vx.py --smoke                        # one contract + VIX3M, printed
    .venv/bin/python research/fetch_cboe_vx.py --svxy                         # data/cboe
    .venv/bin/python research/vix_carry.py --data data/cboe --out research    # vix_carry.md / _results.json
