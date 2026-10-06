"""Documented value types of external operations.

The type analyzer applies these rules to a value assigned to a name, and the
call analyzer to a receiver written inline, so both read one statement of what
each library documents.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import Any

from arcgraph.analyzers.type_unions import union_alternatives

# Factory methods on external types whose return type is part of the library's
# documented contract. Without them a receiver produced by the factory carries
# no type, and every later method call on it stays unresolved. Each entry
# claims only that the named method returns the named type; add one when the
# library documents the factory, not to move a metric.
EXTERNAL_METHOD_RETURN_TYPES = {
    ("argparse.ArgumentParser", "add_subparsers"): "argparse._SubParsersAction",
    ("argparse._SubParsersAction", "add_parser"): "argparse.ArgumentParser",
    ("argparse.ArgumentParser", "add_argument_group"): "argparse._ArgumentGroup",
    (
        "argparse.ArgumentParser",
        "add_mutually_exclusive_group",
    ): "argparse._MutuallyExclusiveGroup",
    # pathlib documents each of these as returning a new path.
    **{
        ("pathlib.Path", method): "pathlib.Path"
        for method in (
            "absolute",
            "expanduser",
            "joinpath",
            "relative_to",
            "resolve",
            "with_name",
            "with_stem",
            "with_suffix",
        )
    },
}
EXTERNAL_METHOD_RETURN_OWNERS = frozenset(
    owner for owner, _ in EXTERNAL_METHOD_RETURN_TYPES
)
# pathlib documents ``path / segment`` as a path of the left operand's flavour,
# and ``path.parent`` as its logical parent of the same flavour.
PATH_TYPE_IDS = frozenset(
    f"extsym:pathlib.{name}"
    for name in (
        "Path",
        "PosixPath",
        "PurePath",
        "PurePosixPath",
        "PureWindowsPath",
        "WindowsPath",
    )
)
PATH_SEGMENT_TYPE_IDS = PATH_TYPE_IDS | {"builtin:str", "extsym:os.PathLike"}
# Expressions whose value is never a str or an os.PathLike, whatever their
# operands: displays and comprehensions, a comparison (a bool), a lambda, and
# a unary operator, which str does not support.
_NON_SEGMENT_NODES = (
    ast.Tuple,
    ast.List,
    ast.Set,
    ast.Dict,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.Compare,
    ast.Lambda,
    ast.UnaryOp,
)


def _never_segment(node: ast.expr) -> bool:
    """Whether the expression alone shows its value is not a segment."""

    if isinstance(node, ast.Constant):
        return not isinstance(node.value, str)
    if isinstance(node, ast.BinOp):
        return _never_segment(node.left) and _never_segment(node.right)
    return isinstance(node, _NON_SEGMENT_NODES)


def may_be_path_segment(
    node: ast.expr,
    resolve: Callable[[ast.expr], dict[str, Any] | None],
    *,
    may_be_none: bool = False,
) -> bool:
    """Whether ``node`` may be the right operand of ``path / node``.

    With ``may_be_none`` a ``None`` value is accepted too, for an operand of
    ``or`` that is never the result when it is ``None``.
    """

    # pathlib joins str and os.PathLike segments. Any other operand raises
    # TypeError or hands the result to its own ``__rtruediv__``. A segment
    # of unknown type is usually an unannotated string.
    if isinstance(node, ast.Constant) and node.value is None:
        return may_be_none
    if isinstance(node, ast.JoinedStr):
        return True
    if _never_segment(node):
        return False
    if isinstance(node, ast.NamedExpr):
        return may_be_path_segment(node.value, resolve, may_be_none=may_be_none)
    # A conditional or boolean expression evaluates to one of its operands, so
    # each operand must be a segment; otherwise ``1 if flag else 2`` has no
    # resolved type and would pass as unknown.
    if isinstance(node, ast.IfExp):
        return all(
            may_be_path_segment(branch, resolve, may_be_none=may_be_none)
            for branch in (node.body, node.orelse)
        )
    if isinstance(node, ast.BoolOp):
        # ``or`` returns its first truthy operand or else its last, so a None
        # before the last is never the result; ``and`` returns its first falsy
        # operand, which may be None.
        *leading, last = node.values
        skips_none = isinstance(node.op, ast.Or)
        return all(
            may_be_path_segment(value, resolve, may_be_none=skips_none)
            for value in leading
        ) and may_be_path_segment(last, resolve, may_be_none=may_be_none)
    ref = resolve(node)
    members = union_alternatives(ref)
    if members is None:
        members = [ref] if ref and ref.get("type_id") else []
    accepted = PATH_SEGMENT_TYPE_IDS | ({"builtin:None"} if may_be_none else set())
    return all(member.get("type_id") in accepted for member in members)
