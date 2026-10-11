"""FastAPI route extraction for ArcGraph."""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace

from arcgraph.adapters.common import (
    AdapterAnalysis,
    ImportAliases,
    NodeIndex,
    SemanticAdapter,
    call_name,
    combine_paths,
    import_aliases,
    iter_symbol_defs,
    keyword,
    literal_str,
    literal_str_list,
    module_node_id,
    resolve_name,
    symbol_id_for_def,
)
from arcgraph.core.ids import route_id
from arcgraph.adapters.fastapi_routes import (
    constant_text,
    include_calls,
    imported_factory_shadowed,
    prefix_mutated,
    qualified,
    receiver_shadowed,
    router_factories,
    stable_receiver,
)
from arcgraph.core.schemas import BuildWarning, Edge, Evidence, FileRecord, Node


@dataclass(frozen=True)
class RouteTarget:
    name: str
    prefix: str
    kind: str
    prefix_known: bool = True
    owner_key: str | None = None


@dataclass(frozen=True)
class RouteRegistration:
    owner_key: str
    owner_name: str
    owner_kind: str
    method: str
    path: str
    source_id: str
    handler_name: str
    dependencies: tuple[str, ...]
    tags: tuple[str, ...]
    file_record: FileRecord
    decorator: ast.AST
    start_line: int
    end_line: int
    path_status: str = "known"
    parameters: tuple[str, ...] = ()
    self_injection: str = "not_applicable"
    path_scope: str = "local_registration"


