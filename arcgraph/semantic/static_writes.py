"""Explicit member writes with lexically resolvable class/module receivers."""

import ast

from .external_targets import target_condition


class Scope:
    def __init__(self, parent=None):
        self.parent = parent
        self.bindings = {}

    def bind(self, name, value):
        # Multiple bindings do not establish a simple, static alias chain.
        self.bindings[name] = value if name not in self.bindings else None

    def reference(self, node, seen=()):
        if len(seen) >= 64:
            return None
        if isinstance(node, ast.Attribute):
            owner = self.reference(node.value, seen)
            return owner + "." + node.attr if owner else None
        if not isinstance(node, ast.Name):
            return None
        key = (id(self), node.id)
        if key in seen:
            return None
        if node.id not in self.bindings:
            if self.parent is not None:
                return self.parent.reference(node, (*seen, key))
            return "builtins." + node.id if node.id in ("setattr", "delattr") else None
        value = self.bindings[node.id]
        return (
            self.reference(value, (*seen, key)) if isinstance(value, ast.AST) else value
        )


def declaration_expressions(node):
    expressions = list(node.decorator_list)
    if isinstance(node, ast.ClassDef):
        expressions.extend(node.bases)
        expressions.extend(keyword.value for keyword in node.keywords)
    else:
        args = node.args
        expressions.extend(args.defaults)
        expressions.extend(value for value in args.kw_defaults if value is not None)
        expressions.extend(
            arg.annotation
            for arg in [
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                args.vararg,
                args.kwarg,
            ]
            if arg is not None and arg.annotation is not None
        )
        if node.returns is not None:
            expressions.append(node.returns)
    return expressions


def suite_nodes(body, snapshot=None):
    """Visit one namespace, including control suites but excluding nested bodies."""
    pending = list(reversed(body))
    while pending:
        node = pending.pop()
        if node is None:
            continue
        yield node
        if snapshot is not None and isinstance(node, ast.If):
            condition = target_condition(node.test, snapshot)
            if condition is not None:
                pending.extend(
                    reversed([node.test, *(node.body if condition else node.orelse)])
                )
                continue
        if isinstance(node, ast.comprehension):
            pending.extend(reversed([node.iter, *node.ifs]))
        elif isinstance(node, ast.Lambda):
            pending.extend(reversed([*node.args.defaults, *node.args.kw_defaults]))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            pending.extend(reversed(declaration_expressions(node)))
        else:
            pending.extend(reversed(list(ast.iter_child_nodes(node))))


