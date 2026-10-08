"""Scope and binding summaries for Python AST files."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Iterable

from arcgraph.analyzers.python_scopes import (
    local_definition_regions,
    nested_functions,
    ModuleFlow,
    module_flow,
)
from arcgraph.analyzers.imports import ImportAnalyzer
from arcgraph.core.ids import binding_id, class_id, function_id, method_id, module_id
from arcgraph.core.schemas import Evidence, FileRecord, Node

_ForNode = ast.For | ast.AsyncFor
_WithNode = ast.With | ast.AsyncWith


@dataclass
class BindingAnalysis:
    export_mutations_by_scope: set[str] = field(default_factory=set)
    comprehension_writes: dict[str, set[str]] = field(default_factory=dict)
    bindings_by_scope: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    diagnostics_by_scope: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    comprehension_contexts: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict
    )

    def attach_to_nodes(self, nodes: list[Node]) -> None:
        by_id = {node.id: node for node in nodes}
        for scope_id in self.export_mutations_by_scope:
            if scope_id in by_id:
                by_id[scope_id].properties["mutates_exports"] = True
        for scope_id, contexts in self.comprehension_contexts.items():
            if scope_id in by_id:
                by_id[scope_id].properties["comprehension_contexts"] = contexts
                by_id[scope_id].properties["comprehension_type_input"] = True
                by_id[scope_id].properties["comprehension_writes"] = sorted(
                    self.comprehension_writes.get(scope_id, set())
                )
        for scope_id, bindings in self.bindings_by_scope.items():
            node = by_id.get(scope_id)
            if node is not None and bindings:
                node.properties["bindings"] = bindings
        for scope_id, diagnostics in self.diagnostics_by_scope.items():
            node = by_id.get(scope_id)
            if node is not None and diagnostics:
                node.properties["binding_diagnostics"] = diagnostics


class BindingAnalyzer:
    """Collect lightweight V2.1 binding facts without changing call resolution."""

    def analyze(
        self,
        file_record: FileRecord,
        tree: ast.Module,
        module_names: set[str],
    ) -> BindingAnalysis:
        analysis = BindingAnalysis()
        _BindingScopeVisitor(
            self,
            analysis=analysis,
            file_record=file_record,
            scope_id=module_id(file_record.module),
            scope_kind="module",
            module_names=module_names,
        ).visit_module_body(tree.body)

        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                self._analyze_class(file_record, stmt, analysis, module_names)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._analyze_function(
                    file_record,
                    stmt,
                    function_id(f"{file_record.module}.{stmt.name}"),
                    analysis,
                    module_names,
                )
        return analysis

    def _analyze_class(
        self,
        file_record: FileRecord,
        stmt: ast.ClassDef,
        analysis: BindingAnalysis,
        module_names: set[str],
    ) -> None:
        class_qualname = f"{file_record.module}.{stmt.name}"
        _BindingScopeVisitor(
            self,
            analysis=analysis,
            file_record=file_record,
            scope_id=class_id(class_qualname),
            scope_kind="class",
            module_names=module_names,
            class_name=stmt.name,
            class_qualname=class_qualname,
        ).visit_class_body(stmt.body)
        for child in stmt.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._analyze_function(
                    file_record,
                    child,
                    method_id(f"{class_qualname}.{child.name}"),
                    analysis,
                    module_names,
                    class_name=stmt.name,
                )

    def _analyze_function(
        self,
        file_record: FileRecord,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
        scope_id: str,
        analysis: BindingAnalysis,
        module_names: set[str],
        *,
        class_name: str | None = None,
    ) -> None:
        visitor = _BindingScopeVisitor(
            self,
            analysis=analysis,
            file_record=file_record,
            scope_id=scope_id,
            scope_kind="method" if class_name else "function",
            module_names=module_names,
            class_name=class_name,
        )
        visitor.visit_function_signature(stmt.args)
        visitor.visit_function_body(stmt.body)
        regions = local_definition_regions(stmt.body)
        for binding in analysis.bindings_by_scope.get(scope_id, []):
            region = regions.get((binding.get("line"), binding.get("column")))
            if binding.get("kind") == "function_definition" and region is not None:
                binding["definition_region"] = region
        for child in nested_functions(stmt.body):
            self._analyze_function(
                file_record,
                child,
                function_id(f"{scope_id.split(':', 1)[1]}.{child.name}"),
                analysis,
                module_names,
            )

    @staticmethod
    def unparse(node: ast.AST | None) -> str | None:
        if node is None:
            return None
        try:
            return ast.unparse(node)
        except Exception:
            return node.__class__.__name__


class _BindingScopeVisitor(ast.NodeVisitor):
    def __init__(
        self,
        analyzer: BindingAnalyzer,
        *,
        analysis: BindingAnalysis,
        file_record: FileRecord,
        scope_id: str,
        scope_kind: str,
        module_names: set[str],
        class_name: str | None = None,
        class_qualname: str | None = None,
    ) -> None:
        self.analyzer = analyzer
        self.analysis = analysis
        self.file_record = file_record
        self.scope_id = scope_id
        self.scope_kind = scope_kind
        self.comprehension_environment: dict[str, str | None] = {}
        self.comprehension_depth = 0
        self.module_names = module_names
        self.class_name = class_name
        self.class_qualname = class_qualname
        self.static_only = False
        self.direct_statements: set[int] = set()
        self.flow = ModuleFlow()
        self._imported_names: dict[str, dict[str, Any]] = {}

    def visit_module_body(self, body: list[ast.stmt]) -> None:
        self.direct_statements = {id(stmt) for stmt in body}
        self.flow = module_flow(body)
        for stmt in body:
            self.visit(stmt)

    def visit_class_body(self, body: list[ast.stmt]) -> None:
        self.direct_statements = {id(stmt) for stmt in body}
        for stmt in body:
            self.visit(stmt)

    def visit_function_signature(self, args: ast.arguments) -> None:
        for arg in self._all_arguments(args):
            self._add_binding(
                self.analysis,
                name=arg.arg,
                kind="parameter",
                node=arg,
                annotation=self.analyzer.unparse(arg.annotation),
                confidence="confirmed",
            )

    def visit_function_body(self, body: list[ast.stmt]) -> None:
        for stmt in body:
            self.visit(stmt)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound_name = alias.asname or alias.name.split(".", 1)[0]
            target_module = self._resolve_internal(alias.name)
            self._add_binding(
                self.analysis,
                name=bound_name,
                kind=(
                    "type_checking_import_alias" if self.static_only else "import_alias"
                ),
                node=node,
                target=target_module,
                target_module=target_module,
                value=alias.name,
                confidence="confirmed",
                static_only=self.static_only,
                imported_name=alias.name,
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = ImportAnalyzer._resolve_import_from_base(self.file_record, node)
        if not base:
            return
        for alias in node.names:
            if alias.name == "*":
                self._add_binding(
                    self.analysis,
                    name="*",
                    kind="star_import",
                    node=node,
                    target_module=self._resolve_internal(base) or base,
                    value=base,
                    confidence="heuristic",
                    static_only=self.static_only,
                    imported_name="*",
                )
                continue
            bound_name = alias.asname or alias.name
            imported = f"{base}.{alias.name}"
            target_module = self._resolve_internal(imported) or self._resolve_internal(
                base
            )
            self._add_binding(
                self.analysis,
                name=bound_name,
                kind=(
                    "type_checking_import_alias" if self.static_only else "import_alias"
                ),
                node=node,
                target=target_module,
                target_module=target_module,
                target_qualname=imported,
                value=imported,
                confidence="confirmed",
                static_only=self.static_only,
                imported_name=alias.name,
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        if self.scope_kind == "module":
            self._maybe_record_all_reexports(node)
        for target in node.targets:
            self._add_target_bindings(
                target,
                node,
                kind="assignment",
                value=self.analyzer.unparse(node.value),
            )
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._add_target_bindings(
            node.target,
            node,
            kind="annotated_assignment",
            value=self.analyzer.unparse(node.value),
            annotation=self.analyzer.unparse(node.annotation),
        )
        if node.value is not None:
            self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._add_target_bindings(target, node, kind="deletion")

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            for pattern in ast.walk(case.pattern):
                name = (
                    pattern.name
                    if isinstance(pattern, (ast.MatchAs, ast.MatchStar))
                    else pattern.rest if isinstance(pattern, ast.MatchMapping) else None
                )
                if name:
                    self._add_binding(
                        self.analysis,
                        name=name,
                        kind="pattern_target",
                        node=pattern,
                        confidence="inferred",
                    )
            if case.guard is not None:
                self.visit(case.guard)
            for stmt in case.body:
                self.visit(stmt)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        if self.comprehension_depth:
            self.analysis.comprehension_writes.setdefault(self.scope_id, set()).update(
                name for name, _ in self._unpack_targets(node.target)
            )
        self._add_target_bindings(
            node.target,
            node,
            kind="assignment",
            value=self.analyzer.unparse(node.value),
        )
        self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if (
            not self.static_only
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
        ):
            self.analysis.export_mutations_by_scope.add(self.scope_id)
        self._add_target_bindings(
            node.target,
            node,
            kind="assignment",
            value=self.analyzer.unparse(node.value),
        )
        self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        self._visit_for(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_for(node)

    def _visit_for(self, node: _ForNode) -> None:
        self._add_target_bindings(
            node.target,
            node,
            kind="for_target",
            value=self.analyzer.unparse(node.iter),
            confidence="confirmed",
            value_region=[node.body[0].lineno, node.body[-1].end_lineno],
        )
        self.visit(node.iter)
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_With(self, node: ast.With) -> None:
        self._visit_with(node, context_manager_kind="sync")

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._visit_with(node, context_manager_kind="async")

    def _visit_with(self, node: _WithNode, *, context_manager_kind: str) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._add_target_bindings(
                    item.optional_vars,
                    node,
                    kind="with_as",
                    value=self.analyzer.unparse(item.context_expr),
                    context_manager_kind=context_manager_kind,
                    confidence="confirmed",
                )
        for stmt in node.body:
            self.visit(stmt)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self._add_binding(
                self.analysis,
                name=node.name,
                kind="except_as",
                node=node,
                value=self.analyzer.unparse(node.type),
                confidence="confirmed",
            )
        for stmt in node.body:
            self.visit(stmt)

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            self._add_binding(
                self.analysis,
                name=name,
                kind="global",
                node=node,
                confidence="confirmed",
            )

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        for name in node.names:
            self._add_binding(
                self.analysis,
                name=name,
                kind="nonlocal",
                node=node,
                confidence="confirmed",
            )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_function_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_function_definition(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Nested class targets stay unresolved until SymbolAnalyzer emits nested nodes.
        qualname = (
            f"{self.file_record.module}.{node.name}"
            if self.scope_kind == "module"
            else None
        )
        self._add_binding(
            self.analysis,
            name=node.name,
            kind="class_definition",
            node=node,
            target=class_id(qualname) if qualname else None,
            target_qualname=qualname,
            confidence="confirmed",
        )

    def visit_If(self, node: ast.If) -> None:
        if self._is_type_checking_guard(node.test):
            previous = self.static_only
            self.static_only = True
            for stmt in node.body:
                self.visit(stmt)
            self.static_only = previous
            for stmt in node.orelse:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node, [node.key, node.value])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node, [node.elt])

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None

    def _record_function_definition(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        target: str | None = None
        target_qualname: str | None = None
        kind = "function_definition"
        if self.scope_kind == "module":
            target_qualname = f"{self.file_record.module}.{node.name}"
            target = function_id(target_qualname)
        elif self.scope_kind == "class" and self.class_qualname:
            target_qualname = f"{self.class_qualname}.{node.name}"
            target = method_id(target_qualname)
            kind = "method_definition"
        elif self.scope_kind in {"function", "method"}:
            target_qualname = f"{self.scope_id.split(':', 1)[1]}.{node.name}"
            target = function_id(target_qualname)
        self._add_binding(
            self.analysis,
            name=node.name,
            kind=kind,
            node=node,
            target=target,
            target_qualname=target_qualname,
            confidence="confirmed",
        )

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        results: list[ast.expr],
    ) -> None:
        outer = self.comprehension_environment
        self.comprehension_depth += 1
        local = {
            **outer,
            **{
                name: None
                for generator in node.generators
                for name, _ in self._unpack_targets(generator.target)
            },
        }
        if self.scope_kind == "class":
            # The implicit function cannot capture class-body locals.
            for binding in self.analysis.bindings_by_scope.get(self.scope_id, []):
                local.setdefault(str(binding["name"]), None)
        for index, generator in enumerate(node.generators):
            # Only the first iterable is evaluated in the enclosing scope.
            self.comprehension_environment = outer if index == 0 else local.copy()
            self._visit_comprehension_expression(generator.iter)
            self._add_target_bindings(
                generator.target,
                generator.target,
                kind="comprehension_target",
                value=self.analyzer.unparse(generator.iter),
                confidence="confirmed",
                value_region=[node.lineno, node.end_lineno],
            )
            for name, _ in self._unpack_targets(generator.target):
                local[name] = binding_id(
                    self.scope_id,
                    name,
                    "comprehension_target",
                    self.file_record.path,
                    generator.target.lineno,
                    generator.target.col_offset,
                )
            self.comprehension_environment = local.copy()
            for condition in generator.ifs:
                self._visit_comprehension_expression(condition)
        for result in results:
            self._visit_comprehension_expression(result)
        self.comprehension_environment = outer
        self.comprehension_depth -= 1

    def _visit_comprehension_expression(self, node: ast.expr) -> None:
        self.analysis.comprehension_contexts.setdefault(self.scope_id, []).append(
            {
                "range": [
                    node.lineno,
                    node.col_offset,
                    node.end_lineno,
                    node.end_col_offset,
                ],
                "bindings": self.comprehension_environment.copy(),
            }
        )
        self.visit(node)

    def _add_target_bindings(
        self,
        target: ast.AST,
        node: ast.AST,
        *,
        kind: str,
        value: str | None = None,
        annotation: str | None = None,
        confidence: str = "inferred",
        context_manager_kind: str | None = None,
        value_region: list[int] | None = None,
    ) -> None:
        if not self.static_only:
            for part in ast.walk(target):
                if isinstance(part, (ast.Subscript, ast.Attribute)):
                    root = part.value
                    while isinstance(root, (ast.Subscript, ast.Attribute)):
                        root = root.value
                    if isinstance(root, ast.Name) and root.id == "__all__":
                        self.analysis.export_mutations_by_scope.add(self.scope_id)
        for name, unpack_path in self._unpack_targets(target):
            self._add_binding(
                self.analysis,
                name=name,
                kind=kind,
                node=node,
                value=value,
                annotation=annotation,
                confidence=confidence,
                context_manager_kind=context_manager_kind,
                unpack_path=unpack_path,
                value_region=value_region,
            )
        for attr in self._self_attributes(target):
            self._add_binding(
                self.analysis,
                name=attr,
                kind="instance_attribute",
                node=node,
                value=value,
                annotation=annotation,
                confidence=confidence,
                owner="self",
                context_manager_kind=context_manager_kind,
            )

    def _unpack_targets(
        self, target: ast.AST, path: list[list[int]] | None = None
    ) -> list[tuple[str, list[list[int]]]]:
        path = path or []
        if isinstance(target, (ast.Tuple, ast.List)):
            size = len(target.elts)
            supported = not any(isinstance(item, ast.Starred) for item in target.elts)
            return [
                row
                for index, item in enumerate(target.elts)
                for row in self._unpack_targets(
                    item, [*path, [index, size if supported else -1]]
                )
            ]
        return [(name, path) for name in self._target_names(target)]

    def _add_binding(
        self,
        analysis: BindingAnalysis,
        *,
        name: str,
        kind: str,
        node: ast.AST,
        confidence: str,
        target: str | None = None,
        target_module: str | None = None,
        target_qualname: str | None = None,
        value: str | None = None,
        annotation: str | None = None,
        static_only: bool | None = None,
        imported_name: str | None = None,
        owner: str | None = None,
        context_manager_kind: str | None = None,
        unpack_path: list[list[int]] | None = None,
        value_region: list[int] | None = None,
    ) -> None:
        line = getattr(node, "lineno", None)
        column = getattr(node, "col_offset", None)
        evidence = Evidence(
            kind=f"ast_binding:{kind}",
            path=self.file_record.path,
            start_line=line,
            end_line=getattr(node, "end_lineno", line),
            column=column,
            detail=name,
        )
        record = {
            "binding_id": binding_id(
                self.scope_id,
                name,
                kind,
                self.file_record.path,
                line,
                column,
            ),
            "scope_id": self.scope_id,
            "scope_kind": self.scope_kind,
            "name": name,
            "kind": kind,
            "confidence": confidence,
            "path": self.file_record.path,
            "line": line,
            "column": column,
            "evidence": evidence.model_dump(exclude_none=True),
        }
        if self.scope_kind in {"module", "class"} and kind in {
            "import_alias",
            "class_definition",
            "method_definition",
        }:
            record["scope_direct"] = id(node) in self.direct_statements
        if kind == "method_definition":
            record["decorators"] = [
                self.analyzer.unparse(d) for d in node.decorator_list
            ]
        if kind == "comprehension_target":
            record["comprehension_inputs"] = self.comprehension_environment.copy()
        elif isinstance(node, ast.NamedExpr) and self.comprehension_depth:
            record["comprehension_write"] = True
        self._set_optional(record, "target", target)
        self._set_optional(record, "target_module", target_module)
        self._set_optional(record, "target_qualname", target_qualname)
        self._set_optional(record, "value", value)
        self._set_optional(record, "annotation", annotation)
        if value_region:
            record["value_region"] = value_region
        if kind in {"assignment", "annotated_assignment"} and isinstance(
            getattr(node, "end_col_offset", None), int
        ):
            # Where the statement ends: a call later on its line, as in
            # c = Other(); c.send(), sees the new value, while one inside it,
            # as in x = x.strip(), still sees the old.
            record["statement_end"] = [node.end_lineno, node.end_col_offset]
        if unpack_path:
            record["unpack_path"] = unpack_path
        if static_only or self.static_only:
            record["static_only"] = True
        if self.scope_kind == "module":
            self._record_module_flow(record, node, kind, name)
        self._set_optional(record, "imported_name", imported_name)
        self._set_optional(record, "owner", owner)
        self._set_optional(record, "context_manager_kind", context_manager_kind)

        if kind in {"import_alias", "type_checking_import_alias", "star_import"}:
            self._imported_names.setdefault(name, record)
        elif name in self._imported_names and kind not in {"global", "nonlocal"}:
            record["shadows_binding_id"] = self._imported_names[name]["binding_id"]
            self._add_shadowing_diagnostic(analysis, record, self._imported_names[name])

        analysis.bindings_by_scope.setdefault(self.scope_id, []).append(record)

    def _record_module_flow(
        self, record: dict[str, Any], node: ast.AST, kind: str, name: str
    ) -> None:
        """Mark a module binding that may not have run by the module's end,
        with the block where it surely has, and the statement around it, if
        any, that surely binds the name by its end."""

        if kind == "for_target":
            # Bound in the loop's body, and only if it runs once.
            block = self.flow.loop_bodies.get(id(node))
        elif isinstance(node, ast.NamedExpr):
            # Bound only where the expression around it reaches it.
            block = None
        else:
            block = self.flow.blocks.get(id(node))
            if block is None:
                return
        record["may_not_run"] = True
        if block is not None:
            record["block"] = block
        position = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        for span, names in self.flow.settling:
            if name in names and (span[0], span[1]) <= position <= (span[2], span[3]):
                record["settles_with"] = span
                break

    def _add_shadowing_diagnostic(
        self,
        analysis: BindingAnalysis,
        binding: dict[str, Any],
        shadowed: dict[str, Any],
    ) -> None:
        analysis.diagnostics_by_scope.setdefault(self.scope_id, []).append(
            {
                "kind": "binding_shadowing",
                "scope_id": self.scope_id,
                "name": binding["name"],
                "binding_id": binding["binding_id"],
                "shadowed_binding_id": shadowed["binding_id"],
                "path": binding["path"],
                "line": binding["line"],
                "column": binding["column"],
                "message": (
                    f"Local binding {binding['name']!r} shadows imported binding "
                    f"in {self.scope_id}."
                ),
            }
        )

    def _maybe_record_all_reexports(self, node: ast.Assign) -> None:
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            return
        exported = self._string_sequence(node.value)
        for name in exported:
            self._add_binding(
                self.analysis,
                name=name,
                kind="re_export",
                node=node,
                value="__all__",
                confidence="confirmed",
            )

    def _resolve_internal(self, candidate: str) -> str | None:
        resolved = ImportAnalyzer._resolve_internal(candidate, self.module_names)
        return module_id(resolved) if resolved else None

    @staticmethod
    def _is_type_checking_guard(test: ast.AST) -> bool:
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        if isinstance(test, ast.Attribute):
            return test.attr == "TYPE_CHECKING"
        return False

    @staticmethod
    def _target_names(target: ast.AST) -> Iterable[str]:
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                yield from _BindingScopeVisitor._target_names(item)
        elif isinstance(target, ast.Starred):
            yield from _BindingScopeVisitor._target_names(target.value)

    @staticmethod
    def _self_attributes(target: ast.AST) -> Iterable[str]:
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            if target.value.id == "self":
                yield f"self.{target.attr}"
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                yield from _BindingScopeVisitor._self_attributes(item)

    @staticmethod
    def _string_sequence(node: ast.AST) -> list[str]:
        if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return []
        values: list[str] = []
        for item in node.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                values.append(item.value)
        return values

    @staticmethod
    def _all_arguments(args: ast.arguments) -> Iterable[ast.arg]:
        yield from args.posonlyargs
        yield from args.args
        if args.vararg:
            yield args.vararg
        yield from args.kwonlyargs
        if args.kwarg:
            yield args.kwarg

    @staticmethod
    def _set_optional(record: dict[str, Any], key: str, value: Any | None) -> None:
        if value is not None:
            record[key] = value
