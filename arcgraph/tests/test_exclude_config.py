"""Tests for [tool.arcgraph] exclude config integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import (
    FileScanner,
    SourceRoot,
    detect_source_roots,
    read_arcgraph_exclude,
)
from arcgraph.interfaces.cli import main
from arcgraph.pipeline.indexer import ArcGraphIndexer


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


def _command(root: Path, output: Path, capsys, *args: str) -> dict:
    assert main(["--repo-root", str(root), "--output-dir", str(output), *args]) == 0
    return json.loads(capsys.readouterr().out)


def _graph(output: Path) -> dict:
    """Compare graph/fact contents, excluding generation times and lifecycle."""
    store = GraphStoreReader.from_current(output)
    graph = {
        kind: sorted(
            (row.model_dump(mode="json") for row in rows),
            key=lambda row: json.dumps(row, sort_keys=True),
        )
        for kind, rows in (
            ("files", store.read_files()),
            ("nodes", store.read_nodes()),
            ("edges", store.read_edges()),
            ("warnings", store.read_warnings()),
        )
    }
    facts = [
        json.loads(line)
        for line in (store.sqlite_path.parent / "semantic_facts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    for fact in facts:
        fact.pop("index_version", None)
        fact.pop("created_at", None)
    graph["facts"] = sorted(facts, key=lambda row: json.dumps(row, sort_keys=True))
    diagnostics = [row.model_dump(mode="json") for row in store.read_diagnostics()]
    for diagnostic in diagnostics:
        for field in (
            "diagnostic_id",
            "index_version",
            "first_seen_index",
            "last_seen_index",
            "seen_count",
        ):
            diagnostic.pop(field, None)
        diagnostic["properties"].pop("diagnostic_lifecycle_status", None)
    graph["diagnostics"] = sorted(
        diagnostics, key=lambda row: json.dumps(row, sort_keys=True)
    )
    return graph


@pytest.mark.parametrize("mutation", ["untouched", "modified", "deleted"])
def test_excluded_files_stay_out_of_freshness_and_sync(
    tmp_path: Path, capsys, mutation: str
) -> None:
    _sample_tree(tmp_path)
    _write_pyproject(tmp_path, ["src/legacy_app/**"])
    output = tmp_path / "output/arcgraph"
    _command(tmp_path, output, capsys, "build")
    ignored = tmp_path / "src/legacy_app/old.py"
    if mutation == "modified":
        ignored.write_text("def ignored(): return 2\n", encoding="utf-8")
    elif mutation == "deleted":
        ignored.unlink()
    assert QueryEngine(output).current()["freshness"]["status"] == "fresh"
    assert _command(tmp_path, output, capsys, "sync")["action"] == "unchanged"

    # Force a real incremental publication, including the no-exclude consumer.
    (tmp_path / "src/pkg/app.py").write_text(
        "def visible(): return 3\n", encoding="utf-8"
    )
    result = _command(tmp_path, output, capsys, "sync", "--if-stale")
    assert result["action"] == "reindexed"
    assert result["changed"] == {
        "added": [],
        "deleted": [],
        "modified": ["src/pkg/app.py"],
    }
    complete = tmp_path / "output/complete"
    _command(tmp_path, complete, capsys, "build")
    assert _graph(output) == _graph(complete)
    assert result["freshness"]["status"] == "fresh"


@pytest.mark.parametrize(
    ("before", "after", "added", "deleted"),
    [
        (
            None,
            ["src/legacy_app/**"],
            [],
            ["src/legacy_app/__init__.py", "src/legacy_app/old.py"],
        ),
        (
            ["src/legacy_app/**"],
            None,
            ["src/legacy_app/__init__.py", "src/legacy_app/old.py"],
            [],
        ),
        (
            ["src/legacy_app/**"],
            ["src/pkg/**"],
            ["src/legacy_app/__init__.py", "src/legacy_app/old.py"],
            ["src/pkg/__init__.py", "src/pkg/app.py"],
        ),
    ],
    ids=["add", "remove", "replace"],
)
def test_exclude_changes_sync_to_the_same_graph_as_full_build(
    tmp_path: Path, capsys, before, after, added, deleted
) -> None:
    _sample_tree(tmp_path)
    (tmp_path / "src/legacy_app/old.py").write_text(
        "def old(): return 1\n", encoding="utf-8"
    )
    (tmp_path / "src/pkg/app.py").write_text(
        "from legacy_app.old import old\ndef run(): return old()\n", encoding="utf-8"
    )
    _write_pyproject(tmp_path, before)
    output = tmp_path / "output/arcgraph"
    _command(tmp_path, output, capsys, "build")
    assert QueryEngine(output).current()["freshness"]["status"] == "fresh"
    _write_pyproject(tmp_path, after)
    stale = QueryEngine(output).current()["freshness"]
    assert stale["status"] == "stale"
    assert stale["stale_files"] == sorted(added + deleted)
    result = _command(tmp_path, output, capsys, "sync", "--if-stale")
    assert result["action"] == "reindexed"
    assert result["changed"] == {"added": added, "deleted": deleted, "modified": []}
    assert result["freshness"]["status"] == "fresh"
    complete = tmp_path / "output/complete"
    _command(tmp_path, complete, capsys, "build")
    assert _graph(output) == _graph(complete)
    assert _command(tmp_path, output, capsys, "sync")["action"] == "unchanged"


def test_scanner_reloads_exclude_without_overriding_caller_ignores(
    tmp_path: Path,
) -> None:
    _sample_tree(tmp_path)
    _write_pyproject(tmp_path, ["src/legacy_app/**"])
    scanner = FileScanner(tmp_path, [SourceRoot("src")], ["src/pkg/app.py"])
    assert [file.path for file in scanner.scan()] == ["src/pkg/__init__.py"]
    _write_pyproject(tmp_path)
    assert sorted(file.path for file in scanner.scan()) == [
        "src/legacy_app/__init__.py",
        "src/legacy_app/old.py",
        "src/pkg/__init__.py",
    ]


def test_library_build_obeys_exclude_with_explicit_roots(tmp_path: Path) -> None:
    _sample_tree(tmp_path)
    _write_pyproject(tmp_path, ["src/legacy_app/**"])
    output = tmp_path / "output/arcgraph"
    metadata, _ = ArcGraphIndexer(tmp_path, output, [SourceRoot("src")]).build()
    assert metadata.file_count == 2
    assert QueryEngine(output).current()["freshness"]["status"] == "fresh"


@pytest.mark.parametrize("suffix", [".py", ".ts", ".tsx", ".vue"])
def test_unexcluding_an_entire_language_lane_is_detected(
    tmp_path: Path, capsys, suffix: str
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    if suffix == ".py":
        (src / "visible.ts").write_text("export const value = 1;\n", encoding="utf-8")
        source = "def restored(): return 1\n"
    else:
        (src / "visible.py").write_text("def visible(): return 1\n", encoding="utf-8")
        source = (
            '<script setup lang="ts">const value = 1;</script>\n'
            if suffix == ".vue"
            else "export function restored() { return 1; }\n"
        )
    path = "src/ignored" + suffix
    (tmp_path / path).write_text(source, encoding="utf-8")
    _write_pyproject(tmp_path, [path])
    output = tmp_path / "output/arcgraph"
    _command(tmp_path, output, capsys, "build")
    assert QueryEngine(output).current()["freshness"]["status"] == "fresh"
    _write_pyproject(tmp_path)
    freshness = QueryEngine(output).current()["freshness"]
    assert freshness["status"] == "stale"
    assert freshness["stale_files"] == [path]
    result = _command(tmp_path, output, capsys, "sync", "--if-stale")
    assert result["changed"] == {"added": [path], "modified": [], "deleted": []}
    assert result["freshness"]["status"] == "fresh"
    complete = tmp_path / "output/complete"
    _command(tmp_path, complete, capsys, "build")
    assert _graph(output) == _graph(complete)
