"""Calls on paths built with ``/`` resolve by type, even in nested scopes."""

from __future__ import annotations

from pathlib import Path

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.semantic_metrics import (
    collect_callsite_records,
    resolved_callsite_ids,
)
from arcgraph.pipeline.indexer import ArcGraphIndexer

SOURCE = """from pathlib import Path


def with_nested_scope(tmp_path: Path) -> None:
    pointer = tmp_path / "current.json"
    pointer.is_symlink()
    deep = tmp_path / "a" / "b.txt"
    deep.stat()

    def helper() -> int:
        return 1

    helper()


def ratio(a: int, b: int) -> None:
    value = a / b
    value.as_integer_ratio()
"""


def _targets_by_expression(tmp_path: Path) -> dict[str, set[str]]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lab.py").write_text(SOURCE, encoding="utf-8")
    output = tmp_path / "out"
    ArcGraphIndexer(
        repo_root=repo, output_dir=output, source_roots=[SourceRoot("src")]
    ).build()
    store = GraphStoreReader.from_current(output)
    nodes, edges = store.read_nodes(), store.read_edges()
    expression_by_callsite = {
        record.callsite_id: record.raw_expression
        for record in collect_callsite_records(nodes)
    }
    targets: dict[str, set[str]] = {}
    for edge in edges:
        for callsite_id in resolved_callsite_ids([edge]):
            expression = expression_by_callsite.get(callsite_id)
            if expression is not None:
                targets.setdefault(expression, set()).add(edge.target)
    return targets


def test_joined_paths_resolve_by_type_inside_a_scope_with_a_nested_def(
    tmp_path: Path,
) -> None:
    targets = _targets_by_expression(tmp_path)

    assert targets.get("pointer.is_symlink") == {"extsym:pathlib.Path.is_symlink"}
    assert targets.get("deep.stat") == {"extsym:pathlib.Path.stat"}
    assert "value.as_integer_ratio" not in targets
