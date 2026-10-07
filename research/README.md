# Research scripts

Every script here, with the command that reruns it and what it writes. "Mac" means it needs keys or data that only
Evan's Mac has (Alpaca, ThetaData, CBOE, EDGAR); the rest run anywhere once research/data exists.

**Committed results predate the 2026-10-07 data fixes.** research_results.json, strategies_bcd_results.json,
strategies_new_results.json / _report.md, vrp_spreads.py's printed tables and the book D quiet-filter numbers quoted in
CLAUDE.md and HANDOFF.md were produced before research.py kept only forward-filled minutes, dropped sessions with no
09:30 bar and took prevC only from the prior NYSE session, and before strategies_bcd.py / vrp_spreads.py skipped days
whose last VIX close is more than 5 days old. f2c_drift.py now uses F2's 34 names with the section 3 floors (it had
no committed results). Rerun a script before quoting its numbers as current; small differences are expected.

## Data

    cd research && bash fetch_data.sh   # ~200 MB of public data into research/data; each GitHub source pinned to a commit
    cd research && python load_oanda.py    # -> data/spx_rth_1m.pkl (2,870 sessions, 2005-01..2020-05)

Data: S&P 500 CFD 1-minute bars (Oanda, via github.com/FutureSharks/financial-data), VIX daily
(github.com/datasets/finance-vix), SPY 5-minute bars 2025-04..2026-03 (github.com/vivek-v-rao/Intraday-Vol).
fetch_data.sh pins each to a commit SHA; change a SHA on purpose and say so next to any results it changes.
nyse_calendar.py (no command) holds NYSE closures and early closes for the scripts below.

## Intraday SPX studies (Black-Scholes from VIX, a model, not quotes)

research.py, vrp_spreads.py and vrp2.py read `data/...` relative to the current directory, so run them from research/.

    cd research && python research.py      # 11 pre-registered intraday strategies -> research_results.json
    cd research && python vrp_spreads.py   # variance risk premium + naked call vs debit spreads on the MACD entries (prints)
    cd research && python vrp2.py          # straddle / iron fly under two premium assumptions (prints)
    python research/strategies_bcd.py      # books B, C (both directions), D, cost/IV sensitivity -> strategies_bcd_results.json
    python research/d_quiet_check.py       # book D quiet filter, look-ahead vs known at entry -> d_quiet_check_results.json
    python research/strategies_new.py      # candidates F1-F4 (book G is F3) -> strategies_new_results.json, strategies_new_report.md

## Book A (Evan's MACD calls)

    python research/book_a_variants.py     # 2005-2020 variants -> book_a_variants_out/summary.json (+ trades/daily CSVs,
                                           # not committed); prices on the calendar clock on purpose (book_a_variants.md)
    python research/book_a_recenter_check.py --days 250   # synthetic check of the holdout's quote re-centring
                                           # -> book_a_recenter_check.json (reconstructed; see its docstring)
    .venv/bin/python research/book_a_holdout.py quotes --variants baseline,v4_slow_exits     # Mac: real ThetaData quotes
    .venv/bin/python research/book_a_holdout.py ticks --variants baseline,v5_swing_only      # Mac: SIP ticks
                                           # both -> book_a_holdout_out/<mode>/{summary.json, daily.csv, trades.csv}
    .venv/bin/python research/iex_vs_sip.py --days 10 --end 2026-09-25    # Mac (Alpaca keys): IEX vs SIP signals
                                           # -> iex_vs_sip_out/{summary.json, per_day.json, trades.csv}; --clock trading
                                           # (default) or calendar; the committed iex_vs_sip_out* folders (10 and 60
                                           # sessions) were run before --clock existed, on the calendar clock

## Real SPY 0DTE quotes (ThetaData, Mac)

    .venv/bin/python research/fetch_thetadata_spy0dte.py --smoke     # one recent Friday, printed, writes nothing
    .venv/bin/python research/fetch_thetadata_spy0dte.py             # resumable pull -> data/thetadata/spy_0dte/YYYY-MM-DD.parquet
    .venv/bin/python research/fetch_thetadata_spy_next.py            # next-expiry quotes -> data/thetadata/spy_next (G, H4)
    .venv/bin/python research/bd_real_quotes.py fetch-spy --start 2018-09-01 --end 2026-09-25 --out data/spy_1m
    .venv/bin/python research/bd_real_quotes.py run --quotes data/thetadata/spy_0dte --out data/thetadata/bd_results \
        [--spy data/spy_1m] [--vix research/data/vix.csv]           # books B and D -> bd_results/{report.md, results.json, trades.parquet}
    .venv/bin/python research/strategies_new_quotes.py --quotes data/thetadata/spy_0dte --out data/thetadata/new_results \
        [--spy data/spy_1m] [--next-quotes data/thetadata/spy_next]   # F1, F2 (+ F3 with --next-quotes)
                                           # -> new_results/{report.md, results.json, trades.parquet};
                                           # strategies_new_quotes_report.md is a committed copy of report.md

