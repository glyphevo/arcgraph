"""Keep the declared Python support equal to the tested Python support.

`requires-python` is a claim pip enforces at install time, and it was
`>=3.11` while CI ran 3.11 and 3.12 only. That form admits every later
interpreter on the strength of nothing: a 3.13 user is told the package
supports them by a declaration no lane ever exercised. The claim and the
evidence are now one fact, and these tests are what keeps them one: widening
the declaration without widening the matrix fails here, and so does widening
the matrix without widening the declaration.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
REQUIRES_PYTHON = re.compile(r"^>=(\d+)\.(\d+),<(\d+)\.(\d+)$")


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _workflow_jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _tested_versions() -> set[str]:
    """Versions the test matrix actually runs the suite on."""

    matrix = _workflow_jobs()["test-matrix"]["strategy"]["matrix"]
    return {str(version) for version in matrix["python"]}


def _packaged_versions() -> set[str]:
    matrix = _workflow_jobs()["package-matrix"]["strategy"]["matrix"]
    return {str(entry["python"]) for entry in matrix["include"]}


def test_requires_python_is_bounded_at_both_ends() -> None:
    """An open upper bound claims interpreters no lane has ever run."""

    requires = _pyproject()["project"]["requires-python"]
    assert REQUIRES_PYTHON.match(requires), (
        f"requires-python is {requires!r}; it must name a lower and an upper "
        "bound so the declaration cannot outrun the CI matrix"
    )


def test_requires_python_admits_exactly_the_tested_versions() -> None:
    requires = _pyproject()["project"]["requires-python"]
    match = REQUIRES_PYTHON.match(requires)
    assert match is not None
    low_major, low_minor, high_major, high_minor = (int(g) for g in match.groups())

    tested = sorted(
        _tested_versions(), key=lambda v: tuple(int(p) for p in v.split("."))
    )
    lowest = tuple(int(part) for part in tested[0].split("."))
    highest = tuple(int(part) for part in tested[-1].split("."))

    assert (low_major, low_minor) == lowest, (
        f"requires-python starts at {low_major}.{low_minor} but the lowest "
        f"tested version is {tested[0]}"
    )
    # The exclusive upper bound is the release after the highest tested one.
    assert (high_major, high_minor) == (highest[0], highest[1] + 1), (
        f"requires-python excludes {high_major}.{high_minor} but the highest "
        f"tested version is {tested[-1]}; the bound must be "
        f"{highest[0]}.{highest[1] + 1}"
    )


def test_classifiers_name_exactly_the_tested_versions() -> None:
    classifiers = _pyproject()["project"]["classifiers"]
    declared = {
        line.rsplit(" :: ", 1)[-1]
        for line in classifiers
        if line.startswith("Programming Language :: Python :: ")
        and "." in line.rsplit(" :: ", 1)[-1]
    }
    assert declared == _tested_versions()


def test_every_packaged_version_is_also_a_tested_version() -> None:
    """A wheel is never smoke-tested on an interpreter the suite skips."""

    assert _packaged_versions() <= _tested_versions()


def test_narrowed_python_range_is_historical_and_points_to_current_support() -> None:
    from arcgraph.interfaces.docs import render_docs

    sections = render_docs("migration-notes", as_json=True)["sections"]
    by_title = {section["title"]: section for section in sections}
    old = " ".join(by_title["Supported Python Versions Narrowed"]["items"])
    current = " ".join(by_title["Supported Python Versions Widened"]["items"])
    assert "historical restriction" in old
    assert "Supported Python Versions Widened" in old
    assert "is now" not in old and "now fails" not in old
    assert _pyproject()["project"]["requires-python"] in current
