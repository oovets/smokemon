"""The package __version__ and pyproject's [project].version must agree, so a release cannot
ship mismatched numbers.

There used to be a third source, the latest CHANGELOG entry. CHANGELOG.md has since been
removed from the repository, so that check is gone rather than left asserting against a file
that is deliberately absent."""

import re
from pathlib import Path

import smokemon

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    # regex (not tomllib) so the test runs on the project's min Python 3.10, pre-stdlib-tomllib
    m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text())
    assert m, "no version in pyproject.toml"
    return m.group(1)


def test_package_and_pyproject_versions_match():
    assert smokemon.__version__ == _pyproject_version()
