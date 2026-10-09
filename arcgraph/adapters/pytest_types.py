"""Bounded fixture result propagation, without running pytest or project code."""

from __future__ import annotations

import ast

from arcgraph.adapters.common import call_name, iter_symbol_defs, symbol_id_for_def
from arcgraph.adapters.pytest_fixtures import FixtureCatalog, FixtureFact
from arcgraph.analyzers.calls.lexical import LexicalScopes
from arcgraph.analyzers.types import (
    TypeRefAnalysis,
    TypeRefAnalyzer,
    _TypeContext,
    _ResolvedType,
)
from arcgraph.core.schemas import FileRecord, Node


class PytestTypeAnalyzer(TypeRefAnalyzer):
    def attach(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
        return_decorator_cache: dict[str, bool] | None = None,
    ) -> None:
        if return_decorator_cache is None:
            return_decorator_cache = {}
        catalog = FixtureCatalog(files, parsed_files, module_names, nodes)
        by_id = {n.id: n for n in nodes}
        provider_paths = {f.file.path for f in catalog.fixtures}
        contexts = {
            f.path: _TypeContext(
                f, parsed_files[f.path], nodes, module_names, return_decorator_cache
            )
            for f in files
            if f.path in provider_paths
        }
        analysis = TypeRefAnalysis()
        for file in files:
            tree = parsed_files.get(file.path)
            if tree is None:
                continue
            for stmt, qualname, factory in iter_symbol_defs(tree):
                if not catalog.collection.is_test(
                    file, stmt, qualname.rpartition(".")[0]
                ):
                    continue
                node = by_id.get(symbol_id_for_def(file, qualname, factory))
                if node is None:
                    continue
                owner = qualname.rpartition(".")[0]
                parameters = catalog.parameters(stmt, owner)
                direct = catalog.direct_parameters(stmt, file, owner)
                for binding in node.properties.get("bindings", []):
                    name = binding.get("name")
                    if binding.get("kind") != "parameter" or name not in parameters:
                        continue
                    # Unknown inputs must not gain a receiver type by name heuristics.
                    binding["pytest_parameter"] = True
                    fixture = (
                        None if name in direct else catalog.resolve(name, file, owner)
                    )
                    if fixture is not None:
                        binding["pytest_fixture"] = fixture.node_id
                    if binding.get("annotation"):
                        continue
                    builtin = (
                        None
                        if name in direct
                        else catalog.builtin_type(name, file, owner)
                    )
                    source = (
                        self.fixture_result(fixture, by_id, contexts[fixture.file.path])
                        if fixture is not None
                        else (
                            _ResolvedType(
                                expression=builtin,
                                type_id=f"extsym:{builtin}",
                                symbol_id=f"extsym:{builtin}",
                                status="resolved",
                            )
                            if builtin
                            else None
                        )
                    )
                    if source is None or not source.type_id:
                        continue
                    if file.path not in contexts:
                        contexts[file.path] = _TypeContext(
                            file, tree, nodes, module_names, return_decorator_cache
                        )
                    ref = self._type_ref_record(
                        contexts[file.path],
                        scope_id=node.id,
                        scope_kind=node.kind,
                        name=name,
                        subject_kind="binding",
                        strategy=(
                            "pytest_fixture_result"
                            if fixture
                            else "pytest_builtin_fixture"
                        ),
                        confidence="inferred",
                        source_expression=(
                            fixture.handler_id if fixture else f"pytest:{name}"
                        ),
                        resolved=source,
                        line=binding.get("line"),
                        end_line=binding.get("line"),
                        column=binding.get("column"),
                        binding_id=binding.get("binding_id"),
                    )
                    ref["fixture_id"] = (
                        fixture.node_id if fixture else f"pytest_builtin:{name}"
                    )
                    ref["provider_evidence"] = (
                        {
                            "path": fixture.file.path,
                            "start_line": fixture.stmt.lineno,
                            "end_line": fixture.stmt.end_lineno,
                        }
                        if fixture
                        else {"builtin": name, "type": builtin}
                    )
                    analysis.type_refs_by_scope.setdefault(node.id, []).append(ref)
                    analysis.type_refs_by_binding[binding["binding_id"]] = ref
        analysis.attach_to_nodes(nodes)
        # Plain aliases of an unknown fixture input must not escape the receiver
        # guard and acquire a guessed type. General alias inference is separate.
        lexical = LexicalScopes(nodes, {})
        changed = True
        while changed:
            changed = False
            for node in nodes:
                for binding in node.properties.get("bindings", []):
                    value = binding.get("value")
                    if (
                        binding.get("pytest_parameter")
                        or not isinstance(value, str)
                        or not value.isidentifier()
                    ):
                        continue
                    found = lexical.lookup(node, value)
                    if found and any(b.get("pytest_parameter") for b in found[1]):
                        binding["pytest_parameter"] = True
                        changed = True

    def fixture_result(
        self, fixture: FixtureFact, nodes: dict[str, Node], context: _TypeContext
    ):
        stmt = fixture.stmt
        node = nodes.get(fixture.handler_id)
        if node is None or node.properties.get("ambiguous_definition"):
            return None
        if isinstance(stmt, ast.AsyncFunctionDef) and not fixture.async_enabled:
            return None
        # Additional decorators can replace the callable or transform its value.
        if len(stmt.decorator_list) != 1:
            return None
        # Accept straight-line bodies only. Never harvest returns from nested scopes,
        # conditional exits, loops, context managers, or exception handlers.
        results = []
        for item in stmt.body:
            if any(kind == "return" for kind, _ in results):
                return None
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(item, ast.Return):
                results.append(("return", item.value))
            elif isinstance(item, ast.Expr) and isinstance(item.value, ast.Yield):
                results.append(("yield", item.value.value))
            elif not isinstance(
                item,
                (
                    ast.Assign,
                    ast.AnnAssign,
                    ast.Expr,
                    ast.Pass,
                    ast.Import,
                    ast.ImportFrom,
                ),
            ):
                return None
            elif any(
                isinstance(child, (ast.Yield, ast.YieldFrom, ast.NamedExpr))
                for child in ast.walk(item)
            ):
                return None
        values = [value for kind, value in results if kind == "yield"]
        if values:
            if len(values) != 1 or any(
                kind == "return" and value is not None for kind, value in results
            ):
                return None
        else:
            if not stmt.body or not isinstance(stmt.body[-1], ast.Return):
                return None
            values = [value for _, value in results]
        if len(values) != 1 or values[0] is None:
            return None
        # A direct annotation describes a normal fixture; generator annotations
        # describe the iterator instead, so infer the yielded expression below.
        if stmt.returns is not None and not any(kind == "yield" for kind, _ in results):
            return context.resolve_annotation(
                ast.unparse(stmt.returns), use_imports=True
            )
        return self.constructor_result(values[0], node, context, set())

    def constructor_result(
        self, value: ast.AST, node: Node, context: _TypeContext, seen: set[str]
    ):
        if isinstance(value, ast.Name):
            if value.id in seen:
                return None
            found = context.lexical.lookup(node, value.id)
            if not found or not context.lexical.stable(*found):
                return None
            binding = found[1][0]
            if found[0].id != node.id or binding.get("kind") not in {
                "assignment",
                "annotated_assignment",
            }:
                return None
            if binding.get("line", 0) >= getattr(value, "lineno", 0):
                return None
            parsed = context.parse_expression(str(binding.get("value", "")))
            return (
                self.constructor_result(parsed, node, context, seen | {value.id})
                if parsed is not None
                else None
            )
        if not isinstance(value, ast.Call):
            return None
        name = call_name(value.func)
        root = context.lexical.root_name(name)
        found = context.lexical.lookup(node, root) if root else None
        if not found or not context.lexical.stable(*found):
            return None
        binding = found[1][0]
        if binding.get("kind") not in {"class_definition", "import_alias"}:
            return None
        if found[0].id == node.id and binding.get("line", 0) >= getattr(
            value, "lineno", 0
        ):
            return None
        resolved = context.resolve_annotation(name, use_imports=True)
        if not resolved.type_id or not resolved.type_id.startswith("class:"):
            return None
        if (
            binding.get("kind") == "class_definition"
            and binding.get("target") != resolved.type_id
        ):
            return None
        return resolved
