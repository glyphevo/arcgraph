"""Bounded source proofs for FastAPI routers and their mount prefixes.

No application or dependency code is executed. Unknown/mutated prefixes retain
local paths. Router subclasses are accepted only when their constructor forwards
the prefix unchanged to a proven APIRouter base.
"""

from __future__ import annotations

import ast

from arcgraph.adapters.common import call_name, keyword, resolve_alias


def constant_text(node, tree):
    """Literal strings, stable module constants, concatenation and f-strings."""
    assignments = {}
    writes = {}
    for part in ast.walk(tree):
        if isinstance(part, ast.Name) and isinstance(part.ctx, (ast.Store, ast.Del)):
            writes[part.id] = writes.get(part.id, 0) + 1
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                if isinstance(target, ast.Name) and writes.get(target.id) == 1:
                    assignments[target.id] = stmt.value

    def resolve(value, seen):
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        if isinstance(value, ast.Name) and value.id not in seen:
            return resolve(assignments.get(value.id), seen | {value.id})
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            left, right = resolve(value.left, seen), resolve(value.right, seen)
            return left + right if left is not None and right is not None else None
        if isinstance(value, ast.JoinedStr):
            parts = []
            for part in value.values:
                if isinstance(part, ast.FormattedValue):
                    if part.conversion != -1 or part.format_spec:
                        return None
                    part = part.value
                text = resolve(part, seen)
                if text is None:
                    return None
                parts.append(text)
            return "".join(parts)
        return None

    return resolve(node, set())


def router_factories(contexts):
    known = {
        "fastapi.APIRouter": "router",
        "fastapi.FastAPI": "app",
        "fastapi.routing.APIRouter": "router",
        "fastapi.applications.FastAPI": "app",
    }
    classes = {}
    for file, tree, imports in contexts:
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                classes[f"{file.module}.{stmt.name}"] = (stmt, file, imports, tree)
    changed = True
    while changed:
        changed = False
        for name, (cls, file, imports, tree) in classes.items():
            if name in known or len(cls.bases) != 1:
                continue
            if imported_factory_shadowed(tree, cls.bases[0], imports):
                continue
            base = qualified(call_name(cls.bases[0]), file, imports)
            if known.get(base) != "router":
                continue
            init = next(
                (
                    s
                    for s in cls.body
                    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and s.name == "__init__"
                ),
                None,
            )
            if init is not None:
                defaults = (
                    dict(
                        zip(
                            [a.arg for a in [*init.args.posonlyargs, *init.args.args]][
                                -len(init.args.defaults) :
                            ],
                            init.args.defaults,
                        )
                    )
                    if init.args.defaults
                    else {}
                )
                default = defaults.get("prefix")
                if not isinstance(default, ast.Constant) or default.value != "":
                    continue
                if "super" in imports.aliases or any(
                    (isinstance(n, ast.arg) and n.arg == "super")
                    or (
                        isinstance(n, ast.Name)
                        and n.id == "super"
                        and isinstance(n.ctx, (ast.Store, ast.Del))
                    )
                    for n in ast.walk(tree)
                ):
                    continue
                forwarding = [
                    c
                    for c in ast.walk(init)
                    if isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "__init__"
                    and isinstance(c.func.value, ast.Call)
                    and call_name(c.func.value.func) == "super"
                ]
                if (
                    len(forwarding) != 1
                    or not isinstance(keyword(forwarding[0], "prefix"), ast.Name)
                    or keyword(forwarding[0], "prefix").id != "prefix"
                ):
                    continue
                if any(
                    isinstance(n, ast.Name)
                    and n.id == "prefix"
                    and isinstance(n.ctx, ast.Store)
                    for n in ast.walk(init)
                ):
                    continue
            known[name] = "router"
            changed = True
    return known


def qualified(name, file, imports):
    if not name:
        return None
    result = resolve_alias(name, imports)
    if (
        result == name
        and name.split(".")[0] not in imports.modules
        and name.split(".")[0] not in imports.aliases
    ):
        return f"{file.module}.{name}"
    return result


def stable_receiver(tree, name):
    """A single module assignment, not a rebind/import with the same name."""
    writes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Name)
        and n.id == name
        and isinstance(n.ctx, (ast.Store, ast.Del))
    ]
    imported = any(
        a.asname == name or (a.asname is None and a.name.split(".")[0] == name)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names
    )
    return len(writes) == 1 and not imported


def receiver_shadowed(cls, name):
    # Decorators execute in the class namespace; method body locals do not.
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if stmt.name == name:
                return True
            continue
        if any(
            isinstance(n, ast.Name)
            and n.id == name
            and isinstance(n.ctx, (ast.Store, ast.Del))
            for n in ast.walk(stmt)
        ):
            return True
    return False


def prefix_mutated(tree, name):
    return any(
        isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == name
        and n.attr == "prefix"
        and isinstance(n.ctx, (ast.Store, ast.Del))
        for n in ast.walk(tree)
    )


def include_calls(tree, receivers):
    """Module calls and direct calls in unshadowed startup functions.

    Conditional mounts and arbitrary loop/dataflow registration are not treated
    as unconditional mounts. Their child routes remain local, marked unknown.
    """
    executed = {
        c.func.id
        for stmt in tree.body
        if isinstance(stmt, ast.Expr)
        for c in [stmt.value]
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    for stmt in tree.body:
        bodies = [stmt]
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.name not in executed:
                continue
            bound = {
                n.id
                for n in ast.walk(stmt)
                if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))
            }
            bound.update(
                a.arg
                for a in [
                    *stmt.args.posonlyargs,
                    *stmt.args.args,
                    *stmt.args.kwonlyargs,
                ]
            )
            if bound & {r.split(".")[0] for r in receivers}:
                continue
            bodies = stmt.body
        for part in bodies:
            if isinstance(part, ast.Expr) and isinstance(part.value, ast.Call):
                call = part.value
                if (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "include_router"
                    and call_name(call.func.value) in receivers
                    and call.args
                ):
                    yield call


def imported_factory_shadowed(tree, factory, imports):
    root = (call_name(factory) or "").split(".")[0]
    if root not in imports.aliases:
        return False
    return any(
        (
            isinstance(n, ast.Name)
            and n.id == root
            and isinstance(n.ctx, (ast.Store, ast.Del))
        )
        or (
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.name == root
        )
        for n in ast.walk(tree)
    )
