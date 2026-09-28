"""Weekly per-book Quant report (HANDOFF section 10, phase 6)."""
import json
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from reporting import weekly_quant as wq

CT = ZoneInfo("America/Chicago")

# trades schema as PR #6 (multi-book framework) writes it: book, legs, max_loss added
SCHEMA = """
CREATE TABLE trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session TEXT, mode TEXT, contract TEXT, occ TEXT, setup TEXT,
  qty INTEGER, entry REAL, opened_ts REAL, closed_ts REAL,
  realized REAL, fees REAL, pnl REAL, pnl_pct REAL, peak REAL,
  exit_reason TEXT, strike_reason TEXT, entry_reasons TEXT, fills TEXT, l2 TEXT,
  book TEXT DEFAULT 'A', legs TEXT, max_loss REAL
);
CREATE TABLE option_quotes (ts REAL, expiry TEXT, strike REAL, right TEXT, bid REAL, ask REAL, spot REAL);
"""


def ts(day: str, hm: str) -> float:
    return datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=CT).timestamp()


def add(db, book, session, pnl, pct=None, mode="paper", fills=None, l2=None, exit_reason="tp", max_loss=150.0):
    db.execute("INSERT INTO trades (session,mode,book,pnl,pnl_pct,fills,l2,exit_reason,max_loss,qty,occ,opened_ts,closed_ts)"
               " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (session, mode, book, pnl, pnl / max_loss if pct is None else pct, json.dumps(fills or []),
                json.dumps(l2), exit_reason, max_loss, 1, "", ts(session, "09:00"), ts(session, "10:00")))


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "journal.db"
    c = sqlite3.connect(p)
    c.executescript(SCHEMA)
    yield p, c
    c.close()


# ---------------------------------------------------------------- statistics

def test_t_two_sided_p_matches_student_t():
    assert wq.t_pvalue(2.228, 10) == pytest.approx(0.05, abs=5e-4)
    assert wq.t_pvalue(2.0, 10) == pytest.approx(0.0734, abs=5e-4)
    assert wq.t_pvalue(0.0, 5) == pytest.approx(1.0)


def test_wilson_interval():
    lo, hi = wq.wilson(7, 10)
    assert lo == pytest.approx(0.397, abs=1e-3) and hi == pytest.approx(0.892, abs=1e-3)
    assert wq.wilson(0, 0) == (None, None)


def test_trade_stats_core_numbers():
    s = wq.trade_stats([10, -5, 20, -5, 30])
    assert s["n"] == 5 and s["net"] == 50 and s["mean"] == 10 and s["median"] == 10
    assert s["win_rate"] == pytest.approx(0.6)
    assert s["pf"] == pytest.approx(60 / 10)
    sd = (sum((x - 10) ** 2 for x in [10, -5, 20, -5, 30]) / 4) ** 0.5
    assert s["t"] == pytest.approx(10 / (sd / 5 ** 0.5))
    lo, hi = s["mean_ci"]
    assert lo < 10 < hi


def test_trade_stats_edge_cases():
    assert wq.trade_stats([])["n"] == 0
    one = wq.trade_stats([5])
    assert one["t"] is None and one["pf"] is None     # no losses: PF undefined, shown as inf-free None
    assert wq.trade_stats([-3, -2])["pf"] == 0.0


def test_iqr_outliers_counted_not_removed():
    s = wq.trade_stats([1, 2, 1, 2, 1, 2, 1, 2, 500])
    assert s["n"] == 9 and s["outliers"] == 1


def test_drawdown_and_worst_day():
    d = wq.daily_stats({"2026-10-01": 100, "2026-10-02": -150, "2026-10-05": -50, "2026-10-06": 300})
    assert d["worst_day"] == ("2026-10-02", -150)
    assert d["max_drawdown"] == -200            # peak +100 -> trough -100
    assert d["sessions"] == 4


# ---------------------------------------------------------------- fills and taker net

def test_combo_taker_uses_logged_natural():
    t = {"pnl": 20.0, "occ": "a,b,c,d", "fills": [
        {"ts": 1, "side": "open", "qty": 1, "px": 1.50, "mid": 1.52, "natural": 1.44},
        {"ts": 2, "side": "close", "qty": 1, "px": 1.30, "mid": 1.28, "natural": 1.34}]}
    r = wq.fill_quality(t, quotes=None)
    assert r["pnl_taker"] == pytest.approx(20 - 6 - 4)
    assert r["vs_mid_c"] == pytest.approx(4.0)     # 2c + 2c given up to mid
    assert r["priced"] == 2 and r["unpriced"] == 0