class FastAPIAdapter(SemanticAdapter):
    name = "fastapi"
    capabilities = ("entrypoint:route", "edge:invokes", "edge:injects")
    required_facts = ("module", "function")

    HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        analysis = AdapterAnalysis()
        node_index = NodeIndex(nodes)
        registrations_by_owner: dict[str, list[RouteRegistration]] = {}

        contexts = [
            (
                file,
                parsed_files[file.path],
                import_aliases(file, parsed_files[file.path], module_names),
            )
            for file in files
            if file.path in parsed_files
        ]
        factories = router_factories(contexts)
        targets_by_module = {
            file.module: self._route_targets(tree, imports, file, factories)
            for file, tree, imports in contexts
        }

        canonical_targets = {
            self._owner_key(module, name): target
            for module, targets in targets_by_module.items()
            for name, target in targets.items()
        }
        for file, tree, imports in contexts:
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call) or not isinstance(
                    call.func, ast.Attribute
                ):
                    continue
                receiver = call_name(call.func.value)
                if not receiver:
                    continue
                key = qualified(receiver, file, imports)
                target = canonical_targets.get(key)
                if target is not None and not imported_factory_shadowed(
                    tree, call.func.value, imports
                ):
                    targets_by_module[file.module].setdefault(
                        receiver, replace(target, name=receiver, owner_key=key)
                    )

        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            route_targets = targets_by_module[file_record.module]
            skipped = self._unindexed_route_decorators(tree, route_targets)
            if skipped:
                analysis.warnings.append(
                    self._unindexed_routes_warning(file_record, skipped)
                )

            for stmt, qualname, id_factory in iter_symbol_defs(tree):
                source_id = symbol_id_for_def(file_record, qualname, id_factory)
                cls = next(
                    (
                        c
                        for c in tree.body
                        if isinstance(c, ast.ClassDef)
                        and qualname.startswith(c.name + ".")
                    ),
                    None,
                )
                for decorator in stmt.decorator_list:
                    route_call = self._route_call(decorator, route_targets)
                    if route_call is None:
                        continue
                    route_target, method, call = route_call
                    if cls is not None and receiver_shadowed(
                        cls, route_target.name.split(".")[0]
                    ):
                        continue
                    route_path = combine_paths(
                        route_target.prefix,
                        self._route_path(call),
                    )
                    dependencies = sorted(
                        set(
                            self._dependencies_from_route(call)
                            + self._dependencies_from_function(stmt)
                        )
                    )
                    registration = RouteRegistration(
                        owner_key=route_target.owner_key
                        or self._owner_key(file_record.module, route_target.name),
                        owner_name=route_target.name,
                        owner_kind=route_target.kind,
                        method=method,
                        path=route_path,
                        source_id=source_id,
                        handler_name=qualname,
                        dependencies=tuple(dependencies),
                        tags=tuple(literal_str_list(keyword(call, "tags"))),
                        file_record=file_record,
                        decorator=decorator,
                        start_line=getattr(decorator, "lineno", stmt.lineno),
                        end_line=getattr(stmt, "end_lineno", stmt.lineno),
                        path_status=(
                            "known"
                            if route_target.prefix_known
                            and literal_str(
                                call.args[0] if call.args else keyword(call, "path")
                            )
                            is not None
                            else "unknown_prefix"
                        ),
                        parameters=tuple(
                            a.arg
                            for a in [
                                *stmt.args.posonlyargs,
                                *stmt.args.args,
                                *stmt.args.kwonlyargs,
                            ]
                            if cls is None or a.arg not in {"self", "cls"}
                        ),
                        self_injection=(
                            ("unverified" if cls.decorator_list else "not_injected")
                            if cls
                            else "not_applicable"
                        ),
                    )
                    registrations_by_owner.setdefault(
                        registration.owner_key, []
                    ).append(registration)

        parents = {}
        all_targets = canonical_targets
        for file, tree, imports in contexts:
            for call in include_calls(tree, set(targets_by_module[file.module])):
                receiver = targets_by_module[file.module][call_name(call.func.value)]
                owner = receiver.owner_key or self._owner_key(
                    file.module, receiver.name
                )
                child = qualified(call_name(call.args[0]), file, imports)
                if child not in all_targets:
                    continue
                prefix_expr = keyword(call, "prefix")
                prefix = (
                    constant_text(prefix_expr, tree) if prefix_expr is not None else ""
                )
                target = all_targets[owner]
                parents.setdefault(child, []).append(
                    (
                        owner,
                        combine_paths(target.prefix, prefix or ""),
                        prefix is not None and target.prefix_known,
                    )
                )
                analysis.edges.append(
                    self._edge(
                        module_node_id(file),
                        "mod:" + child.rsplit(".", 1)[0],
                        "includes_router",
                        file,
                        call,
                        "include_router",
                    )
                )

        def mounted(owner, seen):
            if owner in seen:
                return [("", False)]
            if owner not in parents:
                return [("", True)]
            paths = []
            for parent, prefix, known in parents[owner]:
                paths.extend(
                    (combine_paths(outer, prefix), known and available)
                    for outer, available in mounted(parent, seen | {owner})
                )
            return paths

        for owner, registrations in registrations_by_owner.items():
            for registration in registrations:
                imports = import_aliases(
                    registration.file_record,
                    parsed_files[registration.file_record.path],
                    module_names,
                )
                variants = mounted(owner, set())
                if any(not known for _, known in variants):
                    registration = replace(registration, path_status="unknown_prefix")
                self._add_route(analysis, node_index, registration, imports)
                for prefix, known in variants:
                    current = replace(
                        registration,
                        path_scope="mounted_registration",
                        path=(
                            combine_paths(prefix, registration.path)
                            if known
                            else registration.path
                        ),
                        path_status=(
                            registration.path_status if known else "unknown_prefix"
                        ),
                    )
                    if current.path != registration.path:
                        self._add_route(analysis, node_index, current, imports)

        analysis.nodes.sort(key=lambda node: node.id)
        analysis.edges.sort(key=lambda edge: (edge.source, edge.target, edge.kind))
        return analysis

    def _route_targets(self, tree, imports, file_record, factories):
        targets = {}
        for stmt in tree.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or not isinstance(
                stmt.value, ast.Call
            ):
                continue
            if imported_factory_shadowed(tree, stmt.value.func, imports):
                continue
            factory = qualified(call_name(stmt.value.func), file_record, imports)
            kind = factories.get(factory)
            if kind is None:
                continue
            prefix_expr = keyword(stmt.value, "prefix") if kind == "router" else None
            prefix = constant_text(prefix_expr, tree) if prefix_expr is not None else ""
            assignments = (
                stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            )
            for target in assignments:
                if isinstance(target, ast.Name) and stable_receiver(tree, target.id):
                    known = prefix is not None and not prefix_mutated(tree, target.id)
                    targets[target.id] = RouteTarget(
                        target.id, prefix if known else "", kind, known
                    )
        return targets

    def _unindexed_route_decorators(
        self, tree: ast.Module, route_targets: dict[str, RouteTarget]
    ) -> list[tuple[str, int]]:
        """Route decorators of this file's routers that no route is made of.

        Module functions and methods of module classes become routes. Nested
        function registrations are reported because their execution is unknown.
        A router reached another way, such as self.router, is not seen.
        """

        found: list[tuple[str, int]] = []

        def visit(body: list[ast.stmt], prefix: str, top: bool) -> None:
            for stmt in body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = f"{prefix}{stmt.name}"
                    if not top:
                        found.extend(
                            (name, getattr(decorator, "lineno", stmt.lineno))
                            for decorator in stmt.decorator_list
                            if self._route_call(decorator, route_targets)
                        )
                    visit(stmt.body, f"{name}.", False)
                elif isinstance(stmt, ast.ClassDef):
                    visit(stmt.body, f"{prefix}{stmt.name}.", top)

        visit(tree.body, "", True)
        return found

    @staticmethod
    def _unindexed_routes_warning(
        file_record: FileRecord, skipped: list[tuple[str, int]]
    ) -> BuildWarning:
        examples = ", ".join(f"{name} (line {line})" for name, line in skipped[:3])
        more = f" and {len(skipped) - 3} more" if len(skipped) > 3 else ""
        return BuildWarning(
            kind="adapter_fastapi_routes_not_indexed",
            message=(
                f"{len(skipped)} FastAPI route decorator(s) on methods or nested "
                f"functions are not indexed as routes: {examples}{more}. ArcGraph "
                "indexes module-level functions and methods on module-level classes."
            ),
            path=file_record.path,
        )

    def _route_call(
        self, decorator: ast.expr, route_targets: dict[str, RouteTarget]
    ) -> tuple[RouteTarget, str, ast.Call] | None:
        if not isinstance(decorator, ast.Call):
            return None
        if not isinstance(decorator.func, ast.Attribute):
            return None
        method = decorator.func.attr
        if method not in self.HTTP_METHODS:
            return None
        target_name = call_name(decorator.func.value)
        route_target = route_targets.get(target_name)
        if route_target is None:
            return None
        return route_target, method, decorator

    @staticmethod
    def _route_path(call: ast.Call) -> str:
        if call.args:
            return literal_str(call.args[0]) or ""
        return literal_str(keyword(call, "path")) or ""

    def _dependencies_from_route(self, call: ast.Call) -> list[str]:
        values: list[str] = []
        dependencies = keyword(call, "dependencies")
        if isinstance(dependencies, (ast.List, ast.Tuple)):
            for item in dependencies.elts:
                dependency = self._dependency_name(item)
                if dependency:
                    values.append(dependency)
        return values

    def _dependencies_from_function(
        self, stmt: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> list[str]:
        values: list[str] = []
        positional_args = stmt.args.posonlyargs + stmt.args.args
        defaults = [None] * (len(positional_args) - len(stmt.args.defaults)) + list(
            stmt.args.defaults
        )
        for default in [*defaults, *stmt.args.kw_defaults]:
            if default is None:
                continue
            dependency = self._dependency_name(default)
            if dependency:
                values.append(dependency)
        return values

    @staticmethod
    def _dependency_name(node: ast.AST) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        dependency_func = call_name(node.func)
        dependency_kind = dependency_func.rsplit(".", 1)[-1] if dependency_func else ""
        if dependency_kind not in {"Depends", "Security"} or not node.args:
            return None
        return call_name(node.args[0])

    def _add_route(
        self,
        analysis: AdapterAnalysis,
        node_index: NodeIndex,
        registration: RouteRegistration,
        imports: ImportAliases,
    ) -> None:
        node_id = route_id(registration.method, registration.path)
        analysis.nodes.append(
            Node(
                id=node_id,
                kind="route",
                name=f"{registration.method.upper()} {registration.path}",
                qualname=f"{registration.method.upper()} {registration.path}",
                path=registration.file_record.path,
                start_line=registration.start_line,
                end_line=registration.end_line,
                properties={
                    "method": registration.method.upper(),
                    "path": registration.path,
                    "router": registration.owner_name,
                    "router_kind": registration.owner_kind,
                    "handler": registration.source_id,
                    "dependencies": list(registration.dependencies),
                    "tags": list(registration.tags),
                    "path_status": registration.path_status,
                    "path_scope": registration.path_scope,
                    "parameters": list(registration.parameters),
                    "self_injection": registration.self_injection,
                },
            )
        )
        analysis.edges.append(
            self._edge(
                node_id,
                registration.source_id,
                "invokes",
                registration.file_record,
                registration.decorator,
                (
                    f"{registration.method.upper()} {registration.path} "
                    f"-> {registration.handler_name}"
                ),
            )
        )
        for dependency in registration.dependencies:
            # A dependency name must identify its provider; unique short-name
            # guesses are not evidence for a newly registered class route.
            qualified_dependency = resolve_name(
                dependency, registration.file_record, imports
            )
            target_id = next(
                (
                    candidate
                    for candidate in (
                        "fn:" + qualified_dependency,
                        "class:" + qualified_dependency,
                    )
                    if candidate in node_index.by_id
                ),
                None,
            )
            if target_id:
                analysis.edges.append(
                    self._edge(
                        node_id,
                        target_id,
                        "injects",
                        registration.file_record,
                        registration.decorator,
                        f"Depends({dependency})",
                    )
                )

    @staticmethod
    def _owner_key(module: str, name: str) -> str:
        return f"{module}.{name}"

    @staticmethod
    def _edge(
        source: str,
        target: str,
        kind: str,
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
                    kind="fastapi_route",
                    path=file_record.path,
                    start_line=getattr(node, "lineno", None),
                    end_line=getattr(node, "end_lineno", getattr(node, "lineno", None)),
                    column=getattr(node, "col_offset", None),
                    detail=detail,
                )
            ],
        )
