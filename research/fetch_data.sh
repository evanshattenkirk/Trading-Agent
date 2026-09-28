#!/usr/bin/env bash
# Downloads the public datasets used by the research scripts into research/data (about 200 MB).
set -euo pipefail
cd "$(dirname "$0")"; mkdir -p data/oanda; cd data
curl -sL -o vix.csv https://raw.githubusercontent.com/datasets/finance-vix/main/data/vix-daily.csv
curl -sL -o spy_5m_2025_2026.csv https://raw.githubusercontent.com/vivek-v-rao/Intraday-Vol/main/SPY.csv
base=https://raw.githubusercontent.com/FutureSharks/financial-data/master/pyfinancialdata/data/currencies/oanda/SPX500_USD
for y in $(seq 2005 2020); do for m in $(seq 1 12); do
  f=oanda-SPX500_USD-$y-$m.csv; [ -f oanda/$f ] || curl -sfL -o oanda/$f $base/$y/$f || true
done; done
echo "done: $(ls oanda | wc -l) monthly files"
