"""Small, persisted annotation contracts for invoking callable values.

These contracts are type evidence, never runtime definitions or call targets.
Only direct aliases and single non-generic Protocol signatures are supported.
"""

from __future__ import annotations

import ast
from typing import Any

from arcgraph.analyzers.calls.lexical import LexicalScopes, import_binding_target
from arcgraph.core.schemas import Node
from arcgraph.analyzers.python_scopes import yields

_CALLABLES = {"typing.Callable", "collections.abc.Callable"}
_PROTOCOLS = {"typing.Protocol", "typing_extensions.Protocol"}
_BUILTINS = {
    "str",
    "bytes",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "set",
    "tuple",
    "object",
}


def collect_contracts(module: Node, tree: ast.Module, nodes: list[Node]) -> None:
    """Persist source summaries, including proven typing-only Protocols."""
    module.properties["callable_annotation_member_writes"] = sorted(
        {
            ast.unparse(n)
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del))
        }
    )
    resolver = CallableContracts(nodes)
    contracts: dict[str, dict[str, Any]] = {}
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if (
                len(targets) == 1
                and isinstance(targets[0], ast.Name)
                and isinstance(stmt.value, (ast.Name, ast.Attribute, ast.Subscript))
            ):
                contracts[targets[0].id] = {"expression": ast.unparse(stmt.value)}
        classes = (
            [(stmt, False, [stmt.lineno])] if isinstance(stmt, ast.ClassDef) else []
        )
        if (
            isinstance(stmt, ast.If)
            and resolver.name(stmt.test, module) == "typing.TYPE_CHECKING"
        ):
            for child in stmt.body:
                if isinstance(child, ast.ClassDef) and (
                    not stmt.orelse
                    or all(
                        isinstance(other, ast.ClassDef) and other.name == child.name
                        for other in stmt.orelse
                    )
                ):
                    classes.append(
                        (
                            child,
                            True,
                            [child.lineno, *[other.lineno for other in stmt.orelse]],
                        )
                    )
        for child, typing_only, lines in classes:
            if (
                len(child.bases) != 1
                or resolver.name(child.bases[0], module) not in _PROTOCOLS
            ):
                continue
            calls = [
                m
                for m in child.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and m.name == "__call__"
            ]
            if len(calls) != 1 or not isinstance(calls[0], ast.FunctionDef):
                continue
            call = calls[0]
            if (
                child.decorator_list
                or call.decorator_list
                or call.returns is None
                or yields(call)
                or getattr(child, "type_params", [])
                or any(
                    resolver.generic(a.annotation, module)
                    for a in [
                        *call.args.posonlyargs,
                        *call.args.args,
                        *call.args.kwonlyargs,
                        *([call.args.vararg] if call.args.vararg else []),
                        *([call.args.kwarg] if call.args.kwarg else []),
                    ]
                    if a.annotation is not None
                )
            ):
                continue
            contracts[child.name] = {
                "expression": ast.unparse(call.returns),
                "protocol": True,
                "typing_only": typing_only,
                "declaration_lines": lines,
            }
    module.properties["callable_annotation_contracts"] = contracts


