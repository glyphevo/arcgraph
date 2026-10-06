"""Calls on paths built with ``/`` resolve by type, even in nested scopes.

A receiver written inline, such as ``path.parent`` or ``root / name``, is typed
by the pathlib rules the type analyzer applies to a local assigned from the
same expression.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def reversed_join(other: Path) -> None:
    ("a" / other).is_symlink()


def reversed_join_assigned(other: Path) -> None:
    joined = "a" / other
    joined.is_symlink()


def int_over_path(other: Path) -> None:
    (2 / other).is_symlink()


def reversed_pure_join(other: PurePosixPath) -> None:
    ("a" / other).with_suffix(".txt")


def resolved(root: Path) -> None:
    root.resolve().is_symlink()


def relative(path: Path, root: Path) -> None:
    path.resolve().relative_to(root).as_posix()


def nested_scope(tmp_path: Path) -> None:
    tmp_path.parent.is_symlink()

    def helper() -> int:
        return 1

    helper()


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


def pair(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path, tmp_path


def rebound_pair(tmp_path: Path) -> None:
    repo, other = pair(tmp_path)
    repo, other = pair(tmp_path)
    repo.is_symlink()
    (repo / "a").chmod(0o700)
    repo.parent.mkdir()


def reassigned_join(items: Optional[list[Path]]) -> None:
    items = maybe_paths()
    (items[0] / "a").is_symlink()


def reassigned_reversed_join(items: Optional[list[Path]]) -> None:
    items = maybe_paths()
    ("a" / items[0]).is_symlink()


def reassigned_joined_parent(items: Optional[list[Path]]) -> None:
    items = maybe_paths()
    ("a" / items[0]).parent.is_symlink()
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


def _resolutions_by_function(
    tmp_path: Path, source: str = CHAINED_SOURCE
) -> dict[str, set[tuple[str, str]]]:
    """Each function's resolved (target, strategy) pairs, one per callsite."""

    _, edges = _index(tmp_path, source)
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
        # str / path is a path of the right operand's flavour.
        "reversed_join": {"extsym:pathlib.Path.is_symlink"},
        "reversed_join_assigned": {"extsym:pathlib.Path.is_symlink"},
        "reversed_pure_join": {"extsym:pathlib.PurePosixPath.with_suffix"},
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
    # A parent or a join of an available value is available where the value
    # is, even after the name was bound again.
    assert {
        ("extsym:pathlib.Path.is_symlink", "external_receiver_type"),
        ("extsym:pathlib.Path.chmod", "external_receiver_type"),
        ("extsym:pathlib.Path.mkdir", "external_receiver_type"),
    } <= resolutions.get("rebound_pair", set())

    # An int dividend does not make a path, nor does an int divided by a path,
    # and a union does not say which member's parent is taken.
    for name in ("not_a_path", "int_over_path", "optional_parent"):
        assert not any(
            target.startswith("extsym:pathlib.")
            for target, _ in resolutions.get(name, set())
        ), name
    # An Optional receiver keeps the union rules: maybe.resolve() itself is
    # linked by them, but its result stays untyped, so neither is_symlink nor
    # a receiver named after resolve is linked.
    assert ("extsym:pathlib.Path.resolve", "external_receiver_type") in (
        resolutions.get("optional_resolved", set())
    )
    assert not any(
        target == "extsym:pathlib.Path.is_symlink"
        or target.startswith("extsym:pathlib.Path.resolve.")
        for target, _ in resolutions.get("optional_resolved", set())
    )
    # A parent and a join, on either side, keep the evidence barrier of the
    # path they come from, under which items[0].is_symlink() is not linked
    # after this reassignment either.
    for name in (
        "reassigned_parent",
        "reassigned_join",
        "reassigned_reversed_join",
        "reassigned_joined_parent",
    ):
        assert not any(
            target.startswith("extsym:pathlib.")
            for target, _ in resolutions.get(name, set())
        ), name


