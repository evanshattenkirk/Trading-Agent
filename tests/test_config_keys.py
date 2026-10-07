"""Config hygiene: every config.yaml knob is read by the code, and the test process never sees real keys
(conftest strips them and turns off .env loading)."""
import functools
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import config

ROOT = Path(__file__).resolve().parent.parent


def _tests_conftest():
    """tests/conftest.py, loaded by path (another conftest.py could shadow the module name `conftest`)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("agentdesk_tests_conftest", Path(__file__).with_name("conftest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


is_secret_name = _tests_conftest().is_secret_name


# ------------------------------------------------------------------ no real keys in the test process


def test_the_test_session_has_no_key_variables():
    assert os.environ.get("AGENTDESK_NO_DOTENV") == "1"
    assert [k for k in os.environ if is_secret_name(k)] == []


def test_secret_name_patterns():
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY",
              "MASSIVE_API_KEY", "POLYGON_API_KEY", "THETADATA_USERNAME", "THETADATA_PASSWORD"):
        assert is_secret_name(k), k
    for k in ("PATH", "HOME", "AGENTDESK_IGNORE_OVERRIDES", "AGENTDESK_NO_DOTENV"):
        assert not is_secret_name(k), k


def test_load_dotenv_is_off_when_agentdesk_no_dotenv_is_set(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AGENTDESK_TEST_ONLY_VAR=from-dotenv\n")
    monkeypatch.setenv("AGENTDESK_TEST_ONLY_VAR", "x")               # records "absent", so teardown removes it
    monkeypatch.delenv("AGENTDESK_TEST_ONLY_VAR")
    monkeypatch.setenv("AGENTDESK_NO_DOTENV", "1")
    config._load_dotenv(env)
    assert "AGENTDESK_TEST_ONLY_VAR" not in os.environ
    monkeypatch.delenv("AGENTDESK_NO_DOTENV")
    config._load_dotenv(env)                                         # the engine and research scripts still load it
    assert os.environ["AGENTDESK_TEST_ONLY_VAR"] == "from-dotenv"


# ------------------------------------------------------------------ every config knob is read somewhere
# A misspelt key in config.yaml (stop_debit_x_credt) silently falls back to the code's .get() default. Every leaf key
# below must appear in the code as a string, an attribute or a keyword (cfg["k"], .get("k"), cfg.k, k=, k:).
SECTIONS = ("exits", "risk", "strategy", "crew", "robinhood", "data", "sizing", "orders", "l2", "strikes", "recorder",
            "review", "server", "calendar")                  # plus every book under books.*
CODE_DIRS = ("agentdesk", "reporting", "tools")
DATA_MAPS = {"books.E_earnings_iv.sectors"}                  # keys are data (GICS sector names), not knobs
UNUSED_OK = {                                                # in config.yaml but read by nothing; one line why
    "robinhood.arg_overrides": "escape hatch noted for rh-inspect; the schema matcher in brokers/robinhood.py never reads it",
    "orders.reprice_after_ms": "the engine reprices at once against the 0.5 s quote cache; slated for removal",
    "orders.exit_urgent": "stops and flattens always go out at the bid; there is no other exit mode",
    "books.E_earnings_iv.never_hold_through_announcement": "states the rule; book E always exits before the report",
    "books.F1_stocks_in_play.first_candle": "F1 implements only a green first candle (f_stocks_in_play.py)",
    "books.F1_stocks_in_play.instrument": "F1 trades only whole shares",
}


def config_leaves(node, path):
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}"
            if isinstance(v, dict) and v and p not in DATA_MAPS:
                yield from config_leaves(v, p)
            else:
                yield p, str(k)
                if isinstance(v, list):                      # lists of dicts: scale_outs, the strike schedule
                    for x in v:
                        if isinstance(x, dict):
                            yield from config_leaves(x, p + "[]")


def all_leaves() -> dict:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    out = []
    for s in SECTIONS:
        out += config_leaves(cfg[s], s)
    out += config_leaves(cfg["books"], "books")
    return dict(out)                                         # path -> key


@functools.lru_cache(maxsize=1)
def code_names() -> frozenset:
    """Every name the code uses as a string ('k'), an attribute (.k) or a keyword/field (k= / k:)."""
    names = set()
    for d in CODE_DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if p.suffix in (".py", ".js", ".sh", ".html") and "vendor" not in p.parts:
                t = p.read_text(errors="replace")
                names.update(re.findall(r"""["']([A-Za-z_]\w*)["']""", t))
                names.update(re.findall(r"\.([A-Za-z_]\w*)", t))
                names.update(re.findall(r"\b([A-Za-z_]\w*)\s*[=:]", t))
    return frozenset(names)


def referenced(key: str) -> bool:
    return key in code_names()


def test_every_config_leaf_key_is_read_by_the_code():
    missing = [p for p, k in all_leaves().items() if p not in UNUSED_OK and not referenced(k)]
    assert missing == [], f"config keys no code reads (a typo, or add to UNUSED_OK with a reason): {missing}"


def test_unused_allowlist_is_current():
    leaves = all_leaves()
    now_read = [p for p in UNUSED_OK if p in leaves and referenced(leaves[p])]
    assert now_read == [], f"these keys are read now; take them off UNUSED_OK: {now_read}"
    assert all(UNUSED_OK.values())


def test_a_misspelt_key_is_caught():
    assert referenced("stop_debit_x_credit") and not referenced("stop_debit_x_credt")
    assert "books.B_iron_fly.stop_debit_x_credit" in all_leaves()
