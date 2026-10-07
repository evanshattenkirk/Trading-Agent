#!/usr/bin/env bash
# Downloads the public datasets used by the research scripts into research/data (about 200 MB).
# Each GitHub source is pinned to the commit its branch pointed at on 2026-10-07 (ls-remote), so a rerun gets the
# same files from now on. The committed tables were made from earlier fetches of the same branches, before the pins.
# To move to newer data, update a SHA on purpose and say so next to the results.
set -euo pipefail
VIX_SHA=3dbea23c70cc2afbeeec8ac232c9ec72ac26ac21        # datasets/finance-vix main
SPY5M_SHA=3a1b40fea9dbed724275de4fa9e031c3816265c0      # vivek-v-rao/Intraday-Vol main
OANDA_SHA=7ba1d404aa8b0e1c0f71321acebadcbfb9bcca8d      # FutureSharks/financial-data master
cd "$(dirname "$0")"; mkdir -p data/oanda; cd data
curl -sfL -o vix.csv https://raw.githubusercontent.com/datasets/finance-vix/$VIX_SHA/data/vix-daily.csv
curl -sfL -o spy_5m_2025_2026.csv https://raw.githubusercontent.com/vivek-v-rao/Intraday-Vol/$SPY5M_SHA/SPY.csv
base=https://raw.githubusercontent.com/FutureSharks/financial-data/$OANDA_SHA/pyfinancialdata/data/currencies/oanda/SPX500_USD
for y in $(seq 2005 2020); do for m in $(seq 1 12); do
  f=oanda-SPX500_USD-$y-$m.csv; [ -f oanda/$f ] || curl -sfL -o oanda/$f $base/$y/$f || true
done; done
echo "done: $(ls oanda | wc -l) monthly files"