# Each case joins ``root: Path`` with a segment and calls is_symlink on the
# result, written inline and through a local. pathlib joins a str or an
# os.PathLike segment; any other value raises TypeError, so the call must not
# be linked. A segment of unknown type passes as the usual unannotated string.
# Each entry is (extra parameters, segment, whether it makes a path).
SEGMENT_CASES = {
    "string": ("", '"a"', True),
    "integer": ("", "2", False),
    "bytes_literal": ("", 'b"a"', False),
    "f_string": (", name: str", 'f"{name}.txt"', True),
    "str_name": (", name: str", "name", True),
    "unannotated_name": (", name", "name", True),
    "path_name": (", other: Path", "other", True),
    "path_like_name": (", other: PathLike[str]", "other", True),
    "optional_name": (", name: Optional[str]", "name", False),
    # A conditional evaluates to one of its branches.
    "str_conditional": (", flag: bool", '"a" if flag else "b"', True),
    "int_conditional": (", flag: bool", "1 if flag else 2", False),
    "mixed_conditional": (", flag: bool", '"a" if flag else 1', False),
    # A test on the name itself narrows the branch that reads it.
    "not_none_branch": (
        ", name: Optional[str]",
        'name if name is not None else "x"',
        True,
    ),
    "none_else_branch": (
        ", name: Optional[str]",
        '"x" if name is None else name',
        True,
    ),
    "truthy_branch": (", name: Optional[str]", 'name if name else "x"', True),
    "unrelated_test_branch": (
        ", name: Optional[str], flag: int",
        'name if flag == 1 else "x"',
        False,
    ),
    "falsy_else_branch": (", name: Optional[str]", '"x" if not name else name', True),
    "reversed_not_none_branch": (
        ", name: Optional[str]",
        'name if None is not name else "x"',
        True,
    ),
    "identity_branch": (
        ", name: Optional[str], other: object",
        'name if name is other else "x"',
        False,
    ),
    "non_identity_branch": (
        ", name: Optional[str], other: object",
        'name if name is not other else "x"',
        False,
    ),
    "none_branch": (", name: Optional[str]", 'name if name is None else "x"', False),
    "other_name_branch": (
        ", name: Optional[str], other: Optional[str]",
        'other if name is not None else "x"',
        False,
    ),
    # ``or`` returns its first truthy operand, ``and`` its first falsy one,
    # and either returns its last operand when none is decisive.
    "str_or": (", name: str", 'name or "x"', True),
    "optional_or": (", name: Optional[str]", 'name or "x"', True),
    "union_or": (", name: str | None", 'name or "x"', True),
    "optional_or_optional": (
        ", first: Optional[str], second: Optional[str]",
        "first or second",
        False,
    ),
    "int_or": (", count: int", 'count or "x"', False),
    "unannotated_or_int": (", name", "name or 1", False),
    "zero_or": ("", '0 or "x"', True),
    "empty_tuple_or": ("", '() or "x"', True),
    "tuple_or": ("", '(1, 2) or "x"', False),
    "empty_dict_or": ("", '{} or "x"', True),
    "str_or_none": ("", '"x" or None', True),
    # A value that is always truthy decides ``or`` however it was computed.
    "concatenation_or": ("", '("a" + "b") or 1', True),
    "not_or": ("", '(not 1) or "x"', True),
    "negated_zero_or": ("", '(-0) or "x"', True),
    "inverted_zero_or": ("", '(~0) or "x"', False),
    "negative_repetition_or": ("", '("a" * -2) or 1', False),
    "repetition_or": ("", '("a" * 2) or 1', True),
    "empty_repetition_or": ("", '("a" * 0) or 1', False),
    "f_string_or": ("", 'f"a" or 1', True),
    "narrowed_or": (", name: Optional[str]", '(name if name else "x") or 1', True),
    "computed_path_or": (
        ", other: Path, flag: bool",
        '("a" / (other if flag else other)) or 1',
        True,
    ),
    "reversed_join_or": (", other: Path", '("a" / other) or 1', True),
    "conditional_or": (
        ", name: Optional[str], flag: bool",
        '(name or "a") if flag else (name or "b")',
        True,
    ),
    "str_and": (", name: str", 'name and "x"', True),
    "bool_and": (", flag: bool", 'flag and "x"', False),
    "none_and": ("", 'None and "x"', False),
    "optional_and": (", name: Optional[str]", 'name and "x"', False),
    # Arithmetic follows each operator: only str + str concatenates, str * int
    # repeats, and str % values formats.
    "str_plus_str": (", name: str", 'name + ".txt"', True),
    "str_plus_int": ("", '"a" + 1', False),
    "int_plus_int": (", count: int", "count + 1", False),
    "unannotated_plus_int": (", name", "name + 1", False),
    "str_times_int": ("", '"a" * 2', True),
    "int_times_str": ("", '2 * "a"', True),
    "unannotated_times_int": (", name", "name * 2", True),
    "bool_times_str": ("", '"a" * True', True),
    "int_expression_times_str": (", count: int", '"a" * (count + 1)', True),
    "negated_times_str": (", count: int", '"a" * -count', True),
    "comparison_times_str": (", name: str", '"a" * (name == "b")', True),
    "not_times_str": (", flag: bool", '"a" * (not flag)', True),
    "true_division_times_str": (", count: int", '"a" * (count / 2)', False),
    "floor_division_times_str": (", count: int", '"a" * (count // 2)', True),
    "modulo_times_str": (", count: int", '"a" * (count % 3)', True),
    "left_shift_times_str": (", count: int", '"a" * (1 << count)', True),
    "right_shift_times_str": (", count: int", '"a" * (count >> 1)', True),
    "bit_and_times_str": (", count: int", '"a" * (count & 3)', True),
    "bit_or_times_str": (", count: int", '"a" * (count | 1)', True),
    "bit_xor_times_str": (", count: int", '"a" * (count ^ 1)', True),
    "power_times_str": (", count: int", '"a" * (count ** 2)', True),
    "negative_power_times_str": (", count: int", '"a" * (count ** -1)', False),
    "zero_floor_division_times_str": (", count: int", '"a" * (count // 0)', False),
    "zero_modulo_times_str": (", count: int", '"a" * (count % 0)', False),
    "float_times_str": ("", '"a" * 1.5', False),
    "float_name_times_str": (", ratio: float", '"a" * ratio', False),
    "list_times_str": ("", '"a" * [1]', False),
    "str_times_str": ("", '"a" * "b"', False),
    "str_format": (", count: int", '"%s" % count', True),
    "int_modulo": (", count: int", "count % 2", False),
    "int_minus_int": (", count: int", "count - 1", False),
    "negative": ("", "-1", False),
    "negated_str": (", name: str", "-name", False),
    # A path joins a segment on either side of ``/``.
    "str_over_path": (", other: Path", '"a" / other', True),
    "conditional_path_over_str": (
        ", other: Path, flag: bool",
        '(other if flag else other) / "a"',
        True,
    ),
    "str_over_conditional_path": (
        ", other: Path, flag: bool",
        '"a" / (other if flag else other)',
        True,
    ),
    "str_over_str": ("", '"a" / "b"', False),
    "unannotated_over_str": (", name", 'name / "a"', True),
    # Forms whose value is never a str or a path.
    "tuple": ("", "1, 2", False),
    "list": ("", "[1]", False),
    "set": ("", "{1}", False),
    "dict": ("", '{"a": 1}', False),
    "comprehension": ("", "[c for c in 'ab']", False),
    "lambda_expression": ("", 'lambda: "a"', False),
    "comparison": (", name: str", 'name == "a"', False),
    "int_walrus": ("", "index := 1", False),
    "str_walrus": ("", 'index := "a"', True),
}


