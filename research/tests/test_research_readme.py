"""research/README.md names every research script, and the reproducibility notes stay in place."""
import re
from pathlib import Path

RESEARCH = Path(__file__).resolve().parents[1]
README = (RESEARCH / "README.md").read_text()


def test_every_research_script_is_in_the_readme():
    scripts = sorted(p.name for p in RESEARCH.glob("*.py")) + ["fetch_data.sh"]
    missing = [s for s in scripts if s not in README]
    assert not missing, f"add a rerun command and output for: {missing}"


def test_readme_says_committed_results_predate_the_data_fixes():
    assert "predate the 2026-10-07 data fixes" in README


def test_fetch_data_pins_every_github_source_to_a_commit():
    text = (RESEARCH / "fetch_data.sh").read_text()
    urls = re.findall(r"https://raw\.githubusercontent\.com/\S+", text)
    assert urls
    for url in urls:
        ref = url.split("/")[5]                       # owner/repo/<ref>/...
        assert ref.startswith("$") and ref.endswith("_SHA"), url
    shas = re.findall(r"^(\w+_SHA)=([0-9a-f]+)\s", text, re.M)
    assert {name for name, _ in shas} == {u.split("/")[5][1:] for u in urls}
    assert all(len(sha) == 40 for _, sha in shas)


def test_d_quiet_check_writes_json():
    text = (RESEARCH / "d_quiet_check.py").read_text()
    assert "d_quiet_check_results.json" in text and "json.dumps" in text
