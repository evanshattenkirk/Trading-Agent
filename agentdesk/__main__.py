"""AgentDesk CLI.

  python -m agentdesk run [--mode sim|paper|shadow|live] [--speed 60]   dashboard + engine
  python -m agentdesk rh-inspect                                         connect to Robinhood MCP, dump tool schemas (read-only)
  python -m agentdesk backtest --days 20 [--ticks] [--options model|alpaca]
  python -m agentdesk record-demo --seed 21 --out demo.jsonl             record a sim day for the demo page
  python -m agentdesk record-quotes [--once|--status|--report|--probe]   standalone 0DTE quote recorder (read-only)
  python -m agentdesk f-report [--since YYYY-MM-DD]                      book F paper record
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import webbrowser
from datetime import date

from .config import expand, load_config, set_tick_bar_for_feed


def check_live_promotion(cfg, mode: str) -> None:
    """Live mode needs exactly one promoted book (paper_only: false). Only book A has a live order path in this
    build, so A is the only book that can be promoted (HANDOFF sections 10.8 and 12)."""
    if mode != "live":
        return
    promoted = [k for k, v in (cfg.get("books") or {}).items()
                if isinstance(v, dict) and v.get("enabled") and v.get("paper_only") is False]
    if promoted != ["A_macd_calls"]:
        raise SystemExit("Refusing live mode: set paper_only: false on exactly one book, and only A_macd_calls has a "
                         f"live order path. Promoted now: {promoted or 'none'}.")


def build(cfg, mode: str, speed: float, seed: int, sim_day: str | None = None):
    check_live_promotion(cfg, mode)
    from .brokers.paper import PaperBroker
    from .bus import Bus
    from .crew import Crew
    from .engine import Engine
    from .journal import Journal

    provider = "sim" if mode == "sim" else cfg["data"]["provider"]
    if provider == "sim":
        from .feeds.sim import SimFeed, SimQuotes
        feed = SimFeed(day=date.fromisoformat(sim_day) if sim_day else None, seed=seed, speed=speed)
        quotes = SimQuotes(feed)
    elif provider == "alpaca":
        from .feeds.alpaca import AlpacaFeed, AlpacaQuotes
        n = set_tick_bar_for_feed(cfg, cfg["data"]["alpaca"]["feed"])
        if n != cfg["strategy"]["tick_bar_size"]:
            logging.getLogger("agentdesk").warning("IEX feed: 144t series built from %s IEX prints (approximation)", n)
        feed = AlpacaFeed(cfg)
        quotes = AlpacaQuotes(cfg)
    elif provider == "massive":
        from .feeds.massive import MassiveFeed, MassiveQuotes
        feed = MassiveFeed(cfg)
        quotes = MassiveQuotes(cfg)
    else:
        raise SystemExit(f"unknown data.provider {provider}")

    rh = None
    if mode in ("paper", "shadow", "live") and (mode != "paper" or cfg["data"]["quotes_source"] in ("auto", "robinhood")):
        from .brokers.robinhood import RobinhoodMCP
        rh = RobinhoodMCP(cfg)
    if rh and cfg["data"]["quotes_source"] in ("auto", "robinhood"):
        from .brokers.robinhood import RobinhoodQuotes
        quotes = RobinhoodQuotes(rh)

    if mode in ("sim", "paper"):
        broker = PaperBroker(quotes)
    elif mode == "shadow":
        from .brokers.robinhood import RobinhoodBroker
        broker = RobinhoodBroker(rh, quotes, live=False)
    elif mode == "live":
        if not cfg.get("live_enabled"):
            raise SystemExit("Refusing live mode: set live_enabled: true in config.yaml first.")
        from .brokers.robinhood import RobinhoodBroker
        broker = RobinhoodBroker(rh, quotes, live=True)
    else:
        raise SystemExit(f"unknown mode {mode}")

    bus = Bus()
    journal = Journal(expand(cfg["journal_path"]) if mode != "sim" else None)
    engine = Engine(cfg, feed, quotes, broker, bus, journal, mode)
    if mode != "sim":                          # today's risk state survives a restart (sim days are synthetic)
        from .risk import RiskStore
        engine.risk.store = RiskStore(expand(cfg["risk"].get("state_path", "~/.agentdesk/risk_state_{mode}.json")
                                             .format(mode=mode)))
    engine.crew = Crew(engine, cfg)
    from .l2 import L2Monitor, SimBook
    engine.l2 = L2Monitor(cfg, symbol=cfg["symbol"])
    if provider == "sim":
        engine.simbook = SimBook(seed)
    elif rh is not None:
        engine.l2_rh = rh
    from .books.host import BookHost
    from .books.vol import RobinhoodVix, SimVix
    vix = SimVix(feed) if provider == "sim" else (RobinhoodVix(rh) if rh is not None else None)
    host = BookHost(engine, cfg, vix=vix, reviewer=rh if mode in ("shadow", "live") else None)
    from .books.f_host import build_f
    from .books.group import HostGroup
    group = HostGroup.of(host, build_f(engine, cfg, rh=rh, mode=mode, provider=provider, feed=feed))
    engine.books = group if group.enabled else None
    engine.closers = [feed.close] + ([rh.close] if rh is not None else [])     # run on shutdown, each with a timeout
    return engine, bus


def cmd_run(args) -> None:
    import uvicorn
    from . import lifecycle
    from .server import LOCAL_HOSTS, create_app, resolve_token

    cfg = load_config(args.config)
    mode = args.mode or cfg["mode"]
    engine, bus = build(cfg, mode, args.speed, args.seed, args.day)
    engine.risk.clear_halt_on_restore = args.clear_halt
    host, port = cfg["server"]["host"], cfg["server"]["port"]
    token = resolve_token(cfg["server"])
    app = create_app(engine, bus, token=token, allowed_hosts=[host, *(cfg["server"].get("allowed_hosts") or [])])
    if host not in LOCAL_HOSTS:
        logging.getLogger("agentdesk").warning("dashboard bound to %s: reachable from the network (controls still need "
                                               "the token)", host)
    link = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/#token={token}"

    async def main():
        server = lifecycle.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                                 timeout_graceful_shutdown=2))
        print(f"\n  AgentDesk [{mode.upper()}] -> {link}\n  (open this link for the controls; Ctrl-C to stop)\n",
              flush=True)
        if not args.no_browser:
            webbrowser.open(link)
        return await lifecycle.serve(engine, server, engine.closers)

    timer = asyncio.run(main())
    if timer:
        timer.cancel()


def cmd_record(args) -> None:
    cfg = load_config(args.config)
    engine, bus = build(cfg, "sim", 0, args.seed, args.day)
    engine.inline = True
    with open(args.out, "w") as f:
        bus.recorder = f
        bus.record_filter = {"bar", "cross", "signal", "order", "fill", "position", "trade_closed", "risk", "skip",
                             "crew", "directive", "agent", "log", "session", "l2", "conviction", "proposal",
                             "books", "book_order", "book_position", "book_closed", "book_skip"}

        async def main():
            await engine.run()
            import json
            f.write(json.dumps({"type": "final", "ts": engine.feed.now(), "snapshot": {
                "config": engine.snapshot()["config"], "day": str(engine.day)}}, default=str) + "\n")

        asyncio.run(main())
    print(f"recorded -> {args.out}  day P&L {engine.risk.st.day_pnl:+.2f}, trades {engine.risk.st.trades}")


def cmd_rh_inspect(args) -> None:
    from .brokers.robinhood import inspect_main
    asyncio.run(inspect_main(load_config(args.config), args.out))


def cmd_l2_report(args) -> None:
    import json
    from .journal import Journal
    cfg = load_config(args.config)
    rows = [r for r in Journal(expand(cfg["journal_path"])).trades(limit=5000) if r.get("l2") and r["l2"] != "null"]
    if not rows:
        print("No trades with Level 2 snapshots yet. Run paper/shadow/live during market hours first.")
        return
    def show(label, xs):
        if xs:
            w = sum(1 for x in xs if x["pnl"] > 0)
            print(f"  {label:34s} n={len(xs):4d}  win {100 * w / len(xs):5.1f}%  avg ${sum(x['pnl'] for x in xs) / len(xs):+7.2f}")
    for r in rows:
        r["_l2"] = json.loads(r["l2"])
    print(f"{len(rows)} trades with a book snapshot at entry" + ("  (under 50: treat as anecdote)" if len(rows) < 50 else ""))
    show("book would have blocked", [r for r in rows if r["_l2"].get("would_block")])
    show("book ok", [r for r in rows if not r["_l2"].get("would_block")])
    for lo, hi in ((-1, -0.25), (-0.25, 0), (0, 0.25), (0.25, 1.01)):
        show(f"imbalance {lo:+.2f}..{hi:+.2f}", [r for r in rows if lo <= r["_l2"].get("imb", 0) < hi])
    show("ask wall within $0.30", [r for r in rows if r["_l2"].get("ask_wall")])


def cmd_record_quotes(args) -> None:
    from . import recorder
    cfg = load_config(args.config)
    if args.status:
        print(recorder.status(cfg))
        return
    if args.report:
        print(recorder.rate_report(expand(cfg["journal_path"]), days=args.days, tag=args.tag))
        return
    if args.probe:
        print(asyncio.run(recorder.probe(cfg, rates=tuple(int(x) for x in args.rates.split(",")))))
        return
    from logging.handlers import RotatingFileHandler
    recorder.RECORDER_DIR.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(recorder.RECORDER_DIR / "recorder.log", maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)
    for noisy in ("httpx", "httpcore", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(recorder.RecorderDaemon(cfg).run(once=args.once))


def cmd_f_report(args) -> None:
    import sqlite3
    from .books.f_journal import FJournal
    from .books.f_report import build_report, format_report
    cfg = load_config(args.config)
    print(format_report(build_report(FJournal(sqlite3.connect(str(expand(cfg["journal_path"])))), args.since)))


def cmd_f_clear_stale(args) -> None:
    import sqlite3
    from .books.f_journal import FJournal
    cfg = load_config(args.config)
    fj = FJournal(sqlite3.connect(str(expand(cfg["journal_path"]))))
    rows = fj.open_rows()
    if not rows:
        print("No open F paper positions in the journal.")
        return
    for r in rows:
        print(f"  {r['session']}  {r['symbol']}  {r['qty']} @ {r['entry']:.2f}")
    n = fj.clear_stale(date.today().isoformat() if not args.all else "9999")
    print(f"Marked {n} F paper position(s) from earlier sessions as cleared (no P&L). Book F starts clean next run.")


def cmd_backtest(args) -> None:
    from .backtest import main as bt_main
    asyncio.run(bt_main(load_config(args.config), args))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="agentdesk")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--mode", choices=["sim", "paper", "shadow", "live"])
    r.add_argument("--speed", type=float, default=30.0, help="sim only: sim-seconds per real second")
    r.add_argument("--seed", type=int, default=21)
    r.add_argument("--day", default=None, help="sim only: YYYY-MM-DD")
    r.add_argument("--no-browser", action="store_true")
    r.add_argument("--clear-halt", action="store_true",
                   help="lift today's saved halt (kill switch, safety halt); day P&L, trade count and limits carry over")
    r.set_defaults(fn=cmd_run)

    rec = sub.add_parser("record-demo")
    rec.add_argument("--seed", type=int, default=21)
    rec.add_argument("--day", default="2026-09-28")
    rec.add_argument("--out", default="demo.jsonl")
    rec.set_defaults(fn=cmd_record)

    ins = sub.add_parser("rh-inspect")
    ins.add_argument("--out", default="rh_tools.json")
    ins.set_defaults(fn=cmd_rh_inspect)

    l2r = sub.add_parser("l2-report", help="win rate / avg P&L by Level 2 state at entry")
    l2r.set_defaults(fn=cmd_l2_report)

    rq = sub.add_parser("record-quotes", help="standalone 0DTE quote recorder (launchd runs this; read-only)")
    rq.add_argument("--once", action="store_true", help="sign in if needed, take one snapshot now, exit")
    rq.add_argument("--status", action="store_true", help="is it recording? today's rows and call stats")
    rq.add_argument("--report", action="store_true", help="Robinhood call rates, latency and errors")
    rq.add_argument("--days", type=int, default=5)
    rq.add_argument("--tag", default=None, help="report only recorder or probe calls")
    rq.add_argument("--probe", action="store_true", help="bounded read-only rate ramp; run outside market hours")
    rq.add_argument("--rates", default="1,2,4,8", help="probe steps, calls per second")
    rq.set_defaults(fn=cmd_record_quotes)

    fr = sub.add_parser("f-report", help="book F paper record: win rate, mean R, PF, t, splits, shadow shorts")
    fr.add_argument("--since", default=None, help="YYYY-MM-DD")
    fr.set_defaults(fn=cmd_f_report)

    fc = sub.add_parser("f-clear-stale", help="after checking the account: clear F paper positions left open by a crash")
    fc.add_argument("--all", action="store_true", help="also clear today's open rows")
    fc.set_defaults(fn=cmd_f_clear_stale)

    bt = sub.add_parser("backtest")
    bt.add_argument("--days", type=int, default=20)
    bt.add_argument("--end", default=None, help="YYYY-MM-DD, default yesterday")
    bt.add_argument("--ticks", action="store_true", help="download trades to build real 144t bars (slow, heavy)")
    bt.add_argument("--options", choices=["model", "alpaca"], default="model")
    bt.add_argument("--source", choices=["alpaca", "robinhood"], default="alpaca", help="where SPY 1m bars come from")
    bt.add_argument("--iv", type=float, default=0.16, help="model IV when --options model")
    bt.add_argument("--csv", default=None, help="use a local 1m CSV (t,o,h,l,c,v) instead of downloading")
    bt.add_argument("--sim-days", type=int, default=0, help="run on N synthetic days (plumbing check only)")
    bt.add_argument("--out", default="backtest")
    bt.set_defaults(fn=cmd_backtest)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
