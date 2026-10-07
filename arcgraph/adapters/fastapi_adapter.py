"""FastAPI route extraction for ArcGraph."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from arcgraph.adapters.common import (
    AdapterAnalysis,
    ImportAliases,
    NodeIndex,
    SemanticAdapter,
    call_name,
    combine_paths,
    import_aliases,
    keyword,
    literal_str,
    literal_str_list,
    module_node_id,
    resolve_alias,
    resolve_module_name,
    symbol_id_for_def,
)
from arcgraph.core.ids import function_id, route_id
from arcgraph.core.schemas import BuildWarning, Edge, Evidence, FileRecord, Node


@dataclass(frozen=True)
class RouteTarget:
    name: str
    prefix: str
    kind: str


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

        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            route_targets = self._route_targets(tree, imports)
            skipped = self._unindexed_route_decorators(tree, route_targets)
            if skipped:
                analysis.warnings.append(
                    self._unindexed_routes_warning(file_record, skipped)
                )

            for stmt in tree.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    source_id = symbol_id_for_def(file_record, stmt.name, function_id)
                    for decorator in stmt.decorator_list:
                        route_call = self._route_call(decorator, route_targets)
                        if route_call is None:
                            continue
                        route_target, method, call = route_call
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
                            owner_key=self._owner_key(
                                file_record.module, route_target.name
                            ),
                            owner_name=route_target.name,
                            owner_kind=route_target.kind,
                            method=method,
                            path=route_path,
                            source_id=source_id,
                            handler_name=stmt.name,
                            dependencies=tuple(dependencies),
                            tags=tuple(literal_str_list(keyword(call, "tags"))),
                            file_record=file_record,
                            decorator=decorator,
                            start_line=getattr(decorator, "lineno", stmt.lineno),
                            end_line=getattr(stmt, "end_lineno", stmt.lineno),
                        )
                        registrations_by_owner.setdefault(
                            registration.owner_key, []
                        ).append(registration)

        for registrations in registrations_by_owner.values():
            for registration in registrations:
                imports = import_aliases(
                    registration.file_record,
                    parsed_files[registration.file_record.path],
                    module_names,
                )
                self._add_route(analysis, node_index, registration, imports)

        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            route_targets = self._route_targets(tree, imports)

            self._include_router_edges(
                analysis,
                node_index,
                file_record,
                tree,
                imports,
                module_names,
                parsed_files,
                route_targets,
                registrations_by_owner,
            )

        analysis.nodes.sort(key=lambda node: node.id)
        analysis.edges.sort(key=lambda edge: (edge.source, edge.target, edge.kind))
        return analysis

    def _route_targets(
        self, tree: ast.Module, imports: ImportAliases
    ) -> dict[str, RouteTarget]:
        targets: dict[str, RouteTarget] = {}
        for stmt in tree.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if not isinstance(stmt.value, ast.Call):
                continue
            factory = resolve_alias(call_name(stmt.value.func), imports)
            short = factory.rsplit(".", 1)[-1] if factory else None
            if short not in {"APIRouter", "FastAPI"}:
                continue
            prefix = (
                literal_str(keyword(stmt.value, "prefix"))
                if short == "APIRouter"
                else None
            ) or ""
            kind = "router" if short == "APIRouter" else "app"
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    targets[target.id] = RouteTarget(
                        name=target.id,
                        prefix=prefix,
                        kind=kind,
                    )
        return targets

    def _unindexed_route_decorators(
        self, tree: ast.Module, route_targets: dict[str, RouteTarget]
    ) -> list[tuple[str, int]]:
        """Route decorators of this file's routers that no route is made of.

        Only a module-level function becomes a route, so a decorator such as
        @router.get on a method of a class-based controller, or on a nested
        function, is passed over; it is listed here so the build can say so.
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
                    visit(stmt.body, f"{prefix}{stmt.name}.", False)

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
                "indexes FastAPI routes on module-level functions only."
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
        if not isinstance(decorator.func.value, ast.Name):
            return None
        target_name = decorator.func.value.id
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

    def _include_router_edges(
        self,
        analysis: AdapterAnalysis,
        node_index: NodeIndex,
        file_record: FileRecord,
        tree: ast.Module,
        imports: ImportAliases,
        module_names: set[str],
        parsed_files: dict[str, ast.Module],
        route_targets: dict[str, RouteTarget],
        registrations_by_owner: dict[str, list[RouteRegistration]],
    ) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not self._is_include_router_call(node, route_targets):
                continue
            if not node.args:
                continue
            router_ref = call_name(node.args[0])
            if not router_ref:
                continue

            module_name = resolve_module_name(router_ref, imports)
            if module_name in module_names:
                analysis.edges.append(
                    self._edge(
                        module_node_id(file_record),
                        f"mod:{module_name}",
                        "includes_router",
                        file_record,
                        node,
                        f"include_router({router_ref})",
                    )
                )

            include_prefix = literal_str(keyword(node, "prefix")) or ""
            if not include_prefix:
                continue
            for owner_key in self._router_owner_keys(router_ref, imports):
                for registration in registrations_by_owner.get(owner_key, []):
                    prefixed = self._with_include_prefix(registration, include_prefix)
                    if prefixed.path != registration.path:
                        reg_imports = import_aliases(
                            registration.file_record,
                            parsed_files[registration.file_record.path],
                            module_names,
                        )
                        self._add_route(analysis, node_index, prefixed, reg_imports)

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
            target_id = node_index.function_id_for(
                dependency, registration.file_record, imports
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
    def _is_include_router_call(
        node: ast.Call, route_targets: dict[str, RouteTarget]
    ) -> bool:
        if not isinstance(node.func, ast.Attribute):
            return False
        if node.func.attr != "include_router":
            return False
        receiver = call_name(node.func.value)
        return bool(receiver and route_targets.get(receiver, None))

    @staticmethod
    def _with_include_prefix(
        registration: RouteRegistration, include_prefix: str
    ) -> RouteRegistration:
        return RouteRegistration(
            owner_key=registration.owner_key,
            owner_name=registration.owner_name,
            owner_kind=registration.owner_kind,
            method=registration.method,
            path=combine_paths(include_prefix, registration.path),
            source_id=registration.source_id,
            handler_name=registration.handler_name,
            dependencies=registration.dependencies,
            tags=registration.tags,
            file_record=registration.file_record,
            decorator=registration.decorator,
            start_line=registration.start_line,
            end_line=registration.end_line,
        )

    @staticmethod
    def _router_owner_keys(router_ref: str, imports: ImportAliases) -> list[str]:
        keys: list[str] = []
        module_name = resolve_module_name(router_ref, imports)
        variable_name = router_ref.rsplit(".", 1)[-1]
        if module_name:
            keys.append(FastAPIAdapter._owner_key(module_name, variable_name))
        resolved = resolve_alias(router_ref, imports)
        if resolved and "." in resolved:
            module, variable = resolved.rsplit(".", 1)
            keys.append(FastAPIAdapter._owner_key(module, variable))
        return list(dict.fromkeys(keys))

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
