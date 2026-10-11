"""Identify typing overload declarations without treating spelling as proof."""

from __future__ import annotations

import ast


def overload_declarations(tree: ast.Module) -> tuple[set[int], set[int]]:
    """Return proven declarations and those replaced by a direct implementation.

    Imports and intervening writes are read in execution order. Conditional
    imports of the decorator, star imports, alias chains and factories provide no
    proof. A family is collapsed only inside the same unconditional body.
    Cache by AST identity so symbol and binding passes share the decision.
    """
    cached = getattr(tree, "_arcgraph_overloads", None)
    if cached is not None:
        return cached
    declarations: set[int] = set()
    replaced: set[int] = set()

    def visit(body, inherited):
        aliases = dict(inherited)
        pending: dict[str, list[int]] = {}
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                proven = False
                for decorator in stmt.decorator_list:
                    if not isinstance(decorator, (ast.Name, ast.Attribute)):
                        continue
                    name = ast.unparse(decorator)
                    root, _, tail = name.partition(".")
                    qualified = aliases.get(root, "") + ("." + tail if tail else "")
                    if qualified in {"typing.overload", "typing_extensions.overload"}:
                        proven = True
                if proven:
                    declarations.add(id(stmt))
                    pending.setdefault(stmt.name, []).append(id(stmt))
                else:
                    replaced.update(pending.pop(stmt.name, []))
                aliases.pop(stmt.name, None)
                local = dict(aliases)
                for arg in [
                    *stmt.args.posonlyargs,
                    *stmt.args.args,
                    *stmt.args.kwonlyargs,
                ]:
                    local.pop(arg.arg, None)
                for arg in (stmt.args.vararg, stmt.args.kwarg):
                    if arg is not None:
                        local.pop(arg.arg, None)
                visit(stmt.body, local)
            elif isinstance(stmt, ast.ClassDef):
                visit(stmt.body, aliases)
                aliases.pop(stmt.name, None)
                pending.pop(stmt.name, None)
            elif isinstance(stmt, ast.Import):
                for item in stmt.names:
                    name = item.asname or item.name.split(".")[0]
                    aliases[name] = item.name if item.asname else name
                    pending.pop(name, None)
            elif isinstance(stmt, ast.ImportFrom):
                if any(item.name == "*" for item in stmt.names):
                    aliases.clear()
                    pending.clear()
                else:
                    for item in stmt.names:
                        name = item.asname or item.name
                        aliases[name] = (
                            f"{stmt.module}.{item.name}" if not stmt.level else ""
                        )
                        pending.pop(name, None)
            else:
                # An uncertain write is not allowed to preserve an import or
                # to turn conditional definitions into a direct overload family.
                for part in ast.walk(stmt):
                    if isinstance(part, ast.Name) and isinstance(
                        part.ctx, (ast.Store, ast.Del)
                    ):
                        aliases.pop(part.id, None)
                        pending.pop(part.id, None)
                    elif isinstance(part, ast.Attribute) and isinstance(
                        part.ctx, (ast.Store, ast.Del)
                    ):
                        root = ast.unparse(part).split(".")[0]
                        aliases.pop(root, None)
                    elif isinstance(
                        part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        aliases.pop(part.name, None)
                        pending.pop(part.name, None)
                    elif isinstance(part, (ast.Import, ast.ImportFrom)):
                        if any(item.name == "*" for item in part.names):
                            aliases.clear()
                            pending.clear()
                        else:
                            # A branch can replace its own imported names, not
                            # an unrelated unconditional decorator identity.
                            for item in part.names:
                                name = item.asname or (
                                    item.name.split(".")[0]
                                    if isinstance(part, ast.Import)
                                    else item.name
                                )
                                aliases.pop(name, None)
                                pending.pop(name, None)

    visit(tree.body, {})
    tree._arcgraph_overloads = (declarations, replaced)
    return declarations, replaced
