"""Tests check the rules as committed in config.yaml. The paper engine writes Evan's approved standing changes to
overrides.yaml in its deploy folder, where install_paper.sh also runs these tests, so the tests skip that file.

The tests also run next to the real .env on the Mac, so no test may see a real key: every key-like variable is removed
at session start and AGENTDESK_NO_DOTENV=1 stops config.load_config() from reading .env. A test that needs a key sets
a fake one with monkeypatch."""
import os

SECRET_PREFIXES = ("ANTHROPIC_", "ALPACA_", "THETADATA", "MASSIVE")


def is_secret_name(name: str) -> bool:
    n = name.upper()
    return "_API_KEY" in n or n.startswith(SECRET_PREFIXES)


os.environ["AGENTDESK_IGNORE_OVERRIDES"] = "1"
os.environ["AGENTDESK_NO_DOTENV"] = "1"
for _k in [k for k in os.environ if is_secret_name(k)]:
    del os.environ[_k]
