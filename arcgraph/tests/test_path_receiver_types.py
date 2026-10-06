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


def str_if_exp_segment(root: Path, flag: bool) -> None:
    (root / ("a" if flag else "b")).is_symlink()


def str_bool_op_segment(root: Path, name: str) -> None:
    (root / (name or "x")).is_symlink()


def int_if_exp_segment(root: Path, flag: bool) -> None:
    (root / (1 if flag else 2)).is_symlink()


def int_bool_op_segment(root: Path, name) -> None:
    (root / (name or 1)).is_symlink()


def mixed_if_exp_segment(root: Path, flag: bool) -> None:
    (root / ("a" if flag else 1)).is_symlink()


def int_or_str_segment(root: Path, count: int) -> None:
    (root / (count or "x")).is_symlink()


def assigned_int_if_exp_segment(root: Path, flag: bool) -> None:
    joined = root / (1 if flag else 2)
    joined.is_symlink()


def optional_or_segment(root: Path, name: Optional[str]) -> None:
    (root / (name or "x")).is_symlink()


def assigned_optional_or_segment(root: Path, name: Optional[str]) -> None:
    joined = root / (name or "x")
    joined.is_symlink()


def optional_or_optional_segment(
    root: Path, first: Optional[str], second: Optional[str]
) -> None:
    (root / (first or second)).is_symlink()


def negative_or_segment(root: Path, name: str) -> None:
    (root / (name or -1)).is_symlink()


def arithmetic_segment(root: Path) -> None:
    (root / (1 + 2)).is_symlink()


def tuple_segment(root: Path) -> None:
    (root / (1, 2)).is_symlink()


def walrus_segment(root: Path) -> None:
    (root / (index := 1)).is_symlink()


def comparison_segment(root: Path, name: str) -> None:
    (root / (name == "a")).is_symlink()


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
        # A conditional or boolean segment whose operands are all strings.
        "str_if_exp_segment": {"extsym:pathlib.Path.is_symlink"},
        "str_bool_op_segment": {"extsym:pathlib.Path.is_symlink"},
        # A None operand of ``or`` before the last is never the result.
        "optional_or_segment": {"extsym:pathlib.Path.is_symlink"},
        "assigned_optional_or_segment": {"extsym:pathlib.Path.is_symlink"},
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

    # Neither an int segment, nor a conditional or boolean segment with an int
    # operand or a last operand that may be None, nor an expression whose form
    # shows it is no str, nor an int dividend makes a path, inline or
    # assigned, and a union does not say which member's parent is taken.
    for name in (
        "not_a_segment",
        "int_if_exp_segment",
        "int_bool_op_segment",
        "mixed_if_exp_segment",
        "int_or_str_segment",
        "assigned_int_if_exp_segment",
        "optional_or_optional_segment",
        "negative_or_segment",
        "arithmetic_segment",
        "tuple_segment",
        "walrus_segment",
        "comparison_segment",
        "not_a_path",
        "optional_parent",
    ):
        assert not any(
            target.startswith("extsym:pathlib.")
            for target, _ in resolutions.get(name, set())
        ), name
    # An Optional receiver keeps the union rules: maybe.resolve() itself is
    # linked by them, but its result stays untyped, so neither is_symlink nor
    # a receiver named after resolve is linked.
    assert not any(
        target == "extsym:pathlib.Path.is_symlink"
        or target.startswith("extsym:pathlib.Path.resolve.")
        for target, _ in resolutions.get("optional_resolved", set())
    )
    # A parent keeps its receiver's evidence barrier, under which
    # items[0].is_symlink() is not linked after this reassignment either.
    assert not any(
        target.startswith("extsym:pathlib.")
        for target, _ in resolutions.get("reassigned_parent", set())
    )
