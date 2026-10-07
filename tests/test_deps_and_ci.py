"""requirements.lock pins every requirement, CI installs it on the Mac's Python and runs both suites, and the pytest
config registers its markers (static checks)."""
import re
import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent


def pins() -> dict:
    out = {}
    for line in (ROOT / "requirements.lock").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, ver = line.split(";", 1)[0].strip().split("==")
            out[re.sub(r"[-_.]+", "-", name).lower()] = ver
    return out


def requirements() -> list:
    reqs = []
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            reqs.append(Requirement(line))
    return reqs


def test_lock_pins_every_requirement_within_its_range():
    locked = pins()
    for r in requirements():
        name = re.sub(r"[-_.]+", "-", r.name).lower()
        assert name in locked, f"{r.name} is in requirements.txt but not in requirements.lock (re-run uv pip compile)"
        assert r.specifier.contains(locked[name], prereleases=True), f"{r.name}=={locked[name]} is outside {r.specifier}"


def test_lock_was_compiled_for_python_312():
    head = (ROOT / "requirements.lock").read_text().splitlines()[:3]
    assert any("uv pip compile requirements.txt -o requirements.lock --python-version 3.12" in h for h in head)


def test_ci_installs_the_lock_on_312_and_runs_both_suites():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "tests.yml").read_text())
    steps = wf["jobs"]["pytest"]["steps"]
    py = [s for s in steps if "setup-python" in s.get("uses", "")][0]["with"]
    assert str(py["python-version"]) == "3.12"
    runs = [s["run"] for s in steps if "run" in s]
    assert any("-r requirements.lock" in r for r in runs) and not any("-r requirements.txt" in r for r in runs)
    assert any(re.search(r"pytest .*\btests research/tests\b", r) for r in runs)


def test_pytest_config_runs_both_suites_with_strict_markers():
    ini = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert ini["testpaths"] == ["tests", "research/tests"]
    assert "--strict-markers" in ini["addopts"] and "-p no:cacheprovider" in ini["addopts"]
    assert not re.search(r"(^|\s)-q\b", ini["addopts"])         # `pytest -q tests` must still print "N passed"
    assert any(m.startswith("slow:") for m in ini["markers"])


def test_synthetic_day_files_are_marked_slow():
    for f in ("test_books_sim.py", "test_f_sim.py"):
        assert "pytestmark = pytest.mark.slow" in (ROOT / "tests" / f).read_text(), f