def member_writes(tree, module, import_from, snapshot, scopes):
    """Ignore runtime effects; return only explicit, statically named writes.

    References describe lexical imports, definitions and simple aliases. A call
    result, parameter, ambiguous binding or namespace subscript is not evidence
    that a receiver denotes a class or module. The proof resolves references in
    its frozen search view before comparing them with the selected owner.
    """
    writes = []
    pending = [(tree, module, None, None)]
    while pending:
        owner, qualified, parent, function_parent = pending.pop()
        scope = Scope(parent)
        nodes = [n for n in suite_nodes(owner.body) if n is not None]
        declarations = {
            name
            for node in nodes
            if isinstance(node, (ast.Global, ast.Nonlocal))
            for name in node.names
        }
        for static_pass in (False, True):
            if static_pass:
                if scope.reference(ast.Name(id="sys")) != "sys":
                    break
                # Rebuild bindings from the same statically selected suites as
                # the proof; dead imports must not hide a known receiver.
                nodes = list(suite_nodes(owner.body, snapshot))
                scope = Scope(parent)
            for node in nodes:
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    scope.bind(node.name, qualified + "." + node.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        scope.bind(
                            alias.asname or alias.name.split(".")[0],
                            alias.name if alias.asname else alias.name.split(".")[0],
                        )
                elif isinstance(node, ast.ImportFrom):
                    origin = import_from(module, node)
                    for alias in node.names:
                        if alias.name == "*":
                            scope.bind("setattr", None)
                            scope.bind("delattr", None)
                        else:
                            scope.bind(
                                alias.asname or alias.name, origin + "." + alias.name
                            )
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    for target in targets:
                        if isinstance(target, ast.Name):
                            scope.bind(target.id, node.value)
                elif isinstance(node, ast.Name) and isinstance(
                    node.ctx, (ast.Store, ast.Del)
                ):
                    # Assignment names were handled with their right-hand sides.
                    continue
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    scope.bind(node.name, None)
                elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                    scope.bind(node.name, None)
                elif isinstance(node, ast.MatchMapping) and node.rest:
                    scope.bind(node.rest, None)
            assigned = {
                id(target)
                for node in nodes
                if isinstance(node, (ast.Assign, ast.AnnAssign))
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if isinstance(target, ast.Name)
            }
            for node in nodes:
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, (ast.Store, ast.Del))
                    and id(node) not in assigned
                ):
                    scope.bind(node.id, None)
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = owner.args
                for arg in [
                    *args.posonlyargs,
                    *args.args,
                    *args.kwonlyargs,
                    args.vararg,
                    args.kwarg,
                ]:
                    if arg is not None:
                        scope.bind(arg.arg, None)
            for name in declarations:
                # A write in a nested global/nonlocal scope is checked separately
                # as a namespace rebind. It cannot establish a simple local alias.
                scope.bindings.pop(name, None)

        if owner is tree:
            scopes[module] = scope

        active = [(node, scope) for node in reversed(owner.body)]
        while active:
            node, current = active.pop()
            if isinstance(
                node,
                (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Lambda),
            ):
                local = Scope(current)
                if isinstance(node, ast.Lambda):
                    args = node.args
                    names = [
                        arg.arg
                        for arg in [
                            *args.posonlyargs,
                            *args.args,
                            *args.kwonlyargs,
                            args.vararg,
                            args.kwarg,
                        ]
                        if arg is not None
                    ]
                    expressions = [node.body]
                    active.extend(
                        (value, current)
                        for value in [*args.defaults, *args.kw_defaults]
                        if value is not None
                    )
                else:
                    names = [
                        value.id
                        for generator in node.generators
                        for value in ast.walk(generator.target)
                        if isinstance(value, ast.Name)
                    ]
                    expressions = (
                        [node.key, node.value]
                        if isinstance(node, ast.DictComp)
                        else [node.elt]
                    )
                    expressions.extend(
                        value
                        for generator in node.generators
                        for value in generator.ifs
                    )
                    active.extend(
                        (generator.iter, current if index == 0 else local)
                        for index, generator in enumerate(node.generators)
                    )
                for name in names:
                    local.bind(name, None)
                active.extend((value, local) for value in expressions)
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                is_class = isinstance(node, ast.ClassDef)
                closure = (
                    scope if not isinstance(owner, ast.ClassDef) else function_parent
                )
                pending.append(
                    (
                        node,
                        qualified + "." + node.name,
                        scope if is_class else closure,
                        closure,
                    )
                )
                # These expressions are in the enclosing namespace. Inspect
                # explicit writes in them without modeling decorator/metaclass
                # effects or executing defaults.
                active.extend(
                    (value, current) for value in declaration_expressions(node)
                )
                continue
            if (
                isinstance(node, ast.If)
                and current.reference(ast.Name(id="sys")) == "sys"
            ):
                condition = target_condition(node.test, snapshot)
                if condition is not None:
                    active.extend(
                        (child, current)
                        for child in reversed(node.body if condition else node.orelse)
                    )
                    continue
            if isinstance(node, ast.Attribute) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                receiver, name = current.reference(node.value), node.attr
            elif (
                isinstance(node, ast.Call)
                and current.reference(node.func) is not None
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
            ):
                receiver, name = current.reference(node.args[0]), node.args[1].value
            else:
                receiver, name = None, None
            if (
                receiver is not None
                and isinstance(name, str)
                and name not in ("__bases__", "__class__")
            ):
                callee = (
                    current.reference(node.func) if isinstance(node, ast.Call) else None
                )
                writes.append((receiver, name, node.lineno, callee))
            active.extend(
                (child, current) for child in reversed(list(ast.iter_child_nodes(node)))
            )
    return tuple(sorted(set(writes), key=lambda row: row[2]))