def test_single_leg_taker_from_recorded_quotes(db):
    p, c = db
    day = "2026-10-01"
    c.execute("INSERT INTO option_quotes VALUES (?,?,?,?,?,?,?)", (ts(day, "09:00") - 4, day, 765.0, "call", 1.00, 1.10, 765))
    c.execute("INSERT INTO option_quotes VALUES (?,?,?,?,?,?,?)", (ts(day, "09:20") + 3, day, 765.0, "call", 1.40, 1.50, 766))
    c.commit()
    q = wq.QuoteBook(p)
    t = {"pnl": 30.0, "occ": "SPY261001C00765000", "fills": [
        {"ts": ts(day, "09:00"), "side": "buy", "qty": 1, "px": 1.08, "why": "entry"},
        {"ts": ts(day, "09:20"), "side": "sell", "qty": 1, "px": 1.45, "why": "stop"}]}
    r = wq.fill_quality(t, quotes=q)
    assert r["pnl_taker"] == pytest.approx(30 - 2 - 5)   # ask 1.10 vs 1.08; bid 1.40 vs 1.45
    assert r["vs_mid_c"] == pytest.approx(3 + 0)          # paid 3c over mid 1.05; sold at mid
    assert r["priced"] == 2


def test_single_leg_without_nearby_quote_counts_unpriced(db):
    p, _ = db
    t = {"pnl": 30.0, "occ": "SPY261001C00765000", "fills": [{"ts": 1.0, "side": "buy", "qty": 1, "px": 1.0}]}
    r = wq.fill_quality(t, quotes=wq.QuoteBook(p))
    assert r["pnl_taker"] == 30.0 and r["unpriced"] == 1


def test_parse_occ():
    assert wq.parse_occ("SPY261001C00765500") == ("2026-10-01", 765.5, "call")
    assert wq.parse_occ("a,b") is None


# ---------------------------------------------------------------- loading and filtering

def test_load_excludes_sim_and_defaults_book_a(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, session TEXT, mode TEXT, pnl REAL, pnl_pct REAL, fills TEXT,"
              " l2 TEXT, exit_reason TEXT, occ TEXT, qty INTEGER)")    # pre-PR #6 journal: no book column
    c.execute("INSERT INTO trades (session,mode,pnl,pnl_pct,fills) VALUES ('2026-10-01','paper',5,0.01,'[]')")
    c.execute("INSERT INTO trades (session,mode,pnl,pnl_pct,fills) VALUES ('2026-10-01','sim',99,0.2,'[]')")
    c.commit()
    rows = wq.load_trades(p)
    assert len(rows) == 1 and rows[0]["book"] == "A"


def test_week_bounds():
    assert wq.week_bounds(date(2026, 10, 2)) == ("2026-09-28", "2026-10-02")
    assert wq.last_friday(date(2026, 10, 4)) == date(2026, 10, 2)
    assert wq.last_friday(date(2026, 10, 2)) == date(2026, 10, 2)


# ---------------------------------------------------------------- L2 split

def test_l2_split_groups_by_would_block():
    trades = [{"pnl": 10, "l2": {"would_block": False}}, {"pnl": 20, "l2": {"would_block": False}},
              {"pnl": -30, "l2": {"would_block": True}}, {"pnl": 5, "l2": None}]
    s = wq.l2_split(trades)
    assert s["pass"]["n"] == 2 and s["pass"]["mean"] == 15
    assert s["would_block"]["n"] == 1 and s["no_l2"]["n"] == 1


# ---------------------------------------------------------------- promotion gates

def _trades(book, n, pnl, sessions):
    return [{"book": book, "session": f"2026-10-{1 + i % sessions:02d}", "pnl": pnl(i), "pnl_taker": pnl(i) - 1,
             "pnl_pct": pnl(i) / 150, "pct_taker": (pnl(i) - 1) / 150, "exit_reason": "tp"} for i in range(n)]


def test_gates_small_sample_fails_counts():
    g = wq.promotion_gates("B", _trades("B", 10, lambda i: 20 if i % 3 else -15, 5))
    by = {x["name"]: x for x in g}
    assert by["sessions"]["ok"] is False and by["trades"]["ok"] is False


