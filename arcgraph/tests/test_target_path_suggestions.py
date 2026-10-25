"""Symbol paths remain unresolved but suggest their indexed module."""

from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreWriter
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.schemas import IndexMetadata, Node


def _engine(tmp_path: Path) -> QueryEngine:
    out = tmp_path / "out"
    GraphStoreWriter(out).write(
        IndexMetadata(
            index_version="module-paths", repo_root=str(tmp_path), source_roots=["src"]
        ),
        files=[],
        nodes=[
            Node(
                id="mod:pkg.tools",
                kind="module",
                name="tools",
                qualname="pkg.tools",
                path="src/pkg/tools.py",
            ),
            Node(
                id="fn:pkg.tools.use",
                kind="function",
                name="use",
                qualname="pkg.tools.use",
                path="src/pkg/tools.py",
            ),
            Node(
                id="method:pkg.Tools.py",
                kind="method",
                name="py",
                qualname="pkg.Tools.py",
                path="src/pkg/tools.py",
            ),
        ],
        edges=[],
        warnings=[],
    )
    return QueryEngine(out)


@pytest.mark.parametrize("spelling", ["relative", "dot", "absolute", "windows"])
def test_existing_symbol_path_suggests_only_its_module(
    tmp_path: Path, spelling: str
) -> None:
    engine = _engine(tmp_path)
    path = {
        "relative": "src/pkg/tools.py",
        "dot": "./src/pkg/tools.py",
        "absolute": str(tmp_path / "src/pkg/tools.py"),
        "windows": "src\\pkg\\tools.py",
    }[spelling]
    result = engine.symbol(path)
    assert result["matches"] == []
    resolution = result["target_resolution"]
    assert resolution["status"] == "unresolved"
    assert resolution["resolved_ids"] == []
    assert [item["id"] for item in resolution["suggestions"]] == ["mod:pkg.tools"]


def test_nonexistent_path_has_no_invented_module_suggestion(tmp_path: Path) -> None:
    resolution = _engine(tmp_path).symbol("src/pkg/missing.py")["target_resolution"]
    assert resolution["status"] == "unresolved"
    assert resolution["suggestions"] == []


def test_py_suffix_symbol_and_dotted_module_keep_qualified_resolution(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    for query, ident in [
        ("pkg.Tools.py", "method:pkg.Tools.py"),
        ("pkg.tools", "mod:pkg.tools"),
    ]:
        resolution = engine.symbol(query)["target_resolution"]
        assert resolution["status"] == "resolved"
        assert resolution["strategy"] == "exact_qualname"
        assert resolution["resolved_ids"] == [ident]
        assert resolution["suggestions"] == []


def test_analysis_path_resolution_still_selects_all_file_definitions(
    tmp_path: Path,
) -> None:
    resolution = _engine(tmp_path).impact("src/pkg/tools.py")["target_resolution"]
    assert resolution["status"] == "resolved"
    assert resolution["strategy"] == "exact_path"
    assert set(resolution["resolved_ids"]) == {
        "mod:pkg.tools",
        "fn:pkg.tools.use",
        "method:pkg.Tools.py",
    }
    assert resolution["suggestions"] == []


def test_path_suggestions_are_bounded_to_one_module(tmp_path: Path) -> None:
    out = tmp_path / "out"
    GraphStoreWriter(out).write(
        IndexMetadata(
            index_version="shared-module-path",
            repo_root=str(tmp_path),
            source_roots=["."],
        ),
        files=[],
        nodes=[
            Node(
                id=f"mod:{name}",
                kind="module",
                name=name,
                qualname=name,
                path="entry.py",
            )
            for name in ("first", "second")
        ],
        edges=[],
        warnings=[],
    )
    resolution = QueryEngine(out).symbol("entry.py")["target_resolution"]
    assert resolution["status"] == "unresolved"
    assert [item["id"] for item in resolution["suggestions"]] == ["mod:first"]
