"""Django route, model, and admin extraction for ArcGraph."""

from __future__ import annotations

import ast

from arcgraph.adapters.common import (
    AdapterAnalysis,
    ImportAliases,
    NodeIndex,
    SemanticAdapter,
    call_name,
    import_aliases,
    keyword,
    literal_str,
    module_node_id,
    resolve_alias,
)
from arcgraph.core.ids import class_id, function_id, method_id, route_id, table_id
from arcgraph.core.schemas import Edge, Evidence, FactResolution, FileRecord, Node


class DjangoAdapter(SemanticAdapter):
    """Extract Django URL routes, ORM models, and admin registrations."""

    name = "django"
    capabilities = (
        "entrypoint:route",
        "resource:table",
        "edge:invokes",
        "edge:maps_to",
        "edge:reads",
        "edge:writes",
    )
    required_facts = ("module", "class", "function")

    _URL_FUNCS = {"path", "re_path", "url"}
    _MODEL_BASES = {"Model", "models.Model"}
    _ADMIN_DECORATORS = {"admin.register", "register"}
    _MANAGER_METHODS_READ = {
        "get",
        "filter",
        "exclude",
        "all",
        "first",
        "last",
        "values",
        "values_list",
        "aggregate",
        "annotate",
        "count",
        "exists",
        "order_by",
        "select_related",
        "prefetch_related",
        "get_or_create",
        "distinct",
    }
    _MANAGER_METHODS_WRITE = {
        "create",
        "update",
        "delete",
        "bulk_create",
        "bulk_update",
        "update_or_create",
        "save",
    }

    # ── detect ──────────────────────────────────────────────

    def detect(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> bool:
        del nodes, module_names
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            for stmt in tree.body:
                if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    if self._is_django_import(stmt):
                        return True
        return False

    # ── analyze ─────────────────────────────────────────────

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        analysis = AdapterAnalysis()
        node_index = NodeIndex(nodes)

        # Pass 1: extract models and build table lookup
        model_tables: dict[str, str] = {}  # model class name -> table node id
        _AMBIGUOUS = "<ambiguous>"
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            for class_stmt in (s for s in tree.body if isinstance(s, ast.ClassDef)):
                if self._is_django_model(class_stmt, imports):
                    table_info = self._extract_model(
                        file_record,
                        class_stmt,
                        imports,
                        node_index,
                        analysis,
                    )
                    if table_info:
                        # Always register by fully-qualified name (unambiguous)
                        fqn = f"{file_record.module}.{class_stmt.name}"
                        model_tables[fqn] = table_info
                        # Also register by simple name unless ambiguous
                        short = class_stmt.name
                        existing = model_tables.get(short)
                        if existing is not None and existing != table_info:
                            # Cross-app name collision — mark ambiguous
                            model_tables[short] = _AMBIGUOUS
                        else:
                            model_tables[short] = table_info
        # Drop ambiguous entries so later passes never produce wrong edges
        model_tables = {k: v for k, v in model_tables.items() if v != _AMBIGUOUS}

        # Pass 2: extract URL patterns
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            self._extract_urlpatterns(
                file_record,
                tree,
                imports,
                node_index,
                analysis,
                module_names,
            )

        # Pass 3: extract admin registrations
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            self._extract_admin(
                file_record,
                tree,
                imports,
                node_index,
                model_tables,
                analysis,
            )

        # Pass 4: ORM query edges (read/write)
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            self._extract_orm_edges(
                file_record,
                tree,
                imports,
                node_index,
                model_tables,
                analysis,
            )

        analysis.nodes.sort(key=lambda n: n.id)
        analysis.edges.sort(key=lambda e: (e.source, e.target, e.kind))
        return analysis

    # ── models ──────────────────────────────────────────────

    def _is_django_model(
        self,
        class_stmt: ast.ClassDef,
        imports: ImportAliases,
    ) -> bool:
        for base in class_stmt.bases:
            base_name = call_name(base)
            if not base_name:
                continue
            resolved = resolve_alias(base_name, imports) or base_name
            if base_name in self._MODEL_BASES:
                return True
            if resolved.endswith(".Model") or resolved.endswith("models.Model"):
                return True
        return False

    def _extract_model(
        self,
        file_record: FileRecord,
        class_stmt: ast.ClassDef,
        imports: ImportAliases,
        node_index: NodeIndex,
        analysis: AdapterAnalysis,
    ) -> str | None:
        """Extract a Django Model class as a table node. Returns table node ID."""
        # Determine table name from Meta.db_table or default
        tbl_name = self._meta_db_table(class_stmt)
        if not tbl_name:
            # Django default: app_label + "_" + model_name.lower()
            # Derive app_label from module path (first component)
            app_label = file_record.module.split(".")[0]
            tbl_name = f"{app_label}_{class_stmt.name.lower()}"

        tbl_id = table_id(tbl_name)
        model_cls_id = class_id(f"{file_record.module}.{class_stmt.name}")

        analysis.nodes.append(
            Node(
                id=tbl_id,
                kind="table",
                name=tbl_name,
                qualname=tbl_name,
                path=file_record.path,
                start_line=class_stmt.lineno,
                end_line=getattr(class_stmt, "end_lineno", class_stmt.lineno),
                properties={
                    "backend": "django_orm",
                    "model_class": class_stmt.name,
                    "fields": self._model_fields(class_stmt),
                },
            )
        )
        analysis.edges.append(
            self._edge(
                model_cls_id,
                tbl_id,
                "maps_to",
                file_record,
                class_stmt,
                f"class {class_stmt.name} -> table {tbl_name}",
            )
        )
        return tbl_id

    @staticmethod
    def _meta_db_table(class_stmt: ast.ClassDef) -> str | None:
        """Read Meta.db_table if present."""
        for item in class_stmt.body:
            if isinstance(item, ast.ClassDef) and item.name == "Meta":
                for meta_item in item.body:
                    if not isinstance(meta_item, ast.Assign):
                        continue
                    for target in meta_item.targets:
                        if isinstance(target, ast.Name) and target.id == "db_table":
                            return literal_str(meta_item.value)
        return None

    @staticmethod
    def _model_fields(class_stmt: ast.ClassDef) -> list[str]:
        """Extract field names from model body assignments."""
        fields: list[str] = []
        for item in class_stmt.body:
            if not isinstance(item, ast.Assign):
                continue
            if not isinstance(item.value, ast.Call):
                continue
            func = call_name(item.value.func)
            if func and ("Field" in func or func.startswith("models.")):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        fields.append(target.id)
        return fields

    # ── URL patterns ────────────────────────────────────────

    def _extract_urlpatterns(
        self,
        file_record: FileRecord,
        tree: ast.Module,
        imports: ImportAliases,
        node_index: NodeIndex,
        analysis: AdapterAnalysis,
        module_names: set[str],
    ) -> None:
        """Scan for urlpatterns = [path(...), ...] at module level."""
        for stmt in tree.body:
            if not isinstance(stmt, ast.Assign):
                continue
            targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            if "urlpatterns" not in targets:
                continue
            if not isinstance(stmt.value, (ast.List, ast.Tuple)):
                # Could be urlpatterns += [...] or a function call
                continue
            for item in stmt.value.elts:
                self._process_url_call(
                    file_record,
                    item,
                    "",
                    imports,
                    node_index,
                    analysis,
                    module_names,
                )
            # Also handle augmented assignment: urlpatterns += [...]
        for stmt in tree.body:
            if isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id == "urlpatterns" and isinstance(
                    stmt.value, (ast.List, ast.Tuple)
                ):
                    for item in stmt.value.elts:
                        self._process_url_call(
                            file_record,
                            item,
                            "",
                            imports,
                            node_index,
                            analysis,
                            module_names,
                        )

    def _process_url_call(
        self,
        file_record: FileRecord,
        node: ast.AST,
        prefix: str,
        imports: ImportAliases,
        node_index: NodeIndex,
        analysis: AdapterAnalysis,
        module_names: set[str],
    ) -> None:
        """Process a single path()/re_path()/url() call."""
        if not isinstance(node, ast.Call):
            return
        func = call_name(node.func)
        if not func:
            return
        # Strip module prefix: django.urls.path -> path
        short = func.rsplit(".", 1)[-1] if "." in func else func
        if short not in self._URL_FUNCS:
            return
        # First arg is the route pattern
        pattern = literal_str(node.args[0]) if node.args else None
        if pattern is None:
            return

        full_path = self._combine_url(prefix, pattern)

        # Second arg or 'view' kwarg is the view
        view_arg = node.args[1] if len(node.args) > 1 else keyword(node, "view")

        # Check if this is an include()
        if isinstance(view_arg, ast.Call):
            inc_name = call_name(view_arg.func)
            if inc_name and inc_name.rsplit(".", 1)[-1] == "include":
                # include() — emit an includes_router edge if we can resolve the module
                if view_arg.args:
                    included = literal_str(view_arg.args[0])
                    if included:
                        target_id = f"mod:{included}"
                        existing_target = node_index.by_id.get(target_id)
                        resolved_module = included in module_names or (
                            existing_target is not None
                            and existing_target.properties.get("resolution_status")
                            != "unresolved"
                            and existing_target.properties.get("external_reference")
                            is not True
                        )
                        if not resolved_module and existing_target is None:
                            placeholder = Node(
                                id=target_id,
                                kind="module",
                                name=included.rsplit(".", 1)[-1],
                                qualname=included,
                                properties={
                                    "adapter": self.name,
                                    "external_reference": True,
                                    "resolution_status": "unresolved",
                                    "included_module": included,
                                },
                            )
                            analysis.nodes.append(placeholder)
                            node_index.by_id[target_id] = placeholder
                        analysis.edges.append(
                            self._edge(
                                module_node_id(file_record),
                                target_id,
                                "includes_router",
                                file_record,
                                node,
                                f"include({included!r})",
                                confidence=(
                                    "confirmed" if resolved_module else "unresolved"
                                ),
                                resolution=(
                                    None
                                    if resolved_module
                                    else FactResolution(
                                        status="unresolved",
                                        strategy="django_include_literal",
                                        candidate_count=0,
                                        detail=(
                                            "included module is outside the indexed "
                                            "module registry"
                                        ),
                                    )
                                ),
                            )
                        )
                return

        # Regular view — create route node
        # For CBV: UserView.as_view() -> extract full call chain name
        view_name: str | None = None
        if isinstance(view_arg, ast.Call):
            # e.g. UserView.as_view() — get the full name of the call func
            view_name = call_name(view_arg.func)
        elif view_arg is not None:
            view_name = call_name(view_arg)
        name_kwarg = literal_str(keyword(node, "name")) or view_name or pattern
        method = "ANY"  # Django URL conf doesn't specify method
        node_id = route_id(method, full_path)

        handler_id: str | None = None
        if view_name:
            resolved = resolve_alias(view_name, imports) or view_name
            # Could be a function view or CBV.as_view()
            if resolved.endswith(".as_view"):
                cls_name = resolved.rsplit(".as_view", 1)[0]
                handler_id = class_id(cls_name)
            else:
                handler_id = node_index.function_id_for(
                    view_name, file_record, imports
                ) or function_id(
                    (
                        f"{file_record.module}.{view_name}"
                        if "." not in view_name
                        else resolved
                    ),
                )

        analysis.nodes.append(
            Node(
                id=node_id,
                kind="route",
                name=f"{method} {full_path}",
                qualname=f"{method} {full_path}",
                path=file_record.path,
                start_line=getattr(node, "lineno", None),
                end_line=getattr(node, "end_lineno", getattr(node, "lineno", None)),
                properties={
                    "method": method,
                    "path": full_path,
                    "name": name_kwarg,
                    "handler": handler_id,
                    "backend": "django",
                },
            )
        )

        if handler_id:
            analysis.edges.append(
                self._edge(
                    node_id,
                    handler_id,
                    "invokes",
                    file_record,
                    node,
                    f"path({pattern!r}) -> {view_name}",
                )
            )

    @staticmethod
    def _combine_url(prefix: str, pattern: str) -> str:
        """Combine a URL prefix with a pattern."""
        if not prefix:
            result = f"/{pattern.lstrip('/')}" if pattern else "/"
        elif not pattern:
            result = f"/{prefix.strip('/')}"
        else:
            result = f"/{prefix.strip('/')}/{pattern.lstrip('/')}"
        return result.replace("//", "/")

    # ── admin registrations ─────────────────────────────────

    def _extract_admin(
        self,
        file_record: FileRecord,
        tree: ast.Module,
        imports: ImportAliases,
        node_index: NodeIndex,
        model_tables: dict[str, str],
        analysis: AdapterAnalysis,
    ) -> None:
        """Extract @admin.register(Model) decorators and admin.site.register() calls."""
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                for decorator in stmt.decorator_list:
                    self._admin_register_decorator(
                        file_record,
                        stmt,
                        decorator,
                        imports,
                        node_index,
                        model_tables,
                        analysis,
                    )
            # Also handle admin.site.register(Model, ModelAdmin)
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                func = call_name(stmt.value.func)
                if func and func.endswith("site.register"):
                    self._admin_site_register(
                        file_record,
                        stmt.value,
                        imports,
                        node_index,
                        model_tables,
                        analysis,
                    )

    def _admin_register_decorator(
        self,
        file_record: FileRecord,
        class_stmt: ast.ClassDef,
        decorator: ast.expr,
        imports: ImportAliases,
        node_index: NodeIndex,
        model_tables: dict[str, str],
        analysis: AdapterAnalysis,
    ) -> None:
        if not isinstance(decorator, ast.Call):
            return
        func = call_name(decorator.func)
        if not func:
            return
        short = func.rsplit(".", 1)[-1] if "." in func else func
        if short != "register" or not decorator.args:
            return
        # Could be admin.register or just register (imported)
        if "." in func and not func.endswith("admin.register"):
            resolved = resolve_alias(func, imports) or func
            if not resolved.endswith("admin.register"):
                return

        admin_cls_id = class_id(f"{file_record.module}.{class_stmt.name}")
        for arg in decorator.args:
            model_name = call_name(arg)
            if not model_name:
                continue
            tbl_id = self._resolve_table(model_name, imports, model_tables)
            if tbl_id:
                analysis.edges.append(
                    self._edge(
                        admin_cls_id,
                        tbl_id,
                        "reads",
                        file_record,
                        decorator,
                        f"@admin.register({model_name})",
                    )
                )

    def _admin_site_register(
        self,
        file_record: FileRecord,
        call: ast.Call,
        imports: ImportAliases,
        node_index: NodeIndex,
        model_tables: dict[str, str],
        analysis: AdapterAnalysis,
    ) -> None:
        if not call.args:
            return
        model_name = call_name(call.args[0])
        if not model_name:
            return
        tbl_id = self._resolve_table(model_name, imports, model_tables)
        admin_cls: str | None = None
        if len(call.args) > 1:
            admin_name = call_name(call.args[1])
            if admin_name:
                admin_cls = class_id(
                    (
                        f"{file_record.module}.{admin_name}"
                        if "." not in admin_name
                        else admin_name
                    ),
                )
        source = admin_cls or module_node_id(file_record)
        if tbl_id:
            analysis.edges.append(
                self._edge(
                    source,
                    tbl_id,
                    "reads",
                    file_record,
                    call,
                    f"admin.site.register({model_name})",
                )
            )

    # ── ORM query edges ─────────────────────────────────────

    def _extract_orm_edges(
        self,
        file_record: FileRecord,
        tree: ast.Module,
        imports: ImportAliases,
        node_index: NodeIndex,
        model_tables: dict[str, str],
        analysis: AdapterAnalysis,
    ) -> None:
        """Detect Model.objects.filter() / .create() etc. chains."""
        source_ranges = self._function_line_index(file_record, tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            method_name = node.func.attr
            is_read = method_name in self._MANAGER_METHODS_READ
            is_write = method_name in self._MANAGER_METHODS_WRITE
            if not is_read and not is_write:
                continue

            # Try to resolve Model.objects.method() or queryset.method()
            model_name = self._resolve_orm_model(node.func.value)
            if not model_name:
                continue
            tbl_id = self._resolve_table(model_name, imports, model_tables)
            if not tbl_id:
                continue

            source = self._source_for_line(source_ranges, node.lineno)
            if not source:
                source = module_node_id(file_record)

            kind = "reads" if is_read else "writes"
            analysis.edges.append(
                self._edge(
                    source,
                    tbl_id,
                    kind,
                    file_record,
                    node,
                    f"{model_name}.objects.{method_name}()",
                )
            )

    @staticmethod
    def _resolve_orm_model(node: ast.AST) -> str | None:
        """Try to extract Model name from Model.objects chain."""
        # Pattern: Model.objects.method()
        if isinstance(node, ast.Attribute) and node.attr == "objects":
            if isinstance(node.value, ast.Name):
                return node.value.id
        # Pattern: Model.objects.filter().method() (chained)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            return DjangoAdapter._resolve_orm_model(node.func.value)
        return None

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _resolve_table(
        model_name: str,
        imports: ImportAliases,
        model_tables: dict[str, str],
    ) -> str | None:
        """Resolve a model class name to its table node ID.

        Tries fully-qualified name via import aliases first (always
        unambiguous), then falls back to simple name (only works when no
        cross-app collision exists).
        """
        resolved = resolve_alias(model_name, imports)
        if resolved:
            tbl_id = model_tables.get(resolved)
            if tbl_id:
                return tbl_id
        return model_tables.get(model_name)

    @staticmethod
    def _is_django_import(stmt: ast.stmt) -> bool:
        if isinstance(stmt, ast.Import):
            return any(alias.name.startswith("django") for alias in stmt.names)
        if isinstance(stmt, ast.ImportFrom):
            return bool(stmt.module and stmt.module.startswith("django"))
        return False

    @staticmethod
    def _function_line_index(
        file_record: FileRecord,
        tree: ast.Module,
    ) -> list[tuple[int, int, str]]:
        ranges: list[tuple[int, int, str]] = []
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ranges.append(
                    (
                        stmt.lineno,
                        getattr(stmt, "end_lineno", stmt.lineno),
                        function_id(f"{file_record.module}.{stmt.name}"),
                    )
                )
            elif isinstance(stmt, ast.ClassDef):
                for child in stmt.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        qn = f"{file_record.module}.{stmt.name}.{child.name}"
                        ranges.append(
                            (
                                child.lineno,
                                getattr(child, "end_lineno", child.lineno),
                                method_id(qn),
                            )
                        )
        return ranges

    @staticmethod
    def _source_for_line(
        ranges: list[tuple[int, int, str]],
        line: int,
    ) -> str | None:
        for start, end, node_id in ranges:
            if start <= line <= end:
                return node_id
        return None

    @staticmethod
    def _edge(
        source: str,
        target: str,
        kind: str,
        file_record: FileRecord,
        node: ast.AST,
        detail: str,
        *,
        confidence: str = "confirmed",
        resolution: FactResolution | None = None,
    ) -> Edge:
        return Edge(
            source=source,
            target=target,
            kind=kind,
            confidence=confidence,
            resolution=resolution or FactResolution(),
            evidence=[
                Evidence(
                    kind="django_framework",
                    path=file_record.path,
                    start_line=getattr(node, "lineno", None),
                    end_line=getattr(
                        node,
                        "end_lineno",
                        getattr(node, "lineno", None),
                    ),
                    column=getattr(node, "col_offset", None),
                    detail=detail,
                )
            ],
        )
