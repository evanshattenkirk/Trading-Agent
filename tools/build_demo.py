"""Build the single-file demo page: dashboard + recorded sim session, everything inlined."""
import json, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "agentdesk" / "web"


def main(jsonl: str, out: str, start_hhmm: str = "08:58", artifact: bool = True):
    events = [json.loads(l) for l in open(jsonl)]
    from datetime import datetime, time
    from zoneinfo import ZoneInfo
    d = datetime.fromtimestamp(events[0]["ts"], ZoneInfo("America/Chicago")).date()
    h, m = map(int, start_hhmm.split(":"))
    start_ts = datetime.combine(d, time(h, m), ZoneInfo("America/Chicago")).timestamp()
    html = (WEB / "index.html").read_text()
    body = html.split("<!--BODY-->")[1].split("<!--/BODY-->")[0]
    body = re.sub(r'<script src="[^"]+"></script>\s*', "", body)
    css = (WEB / "styles.css").read_text()
    lib = (WEB / "vendor" / "lightweight-charts.standalone.production.js").read_text()
    js = (WEB / "office.js").read_text() + "\n" + (WEB / "app.js").read_text()
    data = json.dumps(events, separators=(",", ":"))
    head = ('<title>AgentDesk</title>\n'
            '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&family=Silkscreen&display=swap">\n'
            f"<style>\n{css}\n</style>\n")
    scripts = (f"<script>window.AGENTDESK = {{ source: 'replay', startTs: {start_ts} }};</script>\n"
               f"<script>window.AGENTDESK_DEMO = {data};</script>\n"
               f"<script>{lib}</script>\n<script>{js}</script>\n")
    page = head + body + scripts
    if not artifact:
        page = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                + head + "</head><body>" + body + scripts + "</body></html>")
    Path(out).write_text(page)
    print(f"{out}: {len(page) / 1e6:.2f} MB, {len(events)} events, starts {start_hhmm} CT")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "08:58", "--standalone" not in sys.argv)
