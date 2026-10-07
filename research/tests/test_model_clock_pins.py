"""M19: backtest.ModelQuotes now prices on the trading-day clock. The committed book A variant tables were made on the
calendar clock, so book_a_variants.py pins it (its results must reproduce); iex_vs_sip.py exposes it as --clock."""
import re
from pathlib import Path

RESEARCH = Path(__file__).resolve().parents[1]


def test_book_a_variants_pins_the_calendar_clock():
    src = (RESEARCH / "book_a_variants.py").read_text()
    calls = re.findall(r"ModelQuotes\(([^)]*)\)", src)
    assert calls == ['feed, iv, clock="calendar"']


def test_iex_vs_sip_passes_its_clock_to_every_replay():
    src = (RESEARCH / "iex_vs_sip.py").read_text()
    assert "ModelQuotes(feed, iv, clock=clock)" in src
    assert re.search(r'"--clock", choices=\["trading", "calendar"\], default="trading"', src)
    assert len(re.findall(r"await replay\(", src)) == len(re.findall(r"await replay\([^)]*args\.clock\)", src, re.S)) == 3


def test_the_variants_writeup_carries_the_cheap_option_label():
    md = (RESEARCH / "book_a_variants.md").read_text()
    head = md[:3000]
    assert "cheap-option model" in head and "calendar" in head and "holdout" in head
    assert "cheap-option model" in (RESEARCH / "book_a_variants_prereg.md").read_text()