class CallableContracts:
    def __init__(self, nodes: list[Node], module_names: set[str] | None = None):
        self.modules = {n.path: n for n in nodes if n.kind == "module"}
        self.by_name = {n.qualname: n for n in self.modules.values()}
        self.nodes = nodes
        self.module_names = module_names or set()
        self._scopes: dict[str | None, LexicalScopes] = {}
        self._results: dict[tuple[str | None, str, str], str | None] = {}

    def scopes(self, source: Node) -> LexicalScopes:
        if source.path not in self._scopes:
            self._scopes[source.path] = LexicalScopes(
                [n for n in self.nodes if n.path == source.path], {}
            )
        return self._scopes[source.path]

    def name(self, expr: ast.AST, source: Node) -> str | None:
        text = ast.unparse(expr)
        if not isinstance(expr, (ast.Name, ast.Attribute)):
            return None
        root, *tail = text.split(".")
        scopes = self.scopes(source)
        found = scopes.lookup(source, root)
        if not found:
            return root if not tail and root in _BUILTINS else None
        owner, bindings, _ = found
        if (owner.path, root) in scopes.global_writes:
            return None
        module = self.modules.get(source.path)
        if module and text in module.properties.get(
            "callable_annotation_member_writes", []
        ):
            return None
        contract = owner.properties.get("callable_annotation_contracts", {}).get(root)
        # Two proven branches of TYPE_CHECKING can define the same name.
        # This exception is only for interpreting its annotation contract.
        typing_protocol = (
            contract
            and contract.get("typing_only")
            and all(b.get("kind") == "class_definition" for b in bindings)
            and sum(bool(b.get("static_only")) for b in bindings) == 1
            and sorted(b.get("line") for b in bindings)
            == sorted(contract["declaration_lines"])
        )
        if not typing_protocol and not scopes.stable(*found):
            return None
        binding = bindings[0]
        if binding.get("kind") == "import_alias":
            target = import_binding_target(binding)
            if target and not any(
                target == m or target.startswith(m + ".")
                for m in self.module_names
                if m in {"typing", "typing_extensions", "collections"}
            ):
                return ".".join([target, *tail])
        if owner.kind == "module" and (
            contract or binding.get("kind") == "class_definition"
        ):
            return ".".join([str(owner.qualname), root, *tail])
        return None

    def result(
        self, expression: str, source: Node, seen: frozenset[str] = frozenset()
    ) -> str | None:
        key = (source.path, source.id, expression)
        if seen:
            return self._result(expression, source, seen)
        if key not in self._results:
            self._results[key] = self._result(expression, source, seen)
        return self._results[key]

    def _result(
        self, expression: str, source: Node, seen: frozenset[str] = frozenset()
    ) -> str | None:
        """Return a canonical concrete result annotation, or unknown.

        ParamSpec/TypeVar parameter lists, unions, overloads, generic protocols,
        dynamic aliases and unproven annotation origins are deliberately unknown.
        """
        try:
            expr = ast.parse(expression, mode="eval").body
        except SyntaxError:
            return None
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return self.result(expr.value, source, seen)
        if isinstance(expr, ast.Subscript):
            if (
                self.name(expr.value, source) not in _CALLABLES
                or not isinstance(expr.slice, ast.Tuple)
                or len(expr.slice.elts) != 2
            ):
                return None
            params, result = expr.slice.elts
            if not (
                isinstance(params, ast.Constant) and params.value is Ellipsis
            ) and not (
                isinstance(params, ast.List)
                and all(self.concrete(p, source) for p in params.elts)
            ):
                return None
            return self.concrete(result, source)
        qualified = self.name(expr, source)
        if not qualified or qualified in seen or "." not in qualified:
            return None
        module_name, name = qualified.rsplit(".", 1)
        module = self.by_name.get(module_name)
        if module is None:
            return None
        contract = module.properties.get("callable_annotation_contracts", {}).get(name)
        if not contract:
            return None
        # Recheck aliases in their defining module, not in the importing scope.
        if self.name(ast.Name(id=name), module) != qualified:
            return None
        if contract.get("protocol"):
            return self.concrete(
                ast.parse(contract["expression"], mode="eval").body, module
            )
        return self.result(contract["expression"], module, seen | {qualified})

    def generic(self, annotation: ast.AST, source: Node) -> bool:
        for part in ast.walk(annotation):
            if not isinstance(part, ast.Name):
                continue
            found = self.scopes(source).lookup(source, part.id)
            if not found:
                continue
            for binding in found[1]:
                try:
                    value = ast.parse(str(binding.get("value", "")), mode="eval").body
                except SyntaxError:
                    continue
                if isinstance(value, ast.Call) and self.name(value.func, source) in {
                    "typing.TypeVar",
                    "typing.ParamSpec",
                    "typing.TypeVarTuple",
                    "typing_extensions.TypeVar",
                    "typing_extensions.ParamSpec",
                    "typing_extensions.TypeVarTuple",
                }:
                    return True
        return False

    def concrete(self, expr: ast.AST, source: Node) -> str | None:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            try:
                return self.concrete(ast.parse(expr.value, mode="eval").body, source)
            except SyntaxError:
                return None
        name = self.name(expr, source)
        if not name or name.startswith(("typing.", "typing_extensions.")):
            return None
        # Reject local non-class values (TypeVar, ParamSpec, aliases to unknown)
        # and generic result expressions; their substitution is outside this rule.
        if "." in name:
            module_name, member = name.rsplit(".", 1)
            module = self.by_name.get(module_name)
            if module is not None:
                bindings = (
                    self.scopes(module).bindings.get(module.id, {}).get(member, [])
                )
                if len(bindings) != 1 or bindings[0].get("kind") != "class_definition":
                    return None
        return name
