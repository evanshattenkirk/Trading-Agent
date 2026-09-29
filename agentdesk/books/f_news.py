"""Book F news tag (docs/BOOK_F_HANDOFF.md section 4.5). Observe-only: it never gates, sizes or delays an order.

At the scan the Tape and Macro desks get the 5 picks plus up to 5 runners-up. Online (ANTHROPIC_API_KEY, live data)
one Claude call with web search returns, per symbol:
  {symbol, catalyst: earnings|guidance|analyst|m&a|sector_macro|none_found, priced_in: early|partly|fully,
   confidence: 0..1, note: <= 20 words, sources: [urls]}
Offline: catalyst unknown, priced_in unknown. A tag later than 60 s is stored and logged late.
The crew may *propose* promoting the tag to a filter only as a new strategy (waits for Evan); proposals.TWEAKS has
no books.* keys, so F's size and loss fields can't be touched.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

log = logging.getLogger("agentdesk.f_news")
CATALYSTS = {"earnings", "guidance", "analyst", "m&a", "sector_macro", "none_found"}
PRICED = {"early", "partly", "fully"}
OFFLINE = {"catalyst": "unknown", "priced_in": "unknown", "confidence": 0.0, "note": "crew offline", "sources": []}


def clean_tag(t: dict) -> dict:
    cat = str(t.get("catalyst", "")).lower()
    pi = str(t.get("priced_in", "")).lower()
    try:
        conf = max(0.0, min(1.0, float(t.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    note = " ".join(str(t.get("note", "")).split()[:20])
    src = [str(u) for u in (t.get("sources") or []) if str(u).startswith("http")][:5]
    return {"catalyst": cat if cat in CATALYSTS else "unknown", "priced_in": pi if pi in PRICED else "unknown",
            "confidence": round(conf, 2), "note": note, "sources": src}


class NewsTagger:
    def __init__(self, engine, cfg, timeout: float = 60.0):
        self.e, self.cfg, self.timeout = engine, cfg, timeout
        self.task: asyncio.Task | None = None

    @property
    def online(self) -> bool:
        crew = self.e.crew
        return bool(crew is not None and not crew.offline and self.cfg["books"]["F_stocks_in_play"].get("news_tag") == "observe")

    def start(self, now: float, picks: list, runners: list, done) -> None:
        syms = [r.symbol for r in picks + runners]
        if not syms:
            return
        if not self.online:
            done({s: dict(OFFLINE) for s in syms}, False)
            return
        self.task = asyncio.ensure_future(self._run(now, picks, runners, done))

    async def _run(self, now: float, picks, runners, done) -> None:
        t0 = time.time()
        try:
            tags = await self._ask(now, picks, runners)
        except Exception as ex:
            log.warning("news tag failed: %s", ex)
            self.e.bus.emit("log", self.e.feed.now(), level="warn", msg=f"book F news tag failed: {ex}")
            tags = {}
        syms = [r.symbol for r in picks + runners]
        out = {s: clean_tag(tags.get(s, {"catalyst": "unknown", "priced_in": "unknown"})) for s in syms}
        late = time.time() - t0 > self.timeout
        done(out, late)
        found = [f"{s} {t['catalyst']}/{t['priced_in']}" for s, t in out.items() if t["catalyst"] not in ("unknown",)]
        self.e.bus.emit("crew", self.e.feed.now(), desk="macro", phase="say", who="macro",
                        text=("Catalysts: " + ", ".join(found[:5])) if found else "No clear catalysts on the F picks.")

    async def _ask(self, now: float, picks, runners) -> dict:
        from ..crew import cached_system
        crew = self.e.crew
        client = crew._get_client()
        if client is None:
            return {}
        lines = [f"{r.symbol}: RVOL5 {r.rvol5:.1f}x, first 5-min candle {r.open:.2f} -> {r.close:.2f}"
                 + (" (pick)" if r.picked else " (runner-up)") for r in picks + runners]
        system = ("You are the Tape and Macro desks on a small trading team. For each US stock listed, find today's "
                  "news with web search and say whether the move looks priced in. Never invent news; if you find none, "
                  "say none_found. Reply with JSON only: {\"tags\": [{\"symbol\": \"...\", \"catalyst\": "
                  "\"earnings|guidance|analyst|m&a|sector_macro|none_found\", \"priced_in\": \"early|partly|fully\", "
                  "\"confidence\": 0.0, \"note\": \"<= 20 words\", \"sources\": [\"url\"]}]}")
        msg = await asyncio.wait_for(client.messages.create(
            model=crew.cfg["model"], max_tokens=2000, system=cached_system(system),
            messages=[{"role": "user", "content": "Stocks in play at 09:35 ET today:\n" + "\n".join(lines)}],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}]), timeout=self.timeout * 2)
        crew.note_usage(msg, "f_news", crew.cfg["model"])
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        from ..crew import _extract_json
        obj = _extract_json(text, key="tags") or {}
        return {str(t.get("symbol", "")).upper(): t for t in obj.get("tags", []) if isinstance(t, dict)}
