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
# What a segment expression may evaluate to: a kind and a truth value. The
# kinds are "str", "path" (a pathlib path or an os.PathLike), "int" (an int or
# a bool, which can repeat a str), "none", "other" for any other value,
# including an operation that raises, and "unknown" for a value of no resolved
# type. The truth is "true", "false" or "either".
_Value = tuple[str, str]
_SEGMENT_KINDS = frozenset({"str", "path", "unknown"})
_UNKNOWN: frozenset[_Value] = frozenset({("unknown", "either")})
_BOOL: frozenset[_Value] = frozenset({("int", "either")})
# Comprehensions are never a str or a path.
_COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
# Operators that keep two ints an int; true division gives a float, and a
# power with a negative exponent does too.
_INT_OPERATORS = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.FloorDiv,
    ast.Mod,
    ast.LShift,
    ast.RShift,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
)


def may_be_path_segment(
    node: ast.expr,
    resolve: Callable[[ast.expr], dict[str, Any] | None],
) -> bool:
    """Whether ``node`` may be the right operand of ``path / node``."""

    # pathlib joins str and os.PathLike segments. Any other operand raises
    # TypeError or hands the result to its own ``__rtruediv__``. A segment
    # of unknown type is usually an unannotated string.
    return all(kind in _SEGMENT_KINDS for kind, _ in _values(node, resolve))


def _values(
    node: ast.expr,
    resolve: Callable[[ast.expr], dict[str, Any] | None],
) -> frozenset[_Value]:
    """The values ``node`` may evaluate to, read from its form or its type."""

    if isinstance(node, ast.Constant):
        return frozenset({(_constant_kind(node.value), _truth(bool(node.value)))})
    if isinstance(node, ast.JoinedStr):
        # Literal text makes the string non-empty whatever the fields hold.
        literal = any(
            isinstance(part, ast.Constant) and part.value for part in node.values
        )
        return frozenset({("str", "true" if literal else "either")})
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return frozenset({("other", _truth(bool(node.elts)))})
    if isinstance(node, ast.Dict):
        return frozenset({("other", _truth(bool(node.keys)))})
    if isinstance(node, _COMPREHENSION_NODES):
        return frozenset({("other", "either")})
    if isinstance(node, ast.Lambda):
        return frozenset({("other", "true")})
    if isinstance(node, ast.Compare):
        return _BOOL
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return _BOOL
        # -x, +x and ~x keep an int an int; no str or path supports them.
        operand = _values(node.operand, resolve)
        return frozenset(
            ("int" if kind == "int" else "other", "either") for kind, _ in operand
        )
    if isinstance(node, ast.NamedExpr):
        return _values(node.value, resolve)
    if isinstance(node, ast.IfExp):
        return _conditional_values(node, resolve)
    if isinstance(node, ast.BoolOp):
        return _boolean_values(node, resolve)
    typed = _typed_values(resolve(node))
    if typed is not None:
        return typed
    if isinstance(node, ast.BinOp):
        return _binary_values(node, resolve)
    return _UNKNOWN


def _constant_kind(value: object) -> str:
    if isinstance(value, str):
        return "str"
    if value is None:
        return "none"
    # bool is a subclass of int.
    return "int" if isinstance(value, int) else "other"


def _truth(truthy: bool) -> str:
    return "true" if truthy else "false"


def _typed_values(ref: dict[str, Any] | None) -> frozenset[_Value] | None:
    members = union_alternatives(ref)
    if members is None:
        if not ref or not ref.get("type_id"):
            return None
        members = [ref]
    values: set[_Value] = set()
    for member in members:
        type_id = member.get("type_id")
        if type_id == "builtin:str":
            values.add(("str", "either"))
        elif type_id in PATH_TYPE_IDS:
            values.add(("path", "true"))
        elif type_id == "extsym:os.PathLike":
            values.add(("path", "either"))
        elif type_id in {"builtin:int", "builtin:bool"}:
            values.add(("int", "either"))
        elif type_id == "builtin:None":
            values.add(("none", "false"))
        else:
            # Includes a member of no resolved type, which keeps the union's
            # evidence barrier.
            values.add(("other", "either"))
    return frozenset(values)


def _conditional_values(
    node: ast.IfExp,
    resolve: Callable[[ast.expr], dict[str, Any] | None],
) -> frozenset[_Value]:
    body = _values(node.body, resolve)
    orelse = _values(node.orelse, resolve)
    narrowing = _narrowing(node.test)
    if narrowing is not None:
        # ``name if name is not None else ...`` and ``name if name else ...``
        # read the name only where the test has excluded a value.
        name, narrow = narrowing
        if isinstance(node.body, ast.Name) and node.body.id == name:
            body = narrow(body, True)
        if isinstance(node.orelse, ast.Name) and node.orelse.id == name:
            orelse = narrow(orelse, False)
    return body | orelse


