"""Small source contracts for known client dependencies; never import their code."""

from __future__ import annotations
import ast
import hashlib
import importlib.metadata
from pathlib import Path

TESTCLIENT_TYPES = {
    "extsym:starlette.testclient.TestClient",
    "extsym:fastapi.testclient.TestClient",
}


def mark_preferred_imports(body, bindings):
    """Only a typing import that agrees with the first guarded runtime import."""
    for stmt in body:
        if not isinstance(stmt, ast.If) or len(stmt.body) != 1 or len(stmt.orelse) != 1:
            continue
        check = ast.unparse(stmt.test)
        typing_import = stmt.body[0]
        runtime = stmt.orelse[0]
        if (
            not isinstance(typing_import, ast.Import)
            or len(typing_import.names) != 1
            or not isinstance(runtime, ast.Try)
            or len(runtime.body) != 1
        ):
            continue
        primary = runtime.body[0]
        if (
            not isinstance(primary, ast.Import)
            or len(primary.names) != 1
            or not runtime.handlers
        ):
            continue
        a, b = typing_import.names[0], primary.names[0]
        if (a.name, a.asname) != (b.name, b.asname) or a.name not in {
            "httpx",
            "httpx2",
        }:
            continue
        if any(
            ast.unparse(h.type) not in {"ImportError", "ModuleNotFoundError"}
            for h in runtime.handlers
            if h.type is not None
        ) or any(h.type is None for h in runtime.handlers):
            continue
        root = check.split(".")[0]
        guards = [row for row in bindings if row["name"] == root]
        expected = (
            "typing" if check.endswith(".TYPE_CHECKING") else "typing.TYPE_CHECKING"
        )
        if (
            len(guards) != 1
            or guards[0].get("kind") != "import_alias"
            or guards[0].get("value") != expected
            or guards[0].get("target_module")
            or any(
                row["name"] in {"ImportError", "ModuleNotFoundError"}
                for row in bindings
            )
        ):
            continue
        for row in bindings:
            if row.get("line") == primary.lineno and row.get("name") == (
                b.asname or b.name
            ):
                row["preferred_typing_runtime_import"] = True


def installed_testclient_contract(type_id):
    """Read installed source, including FastAPI's explicit re-export.

    Unsupported multiple bases, computed exports and missing source stay unknown.
    This records a source declaration, not a guarantee against runtime monkeypatches.
    """
    if type_id not in TESTCLIENT_TYPES:
        return None
    package = type_id.split(":", 1)[1].split(".")[0]
    try:
        distribution = importlib.metadata.distribution(package)
        path = Path(distribution.locate_file(package + "/testclient.py"))
        data = path.read_bytes()
        tree = ast.parse(data)
    except (importlib.metadata.PackageNotFoundError, OSError, SyntaxError):
        return None

    def assigned_at_module(name):
        return any(
            isinstance(n, ast.Name)
            and n.id == name
            and isinstance(n.ctx, (ast.Store, ast.Del))
            for stmt in tree.body
            if not isinstance(
                stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            for n in ast.walk(stmt)
        )

    if assigned_at_module("TestClient"):
        return None
    if package == "fastapi":
        exports = [
            n
            for n in tree.body
            if isinstance(n, ast.ImportFrom)
            and n.module == "starlette.testclient"
            and any(
                a.name == "TestClient" and a.asname in {None, "TestClient"}
                for a in n.names
            )
        ]
        if len(exports) != 1:
            return None
        return installed_testclient_contract("extsym:starlette.testclient.TestClient")
    classes = [
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TestClient"
    ]
    if len(classes) != 1 or classes[0].keywords or classes[0].decorator_list:
        return None
    cls = classes[0]
    aliases = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for a in stmt.names:
                aliases[a.asname or a.name] = a.name
        elif isinstance(stmt, ast.ImportFrom):
            for a in stmt.names:
                aliases[a.asname or a.name] = (stmt.module or "") + "." + a.name
    if len(cls.bases) != 1:
        return None
    base = ast.unparse(cls.bases[0])
    parts = base.split(".")
    if assigned_at_module(parts[0]):
        return None
    if parts[0] not in aliases:
        records = []
        pending = list(tree.body)
        while pending:
            stmt = pending.pop()
            if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(stmt, ast.Import):
                records.extend(
                    {
                        "name": a.asname or a.name,
                        "value": a.name,
                        "kind": "import_alias",
                        "line": stmt.lineno,
                    }
                    for a in stmt.names
                )
            elif isinstance(stmt, ast.ImportFrom):
                records.extend(
                    {
                        "name": a.asname or a.name,
                        "value": (stmt.module or "") + "." + a.name,
                        "kind": "import_alias",
                        "line": stmt.lineno,
                    }
                    for a in stmt.names
                )
            pending.extend(
                c
                for c in ast.iter_child_nodes(stmt)
                if isinstance(c, (ast.stmt, ast.ExceptHandler))
            )
        mark_preferred_imports(tree.body, records)
        preferred = [
            r["value"]
            for r in records
            if r["name"] == parts[0] and r.get("preferred_typing_runtime_import")
        ]
        candidates = {r["value"] for r in records if r["name"] == parts[0]}
        if len(preferred) != 1 or not candidates <= {"httpx", "httpx2"}:
            return None
        ordered = [preferred[0], *sorted(candidates - {preferred[0]})]
        for candidate in ordered:
            try:
                importlib.metadata.distribution(candidate)
            except importlib.metadata.PackageNotFoundError:
                continue
            aliases[parts[0]] = candidate
            break
        else:
            return None
    base = ".".join([aliases[parts[0]], *parts[1:]])
    if base not in {"httpx.Client", "httpx2.Client"}:
        return None
    methods = {
        n.name
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not n.decorator_list
    }
    blocked = {
        n.id
        for stmt in cls.body
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
        for n in ast.walk(stmt)
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))
    } | {
        stmt.name
        for stmt in cls.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
        and stmt.decorator_list
    }
    return {
        "base": base,
        "methods": methods,
        "blocked": blocked,
        "version": distribution.version,
        "source_sha256": hashlib.sha256(data).hexdigest(),
    }
