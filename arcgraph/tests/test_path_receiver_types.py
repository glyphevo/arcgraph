"""Calls on paths built with ``/`` resolve by type, even in nested scopes.

A receiver written inline, such as ``path.parent`` or ``root / name``, is typed
by the pathlib rules the type analyzer applies to a local assigned from the
same expression.
"""

from __future__ import annotations

from pathlib import Path

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import Edge, Node
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


CHAINED_SOURCE = """from pathlib import Path, PurePosixPath
from typing import Optional


def parent_mkdir(path: Path) -> None:
    path.parent.mkdir()


def parent_of_parent(path: Path) -> None:
    path.parent.parent.is_symlink()


def pure_parent(path: PurePosixPath) -> None:
    path.parent.with_suffix(".txt")


def joined(root: Path, name: str) -> None:
    (root / name).is_symlink()


def joined_parent(root: Path) -> None:
    (root / "a" / "b.txt").parent.chmod(0o700)


def resolved(root: Path) -> None:
    root.resolve().is_symlink()


def relative(path: Path, root: Path) -> None:
    path.resolve().relative_to(root).as_posix()


def nested_scope(tmp_path: Path) -> None:
    tmp_path.parent.is_symlink()

    def helper() -> int:
        return 1

    helper()


def not_a_segment(root: Path) -> None:
    (root / 2).is_symlink()


def not_a_path(count: int) -> None:
    (count / 2).is_integer()


def optional_parent(maybe: Optional[Path]) -> None:
    maybe.parent.is_symlink()


def optional_resolved(maybe: Optional[Path]) -> None:
    maybe.resolve().is_symlink()


def maybe_paths() -> Optional[list[Path]]:
    return None


def reassigned_parent(items: Optional[list[Path]]) -> None:
    items = maybe_paths()
    items[0].parent.is_symlink()
"""


def _index(tmp_path: Path, source: str) -> tuple[list[Node], list[Edge]]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lab.py").write_text(source, encoding="utf-8")
    output = tmp_path / "out"
    ArcGraphIndexer(
        repo_root=repo, output_dir=output, source_roots=[SourceRoot("src")]
    ).build()
    store = GraphStoreReader.from_current(output)
    return store.read_nodes(), store.read_edges()


def _targets_by_expression(tmp_path: Path) -> dict[str, set[str]]:
    nodes, edges = _index(tmp_path, SOURCE)
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


def _resolutions_by_function(tmp_path: Path) -> dict[str, set[tuple[str, str]]]:
    """Each function's resolved (target, strategy) pairs, one per callsite."""

    _, edges = _index(tmp_path, CHAINED_SOURCE)
    resolutions: dict[str, set[tuple[str, str]]] = {}
    for edge in edges:
        if edge.resolution.status != "resolved":
            continue
        callsites = edge.properties.get("callsites") or [
            edge.properties.get("callsite")
        ]
        for callsite in callsites:
            if isinstance(callsite, dict):
                resolutions.setdefault(edge.source.rsplit(".", 1)[-1], set()).add(
                    (edge.target, str(callsite.get("resolution_strategy")))
                )
    return resolutions


def test_inline_path_receivers_resolve_by_type(tmp_path: Path) -> None:
    resolutions = _resolutions_by_function(tmp_path)
    expected = {
        # mkdir is also on a list of common method names, so the strategy is
        # what shows that the receiver's type was read.
        "parent_mkdir": {"extsym:pathlib.Path.mkdir"},
        "parent_of_parent": {"extsym:pathlib.Path.is_symlink"},
        # The parent of a pure path is a pure path of the same flavour.
        "pure_parent": {"extsym:pathlib.PurePosixPath.with_suffix"},
        "joined": {"extsym:pathlib.Path.is_symlink"},
        "joined_parent": {"extsym:pathlib.Path.chmod"},
        # A documented path-returning method yields a path, not a receiver
        # named after the method, such as pathlib.Path.resolve.is_symlink.
        "resolved": {"extsym:pathlib.Path.resolve", "extsym:pathlib.Path.is_symlink"},
        "relative": {
            "extsym:pathlib.Path.resolve",
            "extsym:pathlib.Path.relative_to",
            "extsym:pathlib.Path.as_posix",
        },
    }
    for name, targets in expected.items():
        assert resolutions.get(name, set()) == {
            (target, "external_receiver_type") for target in targets
        }, name
    assert ("extsym:pathlib.Path.is_symlink", "external_receiver_type") in (
        resolutions.get("nested_scope", set())
    )

    # Neither an int segment nor an int dividend makes a path, and a union
    # does not say which member's parent is taken.
    for name in ("not_a_segment", "not_a_path", "optional_parent"):
        assert not any(
            target.startswith("extsym:pathlib.")
            for target, _ in resolutions.get(name, set())
        ), name
    # An Optional receiver keeps the union rules, which leave the result of
    # maybe.resolve() untyped.
    assert not any(
        target == "extsym:pathlib.Path.is_symlink"
        for target, _ in resolutions.get("optional_resolved", set())
    )
    # A parent keeps its receiver's evidence barrier, under which
    # items[0].is_symlink() is not linked after this reassignment either.
    assert not any(
        target.startswith("extsym:pathlib.")
        for target, _ in resolutions.get("reassigned_parent", set())
    )