def _segment_source() -> str:
    lines = [
        "from os import PathLike",
        "from pathlib import Path",
        "from typing import Optional",
    ]
    for name, (parameters, segment, _) in SEGMENT_CASES.items():
        lines += [
            "",
            "",
            f"def {name}(root: Path{parameters}) -> None:",
            f"    (root / ({segment})).is_symlink()",
            "",
            "",
            f"def {name}_assigned(root: Path{parameters}) -> None:",
            f"    joined = root / ({segment})",
            "    joined.is_symlink()",
        ]
    source = "\n".join(lines) + "\n"
    # A case that does not parse would silently drop every case from the index.
    compile(source, "lab.py", "exec")
    return source


@pytest.fixture(scope="module")
def segment_resolutions(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, set[tuple[str, str]]]:
    return _resolutions_by_function(
        tmp_path_factory.mktemp("segments"), _segment_source()
    )


@pytest.mark.parametrize("form", ["", "_assigned"])
@pytest.mark.parametrize("case", list(SEGMENT_CASES))
def test_path_segments_follow_python_semantics(
    segment_resolutions: dict[str, set[tuple[str, str]]], case: str, form: str
) -> None:
    _, segment, makes_path = SEGMENT_CASES[case]
    resolutions = segment_resolutions.get(case + form, set())
    if makes_path:
        assert ("extsym:pathlib.Path.is_symlink", "external_receiver_type") in (
            resolutions
        ), segment
    else:
        assert not any(
            target.startswith("extsym:pathlib.") for target, _ in resolutions
        ), segment
