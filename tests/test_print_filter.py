"""feeds/prints.py: drop SPY prints that don't set the last price, and isolated bad prints, before bars are built."""
import asyncio
import json

from agentdesk.bars import Trade
from agentdesk.feeds import alpaca
from agentdesk.feeds.prints import EXCLUDE_CONDITIONS, PrintFilter


def _run(f: PrintFilter, prints):
    out = []
    for t, p, c in prints:
        out += f.push(Trade(float(t), p, 100.0), c)
    return out


def test_drops_prints_whose_condition_does_not_set_the_last_price():
    f = PrintFilter()
    out = _run(f, [(1, 700.0, ["@"]), (2, 700.1, ["@", "I"]), (3, 690.0, ["@", "Z"]), (4, 700.2, ["@", "F"]), (5, 700.3, None)])
    assert [t.px for t in out] == [700.0, 700.1, 700.2, 700.3]
    assert f.stats == {"kept": 4, "dropped_condition": 1, "dropped_outlier": 0}


def test_excluded_conditions_cover_the_cta_utdf_non_last_sale_codes():
    for code in ("Z", "U", "T", "4", "9", "W"):      # out of sequence, extended hours, prior reference, average price
        assert code in EXCLUDE_CONDITIONS
    for code in ("@", "F", "I", "O", "6", "X"):      # regular, ISO, odd lot, opening, closing, cross: these update last
        assert code not in EXCLUDE_CONDITIONS


def test_drops_an_isolated_spike():
    px = [770.0 + 0.01 * (i % 3) for i in range(60)]
    px[30] = 829.0                                  # a single +7% print, like SIP on 2026-08-11
    f = PrintFilter(max_dev=0.005)
    out = _run(f, [(i, p, ["@"]) for i, p in enumerate(px)])
    assert 829.0 not in [t.px for t in out]
    assert f.stats["dropped_outlier"] == 1 and f.stats["kept"] == 59


def test_follows_a_real_gap_and_keeps_the_confirming_prints_in_order():
    px = [770.0] * 30 + [775.0] * 30                # a genuine 0.65% jump that holds
    f = PrintFilter(max_dev=0.005, confirm=3)
    out = _run(f, [(i, p, None) for i, p in enumerate(px)])
    assert [t.px for t in out] == px                # nothing lost: the first 3 gap prints are held, then released
    assert [t.ts for t in out] == sorted(t.ts for t in out)
    assert f.stats["dropped_outlier"] == 0


def test_a_gap_print_is_held_until_confirmed():
    f = PrintFilter(max_dev=0.005, confirm=3)
    for i in range(20):
        assert len(f.push(Trade(float(i), 770.0, 100.0))) == 1
    assert f.push(Trade(20.0, 775.0, 100.0)) == []
    assert f.push(Trade(21.0, 775.1, 100.0)) == []
    released = f.push(Trade(22.0, 775.0, 100.0))
    assert [t.ts for t in released] == [20.0, 21.0, 22.0]


def test_two_spikes_in_opposite_directions_are_both_dropped():
    f = PrintFilter(max_dev=0.005)
    px = [770.0] * 20 + [800.0, 740.0] + [770.0] * 10
    out = _run(f, [(i, p, None) for i, p in enumerate(px)])
    assert 800.0 not in [t.px for t in out] and 740.0 not in [t.px for t in out]
    assert f.stats["dropped_outlier"] == 2


def test_disabled_filter_passes_everything():
    f = PrintFilter(enabled=False)
    out = _run(f, [(1, 700.0, ["Z"]), (2, 829.0, None)])
    assert [t.px for t in out] == [700.0, 829.0]


class _FakeResp:
    def __init__(self, j):
        self._j = j

    def raise_for_status(self):
        pass

    def json(self):
        return self._j


def test_fetch_trades_filters_rest_prints(monkeypatch):
    page = {"trades": [{"t": f"2026-08-11T14:{30 + i // 60:02d}:{i % 60:02d}Z", "p": 770.0, "s": 100, "c": ["@"]} for i in range(40)],
            "next_page_token": None}
    page["trades"][10]["c"] = ["@", "Z"]
    page["trades"][10]["p"] = 700.0
    page["trades"][20]["p"] = 829.0

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            return _FakeResp(page)

    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    monkeypatch.setattr(alpaca.httpx, "AsyncClient", FakeClient)

    async def collect(**kw):
        return [t async for t in alpaca.fetch_trades("SPY", _Dt(), _Dt(), "sip", **kw)]

    kept = asyncio.run(collect())
    assert len(kept) == 38 and {t.px for t in kept} == {770.0}
    raw = asyncio.run(collect(clean=False))
    assert len(raw) == 40


class _Dt:
    def isoformat(self):
        return "2026-08-11T14:30:00+00:00"


def test_websocket_messages_are_filtered(monkeypatch):
    cfg = {"symbol": "SPY", "data": {"alpaca": {"feed": "sip"}}}
    feed = alpaca.AlpacaFeed(cfg)
    msgs = [{"T": "t", "t": f"2026-08-11T14:30:{i:02d}Z", "p": 770.0, "s": 100, "c": ["@"]} for i in range(30)]
    msgs[5]["c"] = ["@", "U"]
    msgs[12]["p"] = 829.0
    asyncio.run(feed._handle(json.dumps(msgs)))
    got = []
    while not feed.q.empty():
        got.append(feed.q.get_nowait())
    assert len(got) == 28 and {t.px for t in got} == {770.0}
    assert feed.prints.stats["dropped_condition"] == 1 and feed.prints.stats["dropped_outlier"] == 1


def test_feed_filter_can_be_turned_off_in_config():
    cfg = {"symbol": "SPY", "data": {"alpaca": {"feed": "sip", "clean_prints": False}}}
    assert alpaca.AlpacaFeed(cfg).prints.enabled is False
    cfg["data"]["alpaca"].pop("clean_prints")
    assert alpaca.AlpacaFeed(cfg).prints.enabled is True
