"""The three version sources must agree so a release can't ship mismatched numbers:
the package __version__, pyproject's [project].version, and the latest CHANGELOG entry."""

import re
from pathlib import Path

import smokemon

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    # regex (not tomllib) so the test runs on the project's min Python 3.10, pre-stdlib-tomllib
    m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text())
    assert m, "no version in pyproject.toml"
    return m.group(1)


def _changelog_latest() -> str:
    m = re.search(r'(?m)^==\s*(\d+\.\d+\.\d+)\b', (ROOT / "CHANGELOG.md").read_text())
    assert m, "no dated version entry in CHANGELOG.md"
    return m.group(1)


def test_package_and_pyproject_versions_match():
    assert smokemon.__version__ == _pyproject_version()


def test_changelog_leads_with_current_version():
    assert _changelog_latest() == smokemon.__version__
