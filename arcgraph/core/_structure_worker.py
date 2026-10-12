"""Standalone stdlib-only AST reader, launched with -I -S.

Protocol 1 uses marshal's primitive values (never pickle or executable objects).
A node is (type-table index, field values, location values); lists stay lists.
The type table carries name, fields, attributes and the AST base category, so
the receiving Python need not know newly introduced syntax classes.
"""

import ast
import marshal
from pathlib import Path
import sys
import warnings


def main():
    # Only a primitive path map is accepted; input is never executed.
    request = marshal.load(sys.stdin.buffer)  # nosec B302
    types = []
    indices = {}

    def encode(value):
        if isinstance(value, ast.AST):
            cls = type(value)
            if cls not in indices:
                indices[cls] = len(types)
                base = next(
                    (b.__name__ for b in cls.__bases__ if issubclass(b, ast.AST)),
                    "AST",
                )
                types.append((cls.__name__, cls._fields, cls._attributes, base))
            return (
                indices[cls],
                tuple(encode(getattr(value, f, None)) for f in cls._fields),
                tuple(getattr(value, a, None) for a in cls._attributes),
            )
        if isinstance(value, list):
            return [encode(v) for v in value]
        return value

    records = {}
    for name, path in request.items():
        try:
            source = Path(path).read_text(encoding="utf-8-sig")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(source, filename=name)
            records[name] = (encode(tree), None)
        except (OSError, SyntaxError, UnicodeError, RecursionError) as exc:
            message = str(exc)
            if isinstance(exc, SyntaxError):
                message += (
                    " (parsed with Python %s.%s; syntax added in a later Python "
                    "needs a project interpreter on that version)"
                    % sys.version_info[:2]
                )
            records[name] = (None, message)
    marshal.dump((1, sys.version.split()[0], types, records), sys.stdout.buffer, 4)


if __name__ == "__main__":
    main()
