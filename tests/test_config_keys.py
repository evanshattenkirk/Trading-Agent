"""Config hygiene: the test process never sees real keys (conftest strips them and turns off .env loading)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk import config


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
    monkeypatch.delenv("AGENTDESK_TEST_ONLY_VAR", raising=False)
    monkeypatch.setenv("AGENTDESK_NO_DOTENV", "1")
    config._load_dotenv(env)
    assert "AGENTDESK_TEST_ONLY_VAR" not in os.environ
    monkeypatch.delenv("AGENTDESK_NO_DOTENV")
    config._load_dotenv(env)                                       # the engine and research scripts still load it
    assert os.environ["AGENTDESK_TEST_ONLY_VAR"] == "from-dotenv"