## Book F (shares) and F2 (single-name spreads)

Book F replication (docs/BOOK_F_HANDOFF.md section 5; Alpaca SIP 1-minute bars, 2016-2026, resumable, a few hours the
first time):

    python research/strategy_f_intraday.py                    # strategy_f_intraday.md / _results.json / strategy_f_equity.png
    python research/strategy_f_intraday.py --universe broad   # F1 v2 (strategy_f1_prereg.md) -> strategy_f1.md / _results.json

Book F daily study (HANDOFF v3.1 section 7F), rebuilt because the original strategy_f.py isn't in the repo; checked
against 7F's 2013-2018 table in strategy_f_daily.md. Any folder of per-symbol daily CSVs:

    python research/strategy_f_daily.py <folder> <out.json>
    python research/strategy_f_daily.py research/data/f_intraday/daily research/strategy_f_daily_results_2016_2026.json

Book F2 (rules in strategy_f2_prereg.md). The C premise on the cached shares data, then the real-quote replay once
ThetaData's equity options history is bought (resumable; only signal days are pulled):

    python research/f2c_drift.py                                 # f2c_drift.md / f2c_drift_results.json (f2c_drift_prereg.md)
    python research/f2_real_quotes.py pull                       # SIP 1-minute bars for F2's 34 names (Alpaca keys)
    python research/f2_real_quotes.py signals                    # research/data/f2/signals.csv
    python research/fetch_thetadata_equity.py --smoke            # one signal, printed; then without --smoke for all
                                                                 # -> data/thetadata/f2/{SYMBOL}_{day}_{setup}.parquet
    python research/f2_real_quotes.py run --earnings reports.csv # f2_real_quotes.md / .json

Other single-name candidates for the purchased data are drafted in strategies_equity_prereg.md, for Evan's approval
before any script is written.

## Book H candidates and later rounds

Book H (rules in book_h_candidates_prereg.md, frozen before any run). H1-H3 (shares) on the 2005-2020 data above, then
the 2020-2026 SIP holdout on the Mac for any stage-1 survivor; H4 (overnight 1DTE iron fly) on ThetaData quotes only:

    python research/book_h_candidates.py                                      # book_h_candidates.md / _results.json
    python research/book_h_candidates.py --coverage-min-minutes 300           # coverage check, not judged
    .venv/bin/python research/book_h_candidates.py --spy-1m data/spy_1m --holdout       # Mac: book_h_holdout.md
    .venv/bin/python research/h4_overnight_fly.py --quotes data/thetadata/spy_0dte \
        --next-quotes data/thetadata/spy_next --out data/thetadata/h4_results         # h4_overnight_fly.md / _results.json

Round 2, V1 short VIX futures carry (rules in vix_carry_prereg.md, frozen before any data). Mac only, since CBOE's
site isn't reachable from the cloud sessions; public CBOE data plus SVXY closes from Alpaca for the validation check:

    .venv/bin/python research/fetch_cboe_vx.py --smoke                        # one contract + VIX3M, printed
    .venv/bin/python research/fetch_cboe_vx.py --svxy                         # data/cboe
    .venv/bin/python research/vix_carry.py --data data/cboe --out research    # vix_carry.md / _results.json

Round 3, X1 short iron fly through earnings (rules in x1_earnings_fly_prereg.md, frozen before any data). Mac only:
SEC EDGAR for report times, ThetaData single-name option quotes, Alpaca daily bars for the date sanity check:

    .venv/bin/python research/fetch_earnings_edgar.py                         # research/data/x1/earnings.csv
    .venv/bin/python research/fetch_thetadata_x1.py --coverage                # does the plan reach 2018? printed
    .venv/bin/python research/fetch_thetadata_x1.py --daily                   # research/data/x1/daily.csv
    .venv/bin/python research/fetch_thetadata_x1.py                           # data/thetadata/x1
    .venv/bin/python research/x1_earnings_fly.py                              # x1_earnings_fly.md / _results.json

## Tests

    python -m pytest -q research/tests      # pyarrow is needed for the parquet tests (requirements.lock has it)
