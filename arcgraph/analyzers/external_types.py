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

# Methods whose return type the library or the language documents, keyed by
# the receiver's type id. The value is the returned type id and, for a list,
# its element type id. Without an entry, a value returned by the method carries
# no type and later calls on it are not linked by type. Each entry claims only
# what the documentation says; add one for that reason, not to move a metric.
_PATH_FLAVOURS = ("Path", "PosixPath", "WindowsPath")
_PURE_PATH_FLAVOURS = ("PurePath", "PurePosixPath", "PureWindowsPath")
_STR_RETURNING = (
    "capitalize",
    "casefold",
    "center",
    "expandtabs",
    "format",
    "join",
    "ljust",
    "lower",
    "lstrip",
    "removeprefix",
    "removesuffix",
    "replace",
    "rjust",
    "rstrip",
    "strip",
    "swapcase",
    "title",
    "translate",
    "upper",
    "zfill",
)
_BYTES_RETURNING = (
    "join",
    "lower",
    "lstrip",
    "removeprefix",
    "removesuffix",
    "replace",
    "rstrip",
    "strip",
    "upper",
)
_BOOL_RETURNING = ("endswith", "startswith")
_STR_PREDICATES = (
    "isalnum",
    "isalpha",
    "isascii",
    "isdecimal",
    "isdigit",
    "isidentifier",
    "islower",
    "isnumeric",
    "isprintable",
    "isspace",
    "istitle",
    "isupper",
)
_INT_RETURNING = ("count", "find", "index", "rfind", "rindex")
METHOD_RETURN_TYPES: dict[tuple[str, str], tuple[str, str | None]] = {
    ("extsym:argparse.ArgumentParser", "add_subparsers"): (
        "extsym:argparse._SubParsersAction",
        None,
    ),
    ("extsym:argparse._SubParsersAction", "add_parser"): (
        "extsym:argparse.ArgumentParser",
        None,
    ),
    ("extsym:argparse.ArgumentParser", "add_argument_group"): (
        "extsym:argparse._ArgumentGroup",
        None,
    ),
    ("extsym:argparse.ArgumentParser", "add_mutually_exclusive_group"): (
        "extsym:argparse._MutuallyExclusiveGroup",
        None,
    ),
    # pathlib: every flavour derives a path of its own flavour and a str; a
    # concrete path also resolves itself and reads the file system.
    **{
        (f"extsym:pathlib.{flavour}", method): (f"extsym:pathlib.{flavour}", None)
        for flavour in _PATH_FLAVOURS + _PURE_PATH_FLAVOURS
        for method in (
            "joinpath",
            "relative_to",
            "with_name",
            "with_stem",
            "with_suffix",
        )
    },
    **{
        (f"extsym:pathlib.{flavour}", "as_posix"): ("builtin:str", None)
        for flavour in _PATH_FLAVOURS + _PURE_PATH_FLAVOURS
    },
    **{
        (f"extsym:pathlib.{flavour}", method): (f"extsym:pathlib.{flavour}", None)
        for flavour in _PATH_FLAVOURS
        for method in ("absolute", "expanduser", "resolve")
    },
    **{
        (f"extsym:pathlib.{flavour}", method): returned
        for flavour in _PATH_FLAVOURS
        for method, returned in (
            ("read_text", ("builtin:str", None)),
            ("read_bytes", ("builtin:bytes", None)),
            ("exists", ("builtin:bool", None)),
            ("is_dir", ("builtin:bool", None)),
            ("is_file", ("builtin:bool", None)),
            ("is_symlink", ("builtin:bool", None)),
        )
    },
    # str and bytes, as the language reference documents their methods.
    **{("builtin:str", method): ("builtin:str", None) for method in _STR_RETURNING},
    **{
        ("builtin:str", method): ("builtin:list", "builtin:str")
        for method in ("rsplit", "split", "splitlines")
    },
    ("builtin:str", "encode"): ("builtin:bytes", None),
    **{
        ("builtin:str", method): ("builtin:bool", None)
        for method in _BOOL_RETURNING + _STR_PREDICATES
    },
    **{("builtin:str", method): ("builtin:int", None) for method in _INT_RETURNING},
    **{
        ("builtin:bytes", method): ("builtin:bytes", None)
        for method in _BYTES_RETURNING
    },
    **{
        ("builtin:bytes", method): ("builtin:list", "builtin:bytes")
        for method in ("rsplit", "split", "splitlines")
    },
    ("builtin:bytes", "decode"): ("builtin:str", None),
    **{("builtin:bytes", method): ("builtin:bool", None) for method in _BOOL_RETURNING},
    **{("builtin:bytes", method): ("builtin:int", None) for method in _INT_RETURNING},
}
# Standard library functions and class methods whose documented return does not
# depend on their arguments, by qualified name. A function whose result type
# follows its input, such as re.sub, or that may return None, such as
# os.environ.get, has no entry.
_OPENSSL_HASHES = ("md5", "sha1", "sha224", "sha256", "sha384", "sha512")
FUNCTION_RETURN_TYPES: dict[str, tuple[str, str | None]] = {
    **{f"hashlib.{name}": ("extsym:_hashlib.HASH", None) for name in _OPENSSL_HASHES},
    "json.dumps": ("builtin:str", None),
    "tomllib.loads": ("builtin:dict", None),
    "base64.b64decode": ("builtin:bytes", None),
    "base64.b64encode": ("builtin:bytes", None),
    "base64.urlsafe_b64decode": ("builtin:bytes", None),
    "base64.urlsafe_b64encode": ("builtin:bytes", None),
    "zlib.compress": ("builtin:bytes", None),
    "zlib.decompress": ("builtin:bytes", None),
    "subprocess.run": ("extsym:subprocess.CompletedProcess", None),
    **{
        f"datetime.datetime.{name}": ("extsym:datetime.datetime", None)
        for name in ("now", "utcnow", "fromisoformat", "fromtimestamp")
    },
}
METHOD_RETURN_TYPES.update(
    {
        ("extsym:_hashlib.HASH", "hexdigest"): ("builtin:str", None),
        ("extsym:_hashlib.HASH", "digest"): ("builtin:bytes", None),
        ("extsym:datetime.datetime", "isoformat"): ("builtin:str", None),
        ("extsym:datetime.datetime", "strftime"): ("builtin:str", None),
    }
)
# Every public method of these external types on the supported Python versions
# (3.11 and 3.12; a method on either counts). A call on a receiver of one of
# these types names a method of the type or nothing, so pure.parent.mkdir()
# is not linked to a PurePosixPath.mkdir that does not exist.
# test_external_methods_cover_every_public_method checks the running
# interpreter against it.
EXTERNAL_METHODS_BY_TYPE: dict[str, frozenset[str]] = {
    "extsym:_hashlib.HASH": frozenset(
        {
            "copy",
            "digest",
            "hexdigest",
            "update",
        }
    ),
    "extsym:datetime.datetime": frozenset(
        {
            "astimezone",
            "combine",
            "ctime",
            "date",
            "dst",
            "fromisocalendar",
            "fromisoformat",
            "fromordinal",
            "fromtimestamp",
            "isocalendar",
            "isoformat",
            "isoweekday",
            "now",
            "replace",
            "strftime",
            "strptime",
            "time",
            "timestamp",
            "timetuple",
            "timetz",
            "today",
            "toordinal",
            "tzname",
            "utcfromtimestamp",
            "utcnow",
            "utcoffset",
            "utctimetuple",
            "weekday",
        }
    ),
    "extsym:pathlib.Path": frozenset(
        {
            "absolute",
            "as_posix",
            "as_uri",
            "chmod",
            "cwd",
            "exists",
            "expanduser",
            "glob",
            "group",
            "hardlink_to",
            "home",
            "is_absolute",
            "is_block_device",
            "is_char_device",
            "is_dir",
            "is_fifo",
            "is_file",
            "is_junction",
            "is_mount",
            "is_relative_to",
            "is_reserved",
            "is_socket",
            "is_symlink",
            "iterdir",
            "joinpath",
            "lchmod",
            "link_to",
            "lstat",
            "match",
            "mkdir",
            "open",
            "owner",
            "read_bytes",
            "read_text",
            "readlink",
            "relative_to",
            "rename",
            "replace",
            "resolve",
            "rglob",
            "rmdir",
            "samefile",
            "stat",
            "symlink_to",
            "touch",
            "unlink",
            "walk",
            "with_name",
            "with_segments",
            "with_stem",
            "with_suffix",
            "write_bytes",
            "write_text",
        }
    ),
    "extsym:pathlib.PosixPath": frozenset(
        {
            "absolute",
            "as_posix",
            "as_uri",
            "chmod",
            "cwd",
            "exists",
            "expanduser",
            "glob",
            "group",
            "hardlink_to",
            "home",
            "is_absolute",
            "is_block_device",
            "is_char_device",
            "is_dir",
            "is_fifo",
            "is_file",
            "is_junction",
            "is_mount",
            "is_relative_to",
            "is_reserved",
            "is_socket",
            "is_symlink",
            "iterdir",
            "joinpath",
            "lchmod",
            "link_to",
            "lstat",
            "match",
            "mkdir",
            "open",
            "owner",
            "read_bytes",
            "read_text",
            "readlink",
            "relative_to",
            "rename",
            "replace",
            "resolve",
            "rglob",
            "rmdir",
            "samefile",
            "stat",
            "symlink_to",
            "touch",
            "unlink",
            "walk",
            "with_name",
            "with_segments",
            "with_stem",
            "with_suffix",
            "write_bytes",
            "write_text",
        }
    ),
    "extsym:pathlib.PurePath": frozenset(
        {
            "as_posix",
            "as_uri",
            "is_absolute",
            "is_relative_to",
            "is_reserved",
            "joinpath",
            "match",
            "relative_to",
            "with_name",
            "with_segments",
            "with_stem",
            "with_suffix",
        }
    ),
    "extsym:pathlib.PurePosixPath": frozenset(
        {
            "as_posix",
            "as_uri",
            "is_absolute",
            "is_relative_to",
            "is_reserved",
            "joinpath",
            "match",
            "relative_to",
            "with_name",
            "with_segments",
            "with_stem",
            "with_suffix",
        }
    ),
    "extsym:pathlib.PureWindowsPath": frozenset(
        {
            "as_posix",
            "as_uri",
            "is_absolute",
            "is_relative_to",
            "is_reserved",
            "joinpath",
            "match",
            "relative_to",
            "with_name",
            "with_segments",
            "with_stem",
            "with_suffix",
        }
    ),
    "extsym:pathlib.WindowsPath": frozenset(
        {
            "absolute",
            "as_posix",
            "as_uri",
            "chmod",
            "cwd",
            "exists",
            "expanduser",
            "glob",
            "group",
            "hardlink_to",
            "home",
            "is_absolute",
            "is_block_device",
            "is_char_device",
            "is_dir",
            "is_fifo",
            "is_file",
            "is_junction",
            "is_mount",
            "is_relative_to",
            "is_reserved",
            "is_socket",
            "is_symlink",
            "iterdir",
            "joinpath",
            "lchmod",
            "link_to",
            "lstat",
            "match",
            "mkdir",
            "open",
            "owner",
            "read_bytes",
            "read_text",
            "readlink",
            "relative_to",
            "rename",
            "replace",
            "resolve",
            "rglob",
            "rmdir",
            "samefile",
            "stat",
            "symlink_to",
            "touch",
            "unlink",
            "walk",
            "with_name",
            "with_segments",
            "with_stem",
            "with_suffix",
            "write_bytes",
            "write_text",
        }
    ),
    "extsym:subprocess.CompletedProcess": frozenset(
        {
            "check_returncode",
        }
    ),
}
# The external types a project class may inherit these methods from.
EXTERNAL_METHOD_RETURN_OWNERS = frozenset(
    owner.removeprefix("extsym:")
    for owner, _ in METHOD_RETURN_TYPES
    if owner.startswith("extsym:")
)
# Lowercase standard library classes, whose call constructs an instance. A
# capitalised external name and a builtins type are taken as classes already.
LOWERCASE_STDLIB_CLASSES = frozenset(
    {
        "collections.defaultdict",
        "collections.deque",
        "datetime.date",
        "datetime.datetime",
        "datetime.time",
        "datetime.timedelta",
        "datetime.timezone",
    }
)


