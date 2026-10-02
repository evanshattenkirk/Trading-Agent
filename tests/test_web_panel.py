"""Runs the dashboard's pure JS helpers (agentdesk/web/panel.js) under node; skipped where node isn't installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_panel_helpers():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "web" / "panel.test.js")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