_Narrow = Callable[[frozenset[_Value], bool], frozenset[_Value]]


def _narrowing(test: ast.expr) -> tuple[str, _Narrow] | None:
    """The name a test narrows, and the values it leaves that name when the
    test is true or false: ``name``, ``not name``, ``name is None`` and
    ``name is not None``, with None on either side of ``is``."""

    negated = False
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test, negated = test.operand, True
    if isinstance(test, ast.Name):

        def by_truth(values: frozenset[_Value], outcome: bool) -> frozenset[_Value]:
            truth = _truth(outcome != negated)
            return frozenset(
                (kind, truth) for kind, value in values if value in {truth, "either"}
            )

        return test.id, by_truth
    if (
        negated
        or not isinstance(test, ast.Compare)
        or len(test.ops) != 1
        or not isinstance(test.ops[0], (ast.Is, ast.IsNot))
    ):
        return None
    left, right = test.left, test.comparators[0]
    if isinstance(left, ast.Constant) and left.value is None:
        left, right = right, left
    if not (
        isinstance(left, ast.Name)
        and isinstance(right, ast.Constant)
        and right.value is None
    ):
        return None
    is_not = isinstance(test.ops[0], ast.IsNot)

    def by_none(values: frozenset[_Value], outcome: bool) -> frozenset[_Value]:
        keep_none = outcome != is_not
        return frozenset(value for value in values if (value[0] == "none") == keep_none)

    return left.id, by_none


def _boolean_values(
    node: ast.BoolOp,
    resolve: Callable[[ast.expr], dict[str, Any] | None],
) -> frozenset[_Value]:
    # ``or`` returns its first truthy operand and ``and`` its first falsy one;
    # if there is none, both return the last operand.
    decisive = "true" if isinstance(node.op, ast.Or) else "false"
    values: set[_Value] = set()
    *leading, last = node.values
    for operand in leading:
        operand_values = _values(operand, resolve)
        values.update(
            value for value in operand_values if value[1] in {decisive, "either"}
        )
        if all(value[1] == decisive for value in operand_values):
            return frozenset(values)
    return frozenset(values | _values(last, resolve))


def _binary_values(
    node: ast.BinOp,
    resolve: Callable[[ast.expr], dict[str, Any] | None],
) -> frozenset[_Value]:
    left = _values(node.left, resolve)
    right = _values(node.right, resolve)
    return frozenset(
        _binary_value(node, left_value, right_value)
        for left_value in left
        for right_value in right
    )


def _binary_value(node: ast.BinOp, left: _Value, right: _Value) -> _Value:
    kind = _binary_kind(node.op, left[0], right[0])
    if kind == "path":
        return kind, "true"
    if kind == "str" and isinstance(node.op, ast.Add) and "true" in {left[1], right[1]}:
        return kind, "true"
    if kind == "str" and isinstance(node.op, ast.Mult):
        # A non-empty string repeated a positive literal number of times.
        text, count = (left, node.right) if left[0] == "str" else (right, node.left)
        if text[1] == "true" and _positive_int_literal(count):
            return kind, "true"
    return kind, "either"


def _positive_int_literal(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and node.value > 0
    )


def _binary_kind(op: ast.operator, left: str, right: str) -> str:
    """The kind of ``left op right``; "other" also stands for TypeError."""

    kinds = {left, right}
    if isinstance(op, ast.Add) and kinds <= {"str", "unknown"}:
        # str + str is a str.
        return "unknown" if "unknown" in kinds else "str"
    if isinstance(op, ast.Mult):
        # str * int and int * str repeat the string.
        if kinds == {"str", "int"}:
            return "str"
        if "unknown" in kinds and kinds <= {"str", "int", "unknown"}:
            return "unknown"
    if isinstance(op, ast.Mod) and left in {"str", "unknown"}:
        # printf-style formatting keeps the left string.
        return left
    if isinstance(op, ast.Div):
        # A path joins a segment on either side.
        if left == "path" and right in _SEGMENT_KINDS:
            return "path"
        if right == "path" and left in {"str", "unknown"}:
            return "path"
        if "unknown" in kinds and kinds <= _SEGMENT_KINDS:
            return "unknown"
    if kinds == {"int"} and isinstance(op, _INT_OPERATORS):
        return "int"
    # No other combination yields a str, a path or an int.
    return "other"