def method_return_type(owner: str, method: str) -> dict[str, Any] | None:
    """The documented return of ``method`` on a receiver of type id ``owner``,
    as a type reference, or None."""

    returned = METHOD_RETURN_TYPES.get((owner, method))
    if returned is None:
        return None
    type_id, element = returned
    ref: dict[str, Any] = {
        "type_id": type_id,
        "type_expression": type_id.split(":", 1)[1],
    }
    if element is not None:
        ref["type_args"] = [
            {"type_id": element, "type_expression": element.split(":", 1)[1]}
        ]
        ref["type_expression"] += f"[{element.split(':', 1)[1]}]"
    return ref


def function_return_type(qualname: str) -> dict[str, Any] | None:
    """The documented return of the external function ``qualname``."""

    returned = FUNCTION_RETURN_TYPES.get(qualname)
    if returned is None:
        return None
    type_id, _ = returned
    return {"type_id": type_id, "type_expression": type_id.split(":", 1)[1]}


def type_id_of_qualname(qualname: str) -> str:
    """The type id the analyzers give the external or builtins type ``qualname``."""

    if qualname.startswith("builtins."):
        return "builtin:" + qualname.removeprefix("builtins.")
    return "extsym:" + qualname


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
_OTHER: _Value = ("other", "either")
# Comprehensions are never a str or a path.
_COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
# Operators that keep two ints an int; true division gives a float, and a
# power keeps an int only with a non-negative exponent (see _binary_value).
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
        literal = _int_literal(node)
        if literal is not None:
            # A signed or inverted int literal has a known value.
            return frozenset({("int", _truth(literal != 0))})
        operand = _values(node.operand, resolve)
        if isinstance(node.op, ast.Not):
            # A bool of the opposite truth.
            inverted = {"true": "false", "false": "true", "either": "either"}
            return frozenset(("int", inverted[truth]) for _, truth in operand)
        # -x, +x and ~x keep an int an int, and -x and +x keep its truth; no
        # str or path supports them.
        keeps_truth = isinstance(node.op, (ast.USub, ast.UAdd))
        return frozenset(
            ("int", truth if keeps_truth else "either") if kind == "int" else _OTHER
            for kind, truth in operand
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
    if left[0] == right[0] == "int":
        divisor = _int_literal(node.right)
        if isinstance(node.op, ast.Pow) and divisor is not None and divisor >= 0:
            # A non-negative literal exponent keeps an int an int.
            kind = "int"
        elif isinstance(node.op, (ast.FloorDiv, ast.Mod)) and divisor == 0:
            # ZeroDivisionError.
            kind = "other"
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
    value = _int_literal(node)
    return value is not None and value > 0


def _int_literal(node: ast.expr) -> int | None:
    """The value of an int or bool literal, with literal signs and inversions."""

    if isinstance(node, ast.UnaryOp) and isinstance(
        node.op, (ast.USub, ast.UAdd, ast.Invert)
    ):
        value = _int_literal(node.operand)
        if value is None:
            return None
        if isinstance(node.op, ast.Invert):
            return ~value
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return int(node.value)
    return None


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


def mapping_value_type(
    receiver: dict[str, Any] | None,
    method: str,
    default: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The type ``dict.get`` or ``dict.setdefault`` returns on ``receiver``.

    Both return the stored value when the key is present and the default
    otherwise, so with a default the result is typed only when the default is
    of the dict's value type; a dict of unknown value type gives a value of no
    type. Without a default, ``get`` is read as returning the value type.
    """

    if (
        not receiver
        or receiver.get("type_id") != "builtin:dict"
        or method not in {"get", "setdefault"}
    ):
        return None
    type_args = receiver.get("type_args")
    value_type = (
        type_args[1] if isinstance(type_args, list) and len(type_args) >= 2 else None
    )
    if not isinstance(value_type, dict) or not isinstance(
        value_type.get("type_id"), str
    ):
        return None
    if default is not None and default.get("type_id") != value_type.get("type_id"):
        return None
    return value_type


def mapping_default(node: ast.Call) -> ast.expr | None:
    """The default argument of a ``get`` or ``setdefault`` call, if any."""

    if len(node.args) >= 2:
        return node.args[1]
    for keyword in node.keywords:
        if keyword.arg == "default":
            return keyword.value
    return None