def test_gates_pf_and_range_and_order_state():
    tr = _trades("D", 120, lambda i: 12 if i % 4 else -20, 25)
    tr[3]["exit_reason"] = "halt: position mismatch"
    by = {x["name"]: x for x in wq.promotion_gates("D", tr)}
    assert by["sessions"]["ok"] and by["trades"]["ok"]
    assert by["pf_taker"]["ok"]                       # 90*11 / (30*21)
    assert by["in_backtest_range"]["ok"] is not None
    assert by["order_state"]["ok"] is False           # a halt needs Evan to confirm it was resolved


def test_gates_book_e_uses_its_own_gate():
    tr = _trades("E", 120, lambda i: 30 if i % 3 else -10, 25)
    tr[0]["session"] = "2026-11-02"
    names = {x["name"] for x in wq.promotion_gates("E", tr)}
    assert {"events", "mean_taker", "t_taker", "month_share"} <= names


def test_month_share():
    assert wq.max_month_share([{"session": "2026-10-01", "pnl_taker": 60}, {"session": "2026-11-01", "pnl_taker": 40}]) \
        == pytest.approx(0.6)
    assert wq.max_month_share([{"session": "2026-10-01", "pnl_taker": -5}]) is None


def test_within_range_uses_ci_overlap():
    assert wq.overlaps((0.01, 0.05), (-0.07, 0.03)) is True
    assert wq.overlaps((0.10, 0.20), (-0.07, 0.03)) is False
    assert wq.overlaps((None, None), (-0.07, 0.03)) is None


# ---------------------------------------------------------------- straddle vs realized (B/D go/no-go)

def test_straddle_vs_realized(db):
    p, c = db
    for day, s_in, s_out in [("2026-09-28", 765.0, 767.0), ("2026-09-29", 765.2, 764.7)]:
        rows = [(ts(day, "08:45"), day, 765.0, "call", 1.90, 2.10, s_in), (ts(day, "08:45"), day, 765.0, "put", 1.90, 2.10, s_in),
                (ts(day, "08:45"), day, 766.0, "call", 1.40, 1.60, s_in),
                (ts(day, "14:30"), day, 765.0, "call", 0.1, 0.2, s_out)]
        c.executemany("INSERT INTO option_quotes VALUES (?,?,?,?,?,?,?)", rows)
    c.commit()
    r = wq.straddle_vs_realized(p, "2026-09-28", "2026-10-02")
    assert r["days"] == 2
    assert r["mean_straddle"] == pytest.approx(4.0)
    assert r["mean_move"] == pytest.approx((2.0 + 0.5) / 2)
    assert r["mean_edge"] == pytest.approx(4.0 - 1.25)


# ---------------------------------------------------------------- end to end

def test_build_and_render_report(db):
    p, c = db
    fills = [{"ts": 1, "side": "open", "qty": 1, "px": 1.5, "mid": 1.51, "natural": 1.46},
             {"ts": 2, "side": "close", "qty": 1, "px": 0.75, "mid": 0.74, "natural": 0.79}]
    for i, day in enumerate(["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]):
        add(c, "B", day, 70 if i % 2 == 0 else -40, fills=fills)
        add(c, "A", day, 25, max_loss=None, pct=0.05, l2={"would_block": i == 1})
    add(c, "B", "2026-09-21", 10, fills=fills)        # prior week: cumulative only
    add(c, "B", "2026-10-02", 999, mode="sim")         # sim never counts
    c.commit()
    rep = wq.build_report(p, date(2026, 10, 2))
    b = rep["books"]["B"]
    assert b["week"]["n"] == 5 and b["cumulative"]["n"] == 6
    assert b["week"]["net"] == pytest.approx(3 * 70 - 2 * 40)
    assert b["week_taker"]["net"] == pytest.approx(130 - 5 * 8)
    md = wq.render_markdown(rep)
    assert "Book B" in md and "Book A" in md and "Promotion gates" in md
    assert "999" not in md
    assert "Bonferroni" in md
    json.dumps(rep)                                    # serializable for the routine


def test_empty_journal_renders(db):
    p, _ = db
    md = wq.render_markdown(wq.build_report(p, date(2026, 10, 2)))
    assert "No paper trades" in md


def test_cli_writes_files(db, tmp_path):
    p, c = db
    add(c, "D", "2026-10-01", 12)
    c.commit()
    out = tmp_path / "reports"
    assert wq.main(["--journal", str(p), "--week-ending", "2026-10-02", "--out", str(out)]) == 0
    assert (out / "quant-2026-10-02.md").exists() and (out / "quant-2026-10-02.json").exists()
