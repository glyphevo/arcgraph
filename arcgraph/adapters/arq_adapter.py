"""ARQ worker extraction for ArcGraph."""

from __future__ import annotations

import ast

from arcgraph.adapters.common import (
    AdapterAnalysis,
    SemanticAdapter,
    call_name,
    keyword,
    literal_str,
    module_node_id,
)
from arcgraph.core.ids import function_id, queue_id, worker_id
from arcgraph.core.schemas import Edge, Evidence, FactResolution, FileRecord, Node


class ARQAdapter(SemanticAdapter):
    name = "arq"
    capabilities = ("entrypoint:worker_task", "resource:queue", "edge:enqueues")
    required_facts = ("function",)

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        del module_names
        analysis = AdapterAnalysis()
        concrete_node_ids = {node.id for node in nodes}
        emitted_node_ids = set(concrete_node_ids)
        worker_targets: dict[tuple[str, str], str] = {}
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            constants = self._string_constants(tree)
            for stmt in tree.body:
                if isinstance(stmt, ast.ClassDef) and stmt.name == "WorkerSettings":
                    self._analyze_worker_settings(
                        file_record,
                        stmt,
                        constants,
                        analysis,
                        worker_targets,
                        concrete_node_ids,
                        emitted_node_ids,
                    )

        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            self._enqueue_edges(
                file_record,
                tree,
                self._string_constants(tree),
                worker_targets,
                analysis,
                emitted_node_ids,
            )

        analysis.nodes.sort(key=lambda node: node.id)
        analysis.edges.sort(key=lambda edge: (edge.source, edge.target, edge.kind))
        return analysis

    def _analyze_worker_settings(
        self,
        file_record: FileRecord,
        stmt: ast.ClassDef,
        constants: dict[str, str],
        analysis: AdapterAnalysis,
        worker_targets: dict[tuple[str, str], str],
        concrete_node_ids: set[str],
        emitted_node_ids: set[str],
    ) -> None:
        queue_name = "arq:default"
        functions: list[tuple[str, ast.AST, bool]] = []
        for item in stmt.body:
            if not isinstance(item, ast.Assign):
                continue
            target_names = [
                target.id for target in item.targets if isinstance(target, ast.Name)
            ]
            if "queue_name" in target_names:
                queue_name = self._string_value(item.value, constants) or queue_name
            elif "functions" in target_names:
                functions.extend(
                    (name, item, False) for name in self._function_names(item.value)
                )
            elif "cron_jobs" in target_names:
                functions.extend(
                    (name, item, True) for name in self._cron_function_names(item.value)
                )

        queue_node_id = queue_id(queue_name)
        analysis.nodes.append(
            Node(
                id=queue_node_id,
                kind="queue",
                name=queue_name,
                qualname=queue_name,
                path=file_record.path,
                start_line=stmt.lineno,
                end_line=getattr(stmt, "end_lineno", stmt.lineno),
                properties={"backend": "arq"},
            )
        )
        unique_functions: dict[str, tuple[ast.AST, bool]] = {}
        for function_name, evidence_node, scheduled in functions:
            existing = unique_functions.get(function_name)
            if existing is None or (scheduled and not existing[1]):
                unique_functions[function_name] = (evidence_node, scheduled)

        for function_name, (evidence_node, scheduled) in unique_functions.items():
            task_id = worker_id(f"{queue_name}:{function_name}")
            handler_id = function_id(f"{file_record.module}.{function_name}")
            worker_targets[(queue_name, function_name)] = task_id
            handler_resolved = handler_id in concrete_node_ids
            if not handler_resolved and handler_id not in emitted_node_ids:
                analysis.nodes.append(
                    Node(
                        id=handler_id,
                        kind="function",
                        name=function_name.rsplit(".", 1)[-1],
                        qualname=f"{file_record.module}.{function_name}",
                        properties={
                            "adapter": self.name,
                            "external_reference": True,
                            "resolution_status": "unresolved",
                        },
                    )
                )
                emitted_node_ids.add(handler_id)
            analysis.nodes.append(
                Node(
                    id=task_id,
                    kind="worker_task",
                    name=function_name,
                    qualname=f"{queue_name}:{function_name}",
                    path=file_record.path,
                    start_line=getattr(evidence_node, "lineno", stmt.lineno),
                    end_line=getattr(evidence_node, "end_lineno", stmt.lineno),
                    properties={
                        "queue_name": queue_name,
                        "handler": handler_id,
                        "scheduled": scheduled,
                    },
                )
            )
            analysis.edges.append(
                self._edge(
                    task_id,
                    handler_id,
                    "invokes",
                    file_record,
                    evidence_node,
                    f"WorkerSettings.{'cron_jobs' if scheduled else 'functions'} -> {function_name}",
                    confidence="confirmed" if handler_resolved else "unresolved",
                    resolution=(
                        None
                        if handler_resolved
                        else FactResolution(
                            status="unresolved",
                            strategy="arq_worker_settings_literal",
                            candidate_count=0,
                            detail="worker handler is outside the indexed function registry",
                        )
                    ),
                )
            )
            analysis.edges.append(
                self._edge(
                    task_id,
                    queue_node_id,
                    "consumes",
                    file_record,
                    evidence_node,
                    f"WorkerSettings.queue_name = {queue_name}",
                )
            )

    def _enqueue_edges(
        self,
        file_record: FileRecord,
        tree: ast.Module,
        constants: dict[str, str],
        worker_targets: dict[tuple[str, str], str],
        analysis: AdapterAnalysis,
        node_ids: set[str],
    ) -> None:
        source_stack = self._function_line_index(file_record, tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node.func)
            if not (
                (name is not None and name.endswith(".enqueue_job"))
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "enqueue_job"
                )
            ):
                continue
            if not node.args:
                continue
            task_name = self._string_value(node.args[0], constants)
            if not task_name:
                continue
            queue_name = self._string_value(keyword(node, "_queue_name"), constants)
            target = worker_targets.get((queue_name, task_name)) if queue_name else None
            resolved_target = target is not None
            if target is None and queue_name:
                target = worker_id(f"{queue_name}:{task_name}")
            elif target is None:
                matches = {
                    target_id
                    for (_, worker_name), target_id in worker_targets.items()
                    if worker_name == task_name
                }
                target = next(iter(matches)) if len(matches) == 1 else None
                resolved_target = target is not None
            if target is None:
                target = worker_id(f"arq:external:{queue_name or '*'}:{task_name}")
            if not resolved_target and target not in node_ids:
                analysis.nodes.append(
                    Node(
                        id=target,
                        kind="worker_task",
                        name=task_name,
                        qualname=f"{queue_name or '*'}:{task_name}",
                        properties={
                            "adapter": self.name,
                            "external_reference": True,
                            "resolution_status": "unresolved",
                            "queue_name": queue_name,
                        },
                    )
                )
                node_ids.add(target)
            source = self._source_for_line(source_stack, node.lineno)
            source = source or module_node_id(file_record)
            if source:
                analysis.edges.append(
                    self._edge(
                        source,
                        target,
                        "enqueues",
                        file_record,
                        node,
                        f"enqueue_job({task_name})",
                        confidence="confirmed" if resolved_target else "unresolved",
                        resolution=(
                            None
                            if resolved_target
                            else FactResolution(
                                status="unresolved",
                                strategy="arq_enqueue_job_literal",
                                candidate_count=0,
                                detail=(
                                    "literal task name is outside the indexed "
                                    "ARQ worker registry"
                                ),
                            )
                        ),
                    )
                )

    @staticmethod
    def _string_constants(tree: ast.Module) -> dict[str, str]:
        constants: dict[str, str] = {}
        for stmt in tree.body:
            if not isinstance(stmt, ast.Assign):
                continue
            value = literal_str(stmt.value)
            if value is None:
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value
        return constants

    def _string_value(
        self, node: ast.AST | None, constants: dict[str, str]
    ) -> str | None:
        value = literal_str(node)
        if value is not None:
            return value
        if isinstance(node, ast.Name):
            return constants.get(node.id)
        return None

    @staticmethod
    def _function_names(node: ast.AST) -> list[str]:
        if not isinstance(node, (ast.List, ast.Tuple)):
            return []
        return [name for item in node.elts if (name := call_name(item))]

    @staticmethod
    def _cron_function_names(node: ast.AST) -> list[str]:
        if not isinstance(node, (ast.List, ast.Tuple)):
            return []
        names: list[str] = []
        for item in node.elts:
            if (
                isinstance(item, ast.Call)
                and call_name(item.func) == "cron"
                and item.args
            ):
                name = call_name(item.args[0])
                if name:
                    names.append(name)
        return names

    @staticmethod
    def _function_line_index(
        file_record: FileRecord, tree: ast.Module
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
    def _source_for_line(ranges: list[tuple[int, int, str]], line: int) -> str | None:
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
            confidence=confidence,  # type: ignore[arg-type]
            resolution=resolution or FactResolution(),
            evidence=[
                Evidence(
                    kind="arq_worker",
                    path=file_record.path,
                    start_line=getattr(node, "lineno", None),
                    end_line=getattr(node, "end_lineno", getattr(node, "lineno", None)),
                    column=getattr(node, "col_offset", None),
                    detail=detail,
                )
            ],
        )
