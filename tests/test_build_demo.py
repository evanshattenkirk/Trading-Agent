"""The single-file demo page (tools/build_demo.py) inlines every script the dashboard needs (M16: panel.js was
missing, so the built demo froze on its first risk event)."""
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
T0 = 1791293400.0          # 2026-10-06 08:30 CT


def build(tmp_path, **kw) -> str:
    spec = importlib.util.spec_from_file_location("build_demo", ROOT / "tools" / "build_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    events = [{"type": "log", "ts": T0 + 60 * i, "level": "info", "msg": f"event {i}"} for i in range(9)]
    events.append({"type": "risk", "ts": T0 + 600, "risk": {"day_pnl": 0, "wins": 0, "losses": 0, "max_daily_loss": 400}})
    src = tmp_path / "events.jsonl"
    src.write_text("".join(json.dumps(e) + "\n" for e in events))
    out = tmp_path / "demo.html"
    mod.main(str(src), str(out), "08:30", **kw)
    return out.read_text()


def scripts(page: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", page, flags=re.S)


@pytest.mark.parametrize("artifact", [True, False])
def test_demo_inlines_panel_before_app_and_loads_nothing_local(tmp_path, artifact):
    page = build(tmp_path, artifact=artifact)
    assert "root.AgentPanel = api" in page                                   # panel.js is in the page
    assert page.index("root.AgentPanel = api") < page.index("const P = window.AgentPanel")   # and runs before app.js
    assert not re.search(r"<script[^>]*\bsrc=", page)                       # every script inlined
    assert "source: 'replay'" in page and '"msg":"event 0"' in page


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_demo_scripts_parse(tmp_path):
    page = build(tmp_path)
    blocks = scripts(page)
    assert len(blocks) >= 4
    for i, js in enumerate(blocks):
        f = tmp_path / f"block{i}.js"
        f.write_text(js)
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"script {i}: {r.stderr[:500]}"
