"""Celery task and queue extraction for ArcGraph."""

from __future__ import annotations

import ast

from arcgraph.adapters.common import (
    AdapterAnalysis,
    ImportAliases,
    SemanticAdapter,
    call_name,
    import_aliases,
    keyword,
    literal_str,
    module_node_id,
    resolve_alias,
)
from arcgraph.core.ids import function_id, queue_id, worker_id
from arcgraph.core.schemas import Edge, Evidence, FactResolution, FileRecord, Node


class CeleryAdapter(SemanticAdapter):
    """Extract Celery tasks, queues, and enqueue edges."""

    name = "celery"
    capabilities = ("entrypoint:worker_task", "resource:queue", "edge:enqueues")
    required_facts = ("function",)

    _TASK_DECORATORS = {"task", "shared_task"}
    _ENQUEUE_METHODS = {"delay", "apply_async", "s", "si", "signature"}
    _SEND_FUNCTIONS = {"send_task"}

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
                if isinstance(stmt, ast.ImportFrom) and stmt.module:
                    if stmt.module.startswith("celery"):
                        return True
                if isinstance(stmt, ast.Import):
                    if any(a.name.startswith("celery") for a in stmt.names):
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
        del nodes
        analysis = AdapterAnalysis()

        # Pass 1: find all Celery app instances
        app_names: dict[str, set[str]] = {}  # file path -> set of app variable names
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            app_names[file_record.path] = self._find_celery_apps(tree)

        # Pass 2: extract @app.task / @shared_task decorated functions
        task_registry: dict[str, str] = {}  # task_name -> worker node id
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            apps = app_names.get(file_record.path, set())
            self._extract_tasks(
                file_record,
                tree,
                imports,
                apps,
                task_registry,
                analysis,
            )

        # Pass 3: find enqueue calls (task.delay(), task.apply_async(), send_task())
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            apps = app_names.get(file_record.path, set())
            self._extract_enqueue_edges(
                file_record,
                tree,
                imports,
                apps,
                task_registry,
                analysis,
            )

        analysis.nodes.sort(key=lambda n: n.id)
        analysis.edges.sort(key=lambda e: (e.source, e.target, e.kind))
        return analysis

    # ── celery apps ─────────────────────────────────────────

    @staticmethod
    def _find_celery_apps(tree: ast.Module) -> set[str]:
        """Find variable names assigned a Celery() instance."""
        apps: set[str] = set()
        for stmt in tree.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if not isinstance(stmt.value, ast.Call):
                continue
            func = call_name(stmt.value.func)
            if func and func.rsplit(".", 1)[-1] == "Celery":
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        apps.add(target.id)
        return apps

    # ── task extraction ─────────────────────────────────────

    def _extract_tasks(
        self,
        file_record: FileRecord,
        tree: ast.Module,
        imports: ImportAliases,
        apps: set[str],
        task_registry: dict[str, str],
        analysis: AdapterAnalysis,
    ) -> None:
        for stmt in tree.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            task_info = self._task_decorator_info(stmt, imports, apps)
            if task_info is None:
                continue

            task_name, queue_name, bind, decorator_node = task_info

            # Default task name follows Celery convention: module.function_name
            if not task_name:
                task_name = f"{file_record.module}.{stmt.name}"

            # Default queue
            if not queue_name:
                queue_name = "celery"

            q_id = queue_id(f"celery:{queue_name}")
            w_id = worker_id(f"celery:{task_name}")
            handler_id = function_id(f"{file_record.module}.{stmt.name}")

            task_registry[task_name] = w_id
            # Also register by short name for .delay() resolution
            task_registry[stmt.name] = w_id

            # Ensure queue node exists (dedup handled by AdapterRegistry)
            analysis.nodes.append(
                Node(
                    id=q_id,
                    kind="queue",
                    name=queue_name,
                    qualname=f"celery:{queue_name}",
                    path=file_record.path,
                    start_line=stmt.lineno,
                    end_line=getattr(stmt, "end_lineno", stmt.lineno),
                    properties={"backend": "celery"},
                )
            )

            analysis.nodes.append(
                Node(
                    id=w_id,
                    kind="worker_task",
                    name=stmt.name,
                    qualname=task_name,
                    path=file_record.path,
                    start_line=stmt.lineno,
                    end_line=getattr(stmt, "end_lineno", stmt.lineno),
                    properties={
                        "queue_name": queue_name,
                        "handler": handler_id,
                        "bind": bind,
                        "celery_task_name": task_name,
                    },
                )
            )

            # worker_task -> handler function
            analysis.edges.append(
                self._edge(
                    w_id,
                    handler_id,
                    "invokes",
                    file_record,
                    decorator_node,
                    f"@task {task_name} -> {stmt.name}",
                )
            )
            # worker_task -> queue
            analysis.edges.append(
                self._edge(
                    w_id,
                    q_id,
                    "consumes",
                    file_record,
                    decorator_node,
                    f"task {task_name} consumes queue {queue_name}",
                )
            )

    def _task_decorator_info(
        self,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
        imports: ImportAliases,
        apps: set[str],
    ) -> tuple[str | None, str | None, bool, ast.AST] | None:
        """Check if function has @app.task or @shared_task. Returns (name, queue, bind, node)."""
        for decorator in stmt.decorator_list:
            # @shared_task or @app.task (no-call form)
            if isinstance(decorator, ast.Name):
                resolved = resolve_alias(decorator.id, imports) or decorator.id
                short = resolved.rsplit(".", 1)[-1]
                if short in self._TASK_DECORATORS:
                    return None, None, False, decorator

            # @app.task or @shared_task (as attribute, no call)
            if isinstance(decorator, ast.Attribute):
                if (
                    isinstance(decorator.value, ast.Name)
                    and decorator.value.id in apps
                    and decorator.attr == "task"
                ):
                    return None, None, False, decorator

            # @shared_task(...) or @app.task(...)
            if isinstance(decorator, ast.Call):
                func = decorator.func
                is_task = False

                if isinstance(func, ast.Name):
                    resolved = resolve_alias(func.id, imports) or func.id
                    short = resolved.rsplit(".", 1)[-1]
                    is_task = short in self._TASK_DECORATORS
                elif isinstance(func, ast.Attribute):
                    if (
                        isinstance(func.value, ast.Name)
                        and func.value.id in apps
                        and func.attr == "task"
                    ):
                        is_task = True
                    elif func.attr == "shared_task":
                        is_task = True

                if is_task:
                    task_name = literal_str(keyword(decorator, "name"))
                    queue_name = literal_str(keyword(decorator, "queue"))
                    bind = self._bool_keyword(decorator, "bind")
                    return task_name, queue_name, bind, decorator

        return None

    # ── enqueue edges ───────────────────────────────────────

    def _extract_enqueue_edges(
        self,
        file_record: FileRecord,
        tree: ast.Module,
        imports: ImportAliases,
        apps: set[str],
        task_registry: dict[str, str],
        analysis: AdapterAnalysis,
    ) -> None:
        source_ranges = self._function_line_index(file_record, tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            target_id: str | None = None
            unresolved_external_task = False

            # Pattern 1: task_func.delay(...) / task_func.apply_async(...)
            if isinstance(node.func, ast.Attribute):
                method = node.func.attr
                if method in self._ENQUEUE_METHODS:
                    task_ref = call_name(node.func.value)
                    if task_ref:
                        target_id = task_registry.get(task_ref)
                        if target_id is None:
                            # Try resolving through imports
                            resolved = resolve_alias(task_ref, imports)
                            if resolved:
                                target_id = task_registry.get(resolved)

            # Pattern 2: app.send_task("task.name", ...)
            if target_id is None:
                func = call_name(node.func)
            else:
                func = None
            if func and node.args:
                short = func.rsplit(".", 1)[-1]
                if short in self._SEND_FUNCTIONS:
                    task_name = literal_str(node.args[0])
                    if task_name:
                        target_id = task_registry.get(task_name)
                        if target_id is None:
                            # Preserve the literal external reference without
                            # claiming that ArcGraph resolved its implementation.
                            target_id = worker_id(f"celery:{task_name}")
                            unresolved_external_task = True
                            if not any(
                                candidate.id == target_id
                                for candidate in analysis.nodes
                            ):
                                analysis.nodes.append(
                                    Node(
                                        id=target_id,
                                        kind="worker_task",
                                        name=task_name,
                                        qualname=task_name,
                                        properties={
                                            "adapter": self.name,
                                            "external_reference": True,
                                            "resolution_status": "unresolved",
                                            "task_name": task_name,
                                        },
                                    )
                                )

            if target_id is None:
                continue

            source = self._source_for_line(source_ranges, node.lineno)
            if not source:
                source = module_node_id(file_record)

            analysis.edges.append(
                self._edge(
                    source,
                    target_id,
                    "enqueues",
                    file_record,
                    node,
                    f"enqueue {call_name(node.func) or 'task'}",
                    confidence=(
                        "unresolved" if unresolved_external_task else "confirmed"
                    ),
                    resolution=(
                        FactResolution(
                            status="unresolved",
                            strategy="celery_send_task_literal",
                            candidate_count=0,
                            detail="literal task name is outside the indexed registry",
                        )
                        if unresolved_external_task
                        else None
                    ),
                )
            )

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _bool_keyword(call: ast.Call, name: str) -> bool:
        for kw in call.keywords:
            if kw.arg == name and isinstance(kw.value, ast.Constant):
                return bool(kw.value.value)
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
                    kind="celery_task",
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
