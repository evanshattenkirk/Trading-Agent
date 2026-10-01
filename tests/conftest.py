"""Tests check the rules as committed in config.yaml. The paper engine writes Evan's approved standing changes to
overrides.yaml in its deploy folder, where install_paper.sh also runs these tests, so the tests skip that file."""
import os

os.environ["AGENTDESK_IGNORE_OVERRIDES"] = "1"
