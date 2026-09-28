"""Strategy proposals from the crew.

Scopes
  trade          tweak for the next entry only        auto-applied if inside bounds (crew.auto_apply_tweaks)
  day            tweak for the rest of today           auto-applied if inside bounds, reverted at the next session
  standing       permanent parameter change            waits for your Approve in the dashboard -> written to overrides.yaml
  new_strategy   a different strategy (written spec)   waits for your Approve -> queued for build + backtest; never runs by itself

Tweaks may only touch the whitelisted keys below, inside the listed bounds. Anything else is rejected.
"""
from __future__ import annotations

import copy
import itertools
import json
import time
from pathlib import Path

import yaml

from .config import ROOT, hhmm

TWEAKS = {
    "exits.stop_loss_pct": ("float", 0.10, 0.25),
    "exits.swing.runner_trail_pct": ("float", 0.10, 0.35),
    "exits.scalp.runner_trail_pct": ("float", 0.08, 0.25),
    "exits.swing.scale_outs.0.at": ("float", 0.15, 0.50),
    "exits.scalp.scale_outs.0.at": ("float", 0.10, 0.30),
    "exits.swing.time_stop_min": ("int", 5, 30),
    "exits.scalp.time_stop_min": ("int", 3, 10),
    "strategy.rsi.upper": ("float", 60, 75),
    "strategy.entry_window.end": ("hhmm_earlier", None, None),
    "risk.max_trades_per_day": ("int_lower", 1, None),
    "strategy.enabled_setups": ("subset", ["SWING", "SCALP"], None),
    "strikes.max_offset": ("int", -1, 2),
}
TRADE_SCOPE_KEYS = {k for k in TWEAKS if k.startswith("exits.")} | {"strikes.max_offset"}


def get_path(d, key: str):
    for part in key.split("."):
        d = d[int(part)] if isinstance(d, list) else d.get(part) if isinstance(d, dict) else None
        if d is None:
            return None
    return d


def set_path(d, key: str, value) -> None:
    parts = key.split(".")
    for part in parts[:-1]:
        d = d[int(part)] if isinstance(d, list) else d.setdefault(part, {})
    last = parts[-1]
    if isinstance(d, list):
        d[int(last)] = value
    else:
        d[last] = value


def validate(cfg, key: str, value) -> tuple[bool, str, object]:
    spec = TWEAKS.get(key)
    if spec is None:
        return False, f"{key} is not a tweakable parameter", None
    kind, lo, hi = spec
    cur = get_path(cfg, key)
    try:
        if kind == "float":
            v = float(value)
            return (lo <= v <= hi), f"{key} must be in [{lo}, {hi}]", v
        if kind == "int":
            v = int(value)
            return (lo <= v <= hi), f"{key} must be in [{lo}, {hi}]", v
        if kind == "int_lower":
            v = int(value)
            return (lo <= v <= int(cur or 99)), f"{key} can only be lowered", v
        if kind == "hhmm_earlier":
            v = str(value)
            return (hhmm(v) <= hhmm(str(cur))), f"{key} can only move earlier", v
        if kind == "subset":
            v = [x for x in value if x in lo]
            return (1 <= len(v) <= len(lo) and len(v) == len(value)), f"{key} must be a non-empty subset of {lo}", v
    except (TypeError, ValueError):
        pass
    return False, f"bad value for {key}", None


class ProposalBook:
    def __init__(self, cfg, path: Path | None):
        self.cfg = cfg
        self.path = path
        self.items: list[dict] = []
        self._ids = itertools.count(1)
        if path and path.exists():
            try:
                self.items = json.loads(path.read_text())
                self._ids = itertools.count(max([int(i["id"]) for i in self.items] or [0]) + 1)
            except Exception:
                self.items = []

    def save(self) -> None:
        if self.path:
            self.path.write_text(json.dumps(self.items, indent=1, default=str))

    def submit(self, desk: str, p: dict, now: float) -> dict:
        scope = p.get("scope", "day")
        item = {"id": str(next(self._ids)), "ts": now, "desk": desk, "scope": scope, "title": p.get("title", "")[:120],
                "rationale": p.get("rationale", "")[:600], "params": {}, "spec": p.get("spec", "")[:3000],
                "evidence": p.get("evidence", "")[:600], "status": "pending"}
        if scope in ("trade", "day") and any(i["title"] == item["title"] and i["status"] == "applied" and now - i["ts"] < 12 * 3600
                                             for i in self.items):
            return {}
        if scope == "new_strategy":
            if any(i["title"] == item["title"] and i["scope"] == "new_strategy" for i in self.items):
                return {}                                              # one pitch per idea
        else:
            params, errors = {}, []
            for k, v in (p.get("params") or {}).items():
                if scope == "trade" and k not in TRADE_SCOPE_KEYS:
                    errors.append(f"{k} not allowed per-trade")
                    continue
                ok, why, val = validate(self.cfg, k, v)
                if ok:
                    params[k] = val
                else:
                    errors.append(why)
            item["params"] = params
            if errors or not params:
                item["status"] = "rejected: " + ("; ".join(errors) if errors else "no parameters")
            elif scope in ("trade", "day") and self.cfg["crew"].get("auto_apply_tweaks", True):
                item["status"] = "applied"
        self.items.append(item)
        self.items = self.items[-200:]
        self.save()
        return item

    def set_status(self, pid: str, status: str) -> dict | None:
        for i in self.items:
            if i["id"] == pid:
                i["status"] = status
                i["decided_ts"] = time.time()
                self.save()
                return i
        return None

    def pending(self) -> list[dict]:
        return [i for i in self.items if i["status"] == "pending"]


def write_override(params: dict, path: Path | None = None) -> Path:
    path = path or ROOT / "overrides.yaml"
    cur = yaml.safe_load(path.read_text()) if path.exists() else {}
    cur = cur or {}
    for k, v in params.items():
        set_path(cur, k, v) if "scale_outs" not in k else _set_list_override(cur, k, v)
    path.write_text(yaml.safe_dump(cur, sort_keys=False))
    return path


def _set_list_override(cur: dict, key: str, value) -> None:
    # overrides.yaml stores list-element tweaks as dotted keys to avoid clobbering whole lists
    cur.setdefault("_dotted", {})[key] = value


def apply_overrides(cfg, path: Path | None = None) -> None:
    path = path or ROOT / "overrides.yaml"
    if not path.exists():
        return
    ov = yaml.safe_load(path.read_text()) or {}
    for k, v in (ov.pop("_dotted", {}) or {}).items():
        set_path(cfg, k, v)
    _deep_merge(cfg, ov)


def _deep_merge(dst, src) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
