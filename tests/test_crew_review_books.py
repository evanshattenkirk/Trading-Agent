"""Crew review, book side: each book sizes with its own crew multiplier, entries carry the crew's effect, and
blocks from crew reads reach the crew log (docs/superpowers/specs/2026-09-29-crew-review-changes.md)."""
import asyncio

from books_fakes import FakeQuotes, ct_ts
from test_books_host import FLY, setup, tick


class Crew:
    def __init__(self):
        self.blocks, self.briefs = [], {}

    def effect(self, book, qty, qty_1x, up=1.0, tweaks=None):
        return {"book": book, "qty": qty, "qty_1x": qty_1x}

    def note_block(self, book, why, now):
        self.blocks.append((book, why))


def test_each_book_sizes_with_its_own_crew_multiplier_and_saves_the_effect():
    eng, fq, host = setup(intents=("C",))
    eng.crew = Crew()
    eng.risk.set_book_mults({"A": 0.5, "C": 0.75})
    seen = []
    size = host.account.size
    host.account.size = lambda per_lot, lots=None, budget=None, mult=1.0: (seen.append(mult), size(per_lot, lots, budget, mult))[1]
    tick(host, fq, ct_ts(8, 45))
    (pos,) = host.positions()
    assert seen[0] == 0.75 and 1.0 in seen[1:]
    assert pos.meta["crew"] == {"book": "C", "qty": 1, "qty_1x": 1}


def test_a_blackout_skip_reaches_the_crew_log():
    eng, fq, host = setup(intents=("B",))
    eng.crew = Crew()
    eng.risk.add_blackout(ct_ts(8, 50), "JOLTS")
    tick(host, fq, ct_ts(8, 45))
    assert eng.crew.blocks == [("B", "blackout: JOLTS")]
