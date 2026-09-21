"""Tests for [tool.arcgraph] exclude config integration."""

from __future__ import annotations

import json
from pathlib import Path

from arcgraph.core.scanner import (
    detect_source_roots,
    read_arcgraph_exclude,
)
from arcgraph.interfaces.cli import main


def _write_pyproject(root: Path, exclude: list[str] | None = None) -> None:
    lines = [
        '[project]\nname = "sample"\nversion = "0.1.0"\n',
        "\n[tool.arcgraph]\n",
        'source_roots = ["src"]\n',
    ]
    if exclude:
        patterns = ", ".join(f'"{e}"' for e in exclude)
        lines.append(f"exclude = [{patterns}]\n")
    (root / "pyproject.toml").write_text("".join(lines), encoding="utf-8")


def _sample_tree(root: Path) -> None:
    """Create src/pkg with app.py and a legacy_app subtree."""
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "legacy_app").mkdir(parents=True)
    (root / "src" / "legacy_app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "legacy_app" / "old.py").write_text("y = 2\n", encoding="utf-8")


def test_read_arcgraph_exclude_returns_patterns(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, exclude=["**/generated/**", "legacy/**"])
    result = read_arcgraph_exclude(tmp_path)
    assert result == ("**/generated/**", "legacy/**")


def test_read_arcgraph_exclude_empty_when_missing(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, exclude=None)
    result = read_arcgraph_exclude(tmp_path)
    assert result == ()


def test_read_arcgraph_exclude_no_pyproject(tmp_path: Path) -> None:
    result = read_arcgraph_exclude(tmp_path)
    assert result == ()


def test_detect_source_roots_carries_exclude(tmp_path: Path) -> None:
    _sample_tree(tmp_path)
    _write_pyproject(tmp_path, exclude=["src/legacy_app/**"])
    resolved = detect_source_roots(tmp_path)
    assert resolved.detection.exclude == ("src/legacy_app/**",)


def test_build_with_exclude_omits_files(tmp_path: Path, capsys) -> None:
    """arcgraph build with exclude should skip matched paths."""
    _sample_tree(tmp_path)
    _write_pyproject(tmp_path, exclude=["src/legacy_app/**"])

    exit_code = main(
        [
            "--repo-root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "output" / "arcgraph"),
            "build",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    # legacy_app/old.py should be excluded — only pkg/__init__.py and pkg/app.py
    assert payload["file_count"] == 2


def test_build_without_exclude_includes_all(tmp_path: Path, capsys) -> None:
    """Without exclude, all files should be scanned."""
    _sample_tree(tmp_path)
    _write_pyproject(tmp_path, exclude=None)

    exit_code = main(
        [
            "--repo-root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "output" / "arcgraph"),
            "build",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    # All 4 .py files: pkg/__init__.py, pkg/app.py, legacy_app/__init__.py, legacy_app/old.py
    assert payload["file_count"] == 4
