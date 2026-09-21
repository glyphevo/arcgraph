"""Tests for ArcGraph package metadata exposed at import time."""

from __future__ import annotations

from pathlib import Path

import tomllib

import arcgraph


def test_package_version_matches_pyproject() -> None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert arcgraph.__version__ == pyproject["project"]["version"]
