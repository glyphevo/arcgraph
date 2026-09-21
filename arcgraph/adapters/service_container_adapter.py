"""ServiceContainer dependency and typed service call extraction."""

from __future__ import annotations

import ast
from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import dataclass

from arcgraph.adapters.common import (
    AdapterAnalysis,
    ImportAliases,
    NodeIndex,
    SemanticAdapter,
    annotation_name,
    call_name,
    import_aliases,
    iter_symbol_defs,
    symbol_id_for_def,
)
from arcgraph.core.schemas import Edge, Evidence, FileRecord, Node


@dataclass(frozen=True)
class ProviderFact:
    source_id: str
    target: Node


class ServiceContainerAdapter(SemanticAdapter):
    """Extract high-confidence ServiceContainer provider facts.

    The adapter intentionally keeps the model simple:
    - provider/factory functions point to concrete service classes with
      ``provides`` edges;
    - call sites that obtain a service from a provider get ``injects`` edges;
    - method calls on typed service variables become confirmed ``calls`` edges.
    """

    name = "service_container"
    capabilities = ("edge:provides", "edge:injects", "edge:calls")
    required_facts = ("class", "function", "method")

    SERVICE_SUFFIXES = ("Service", "Pipeline", "Orchestrator")

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        analysis = AdapterAnalysis()
        node_index = NodeIndex(nodes)
        providers = self._provider_facts(
            files, parsed_files, node_index, module_names, analysis
        )

        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            for stmt, qualified_name, id_factory in iter_symbol_defs(tree):
                source_id = symbol_id_for_def(file_record, qualified_name, id_factory)
                locals_by_name = self._typed_locals(
                    stmt,
                    file_record,
                    imports,
                    node_index,
                    providers,
                    source_id,
                    analysis,
                )
                analysis.edges.extend(
                    self._typed_service_call_edges(
                        stmt,
                        file_record,
                        node_index,
                        locals_by_name,
                        source_id,
                        imports=imports,
                        providers=providers,
                        analysis=analysis,
                        closure_locals=locals_by_name,
                    )
                )

        analysis.edges.sort(key=lambda edge: (edge.source, edge.target, edge.kind))
        return analysis

    def _provider_facts(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        node_index: NodeIndex,
        module_names: set[str],
        analysis: AdapterAnalysis,
    ) -> dict[str, list[ProviderFact]]:
        providers: dict[str, list[ProviderFact]] = {}
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            for stmt, qualified_name, id_factory in iter_symbol_defs(tree):
                target = self._provided_class(stmt, file_record, imports, node_index)
                if target is None:
                    continue
                source_id = symbol_id_for_def(file_record, qualified_name, id_factory)
                providers.setdefault(stmt.name, []).append(
                    ProviderFact(source_id=source_id, target=target)
                )
                analysis.edges.append(
                    self._edge(
                        source_id,
                        target.id,
                        "provides",
                        "service_container_factory",
                        file_record,
                        stmt,
                        f"{stmt.name} -> {target.name}",
                    )
                )
        return providers

    def _provided_class(
        self,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
        file_record: FileRecord,
        imports: ImportAliases,
        node_index: NodeIndex,
    ) -> Node | None:
        annotated = node_index.class_for_annotation(
            annotation_name(stmt.returns), file_record, imports
        )
        if self._is_service_like(annotated):
            return annotated

        for node in _body_nodes(stmt):
            if not isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)):
                continue
            value = self._unwrap_expr(node.value)
            target = self._class_from_expr(value, file_record, imports, node_index)
            if self._is_service_like(target):
                return target
        return None

    def _typed_locals(
        self,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        file_record: FileRecord,
        imports: ImportAliases,
        node_index: NodeIndex,
        providers: dict[str, list[ProviderFact]],
        source_id: str,
        analysis: AdapterAnalysis,
    ) -> dict[str, Node]:
        locals_by_name: dict[str, Node] = {}

        args = getattr(stmt, "args", None)
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs] if args else []:
            target = node_index.class_for_annotation(
                annotation_name(arg.annotation), file_record, imports
            )
            if self._is_service_like(target):
                locals_by_name[arg.arg] = target
                analysis.edges.append(
                    self._edge(
                        source_id,
                        target.id,
                        "injects",
                        "typed_service_parameter",
                        file_record,
                        arg,
                        f"{arg.arg}: {target.name}",
                    )
                )

        for node in _body_nodes(stmt):
            if isinstance(node, ast.Assign):
                target = self._service_from_expr(
                    node.value, file_record, imports, node_index, providers
                )
                if target:
                    for item in node.targets:
                        if isinstance(item, ast.Name):
                            locals_by_name[item.id] = target.target
                            self._record_injection(
                                source_id, target, file_record, node, analysis
                            )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target = node_index.class_for_annotation(
                    annotation_name(node.annotation), file_record, imports
                )
                if self._is_service_like(target):
                    locals_by_name[node.target.id] = target
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if not isinstance(item.optional_vars, ast.Name):
                        continue
                    target = self._service_from_expr(
                        item.context_expr,
                        file_record,
                        imports,
                        node_index,
                        providers,
                    )
                    if target:
                        locals_by_name[item.optional_vars.id] = target.target
                        self._record_injection(
                            source_id, target, file_record, item.context_expr, analysis
                        )

        for node in _body_nodes(stmt):
            if not isinstance(node, ast.Call):
                continue
            target = self._service_from_expr(
                node, file_record, imports, node_index, providers
            )
            if target:
                self._record_injection(source_id, target, file_record, node, analysis)

        return locals_by_name

    def _typed_service_call_edges(
        self,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        file_record: FileRecord,
        node_index: NodeIndex,
        locals_by_name: dict[str, Node],
        source_id: str,
        *,
        imports: ImportAliases,
        providers: dict[str, list[ProviderFact]],
        analysis: AdapterAnalysis,
        closure_locals: dict[str, Node],
    ) -> list[Edge]:
        edges: list[Edge] = []
        class_positions = (
            _class_local_positions(stmt) if isinstance(stmt, ast.ClassDef) else {}
        )
        for node in _body_nodes(stmt):
            if isinstance(node, ast.ClassDef):
                # A class executes now, in its own namespace. Its assignments
                # must not type the enclosing function or an inner class.
                bound_names = _class_bound_names(node)
                class_locals = {
                    name: target
                    for name, target in closure_locals.items()
                    if name not in bound_names
                }
                inferred_locals = self._typed_locals(
                    node,
                    file_record,
                    imports,
                    node_index,
                    providers,
                    source_id,
                    analysis,
                )
                positions = _class_local_positions(node)
                class_locals.update(
                    {
                        name: target
                        for name, target in inferred_locals.items()
                        if name in positions
                    }
                )
                edges.extend(
                    self._typed_service_call_edges(
                        node,
                        file_record,
                        node_index,
                        class_locals,
                        source_id,
                        imports=imports,
                        providers=providers,
                        analysis=analysis,
                        closure_locals=closure_locals,
                    )
                )
                continue
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if not isinstance(func.value, ast.Name):
                continue
            if (
                func.value.id in class_positions
                and (node.lineno, node.col_offset) <= class_positions[func.value.id]
            ):
                continue
            class_node = locals_by_name.get(func.value.id)
            if class_node is None:
                continue
            method = node_index.method_for_class(class_node, func.attr)
            if method is None or method.id == source_id:
                continue
            edges.append(
                self._edge(
                    source_id,
                    method.id,
                    "calls",
                    "typed_service_call",
                    file_record,
                    node,
                    f"{func.value.id}.{func.attr} -> {method.qualname}",
                )
            )
        return edges

    def _service_from_expr(
        self,
        expr: ast.AST,
        file_record: FileRecord,
        imports: ImportAliases,
        node_index: NodeIndex,
        providers: dict[str, list[ProviderFact]],
    ) -> ProviderFact | None:
        expr = self._unwrap_expr(expr)
        if not isinstance(expr, ast.Call):
            return None

        provider_name = call_name(expr.func)
        if provider_name:
            provider = self._provider_for_call(
                provider_name,
                file_record,
                providers,
                prefer_method=isinstance(expr.func, ast.Attribute),
            )
            if provider:
                return provider

        target = self._class_from_expr(expr, file_record, imports, node_index)
        if self._is_service_like(target):
            return ProviderFact(source_id=target.id, target=target)
        return None

    @staticmethod
    def _provider_for_call(
        provider_name: str,
        file_record: FileRecord,
        providers: dict[str, list[ProviderFact]],
        *,
        prefer_method: bool,
    ) -> ProviderFact | None:
        short_name = provider_name.rsplit(".", 1)[-1]
        candidates = providers.get(short_name, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        if prefer_method:
            method_candidates = [
                fact for fact in candidates if fact.source_id.startswith("method:")
            ]
            if method_candidates:
                return sorted(method_candidates, key=lambda fact: fact.source_id)[0]

        same_module = f"fn:{file_record.module}.{short_name}"
        for fact in candidates:
            if fact.source_id == same_module:
                return fact

        return sorted(candidates, key=lambda fact: fact.source_id)[0]

    def _class_from_expr(
        self,
        expr: ast.AST | None,
        file_record: FileRecord,
        imports: ImportAliases,
        node_index: NodeIndex,
    ) -> Node | None:
        expr = self._unwrap_expr(expr)
        if isinstance(expr, ast.Call):
            return node_index.class_for_annotation(
                call_name(expr.func), file_record, imports
            )
        return None

    def _record_injection(
        self,
        source_id: str,
        provider: ProviderFact,
        file_record: FileRecord,
        node: ast.AST,
        analysis: AdapterAnalysis,
    ) -> None:
        if provider.source_id != provider.target.id:
            analysis.edges.append(
                self._edge(
                    source_id,
                    provider.source_id,
                    "calls",
                    "service_container_call",
                    file_record,
                    node,
                    f"container factory -> {provider.source_id}",
                )
            )
        analysis.edges.append(
            self._edge(
                source_id,
                provider.target.id,
                "injects",
                "service_container_call",
                file_record,
                node,
                f"injects {provider.target.name}",
            )
        )

    @classmethod
    def _is_service_like(cls, node: Node | None) -> bool:
        return bool(node and node.name.endswith(cls.SERVICE_SUFFIXES))

    @staticmethod
    def _unwrap_expr(expr: ast.AST | None) -> ast.AST | None:
        while isinstance(expr, ast.Await):
            expr = expr.value
        return expr

    @staticmethod
    def _edge(
        source: str,
        target: str,
        kind: str,
        evidence_kind: str,
        file_record: FileRecord,
        node: ast.AST,
        detail: str,
    ) -> Edge:
        return Edge(
            source=source,
            target=target,
            kind=kind,
            confidence="confirmed",
            evidence=[
                Evidence(
                    kind=evidence_kind,
                    path=file_record.path,
                    start_line=getattr(node, "lineno", None),
                    end_line=getattr(node, "end_lineno", getattr(node, "lineno", None)),
                    column=getattr(node, "col_offset", None),
                    detail=detail,
                )
            ],
        )


def _body_nodes(
    stmt: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> Iterator[ast.AST]:
    """Walk this callable's execution scope without borrowing nested bodies.

    Definition headers execute here; function bodies do not. Classes are yielded
    as scope boundaries so callers can analyze their eager bodies separately.
    The root callable's header belongs to its defining scope.
    """
    pending = deque(stmt.body)
    while pending:
        node = pending.popleft()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                pending.extend(node.decorator_list)
            pending.extend(node.args.defaults)
            pending.extend(
                value for value in node.args.kw_defaults if value is not None
            )
            continue
        if isinstance(node, ast.ClassDef):
            yield node
            pending.extend(node.decorator_list)
            pending.extend(node.bases)
            pending.extend(keyword.value for keyword in node.keywords)
            continue
        if isinstance(stmt, ast.ClassDef) and isinstance(
            node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            # Only the first iterable executes in the class namespace. The
            # implicit callable cannot close over class-local service bindings.
            pending.append(node.generators[0].iter)
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def _class_local_positions(stmt: ast.ClassDef) -> dict[str, tuple[int, int]]:
    positions = {}
    counts = Counter(_class_bound_names(stmt))
    for item in stmt.body:
        targets = (
            item.targets
            if isinstance(item, ast.Assign)
            else [item.target] if isinstance(item, ast.AnnAssign) else []
        )
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            # Multiple writes, conditional definitions, deletes and imports
            # make a class-local type uncertain; never reuse the earlier type.
            if counts[target.id] == 1:
                positions[target.id] = (item.end_lineno, item.end_col_offset)
    return positions


def _class_scope_nodes(stmt: ast.ClassDef) -> Iterator[ast.AST]:
    pending = list(stmt.body)
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                pending.extend(node.decorator_list)
            pending.extend(node.args.defaults)
            pending.extend(v for v in node.args.kw_defaults if v is not None)
            continue
        if isinstance(node, ast.ClassDef):
            pending.extend(node.decorator_list)
            pending.extend(node.bases)
            pending.extend(k.value for k in node.keywords)
            continue
        pending.extend(ast.iter_child_nodes(node))


def _class_bound_names(stmt: ast.ClassDef) -> list[str]:
    names: list[str] = []
    for node in _class_scope_nodes(stmt):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.append(node.id)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.extend(a.asname or a.name.split(".")[0] for a in node.names)
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            names.extend(node.names)
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.append(node.name)
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.append(node.name)
        if isinstance(node, ast.MatchMapping) and node.rest:
            names.append(node.rest)
    return names
