"""Config loading. YAML file -> nested attribute dicts, plus .env secrets."""
from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
TZ_CT = "America/Chicago"


class Cfg(dict):
    """dict with attribute access, recursively."""

    def __getattr__(self, k: str) -> Any:
        try:
            v = self[k]
        except KeyError as e:
            raise AttributeError(k) from e
        return Cfg(v) if isinstance(v, dict) and not isinstance(v, Cfg) else v

    def __setattr__(self, k: str, v: Any) -> None:
        self[k] = v


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | Path | None = None) -> Cfg:
    _load_dotenv(ROOT / ".env")
    p = Path(path) if path else ROOT / "config.yaml"
    data = yaml.safe_load(p.read_text())
    from .proposals import apply_overrides
    apply_overrides(data)                      # standing changes you approved in the dashboard
    return Cfg(data)


def hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def expand(p: str) -> Path:
    path = Path(os.path.expanduser(p))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
