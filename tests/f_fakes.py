"""Shared fakes for the book F tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CFG = {"enabled": True, "paper_only": True, "instrument": "shares", "scan_et": "09:35", "rvol5_min": 2.0, "top_n": 5,
       "first_candle": "green", "entry_cutoff_et": "10:30", "stop_atr_frac": 0.10, "exit_et": "15:55",
       "risk_per_trade": 25, "max_notional": 1000, "max_positions": 5, "daily_loss": 75,
       "universe": {"min_price": 10, "min_atr": 0.50, "min_dollar_vol_20d": 100_000_000, "top_sp500_by_dollar_vol": 130,
                    "extra": ["NVDA", "AMD", "AVGO", "MU", "TSM", "ARM", "MRVL", "SMCI", "WDC", "STX", "MSFT", "META",
                              "GOOGL", "AMZN", "AAPL", "ORCL", "PLTR", "TSLA"]},
       "news_tag": "observe", "shorts": "log_only"}
