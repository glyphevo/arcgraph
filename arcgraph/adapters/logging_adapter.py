"""Python logging and structlog semantic extraction."""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass

from arcgraph.adapters.common import (
    AdapterAnalysis,
    ImportAliases,
    SemanticAdapter,
    call_name,
    import_aliases,
    iter_symbol_defs,
    literal_str,
    make_adapter_edge,
    module_node_id,
    resolve_alias,
    symbol_id_for_def,
)
from arcgraph.core.ids import class_id, log_sink_id
from arcgraph.core.schemas import Edge, FileRecord, Node


@dataclass(frozen=True)
class LoggerBinding:
    name: str
    call: ast.Call
    source_id: str


class LoggingAdapter(SemanticAdapter):
    name = "python_logging"
    capabilities = ("resource:log_sink", "edge:logs")
    required_facts = ("module", "function", "method")

    LOGGER_FACTORIES = {
        "logging.getLogger",
        "logging.get_logger",
        "structlog.get_logger",
        "structlog.getLogger",
    }
    LOG_METHODS = {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "log",
    }

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        del nodes
        analysis = AdapterAnalysis()
        sinks: dict[str, Node] = {}
        binding_count = 0
        log_call_count = 0

        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            imports = import_aliases(file_record, tree, module_names)
            bindings = self._logger_bindings(file_record, tree, imports)
            for binding in self._all_bindings(bindings):
                sink_node = self._sink_node(
                    binding.call, file_record, binding.call.lineno, sinks
                )
                analysis.nodes.append(sink_node)
                analysis.edges.append(
                    self._edge(
                        binding.source_id,
                        sink_node.id,
                        file_record,
                        binding.call,
                        f"logger binding {binding.name}",
                        "logs:binding",
                    )
                )
                binding_count += 1

            for stmt, qualified_name, id_factory in iter_symbol_defs(tree):
                source_id = symbol_id_for_def(file_record, qualified_name, id_factory)
                for edge, sink_node in self._log_edges(
                    source_id, stmt, file_record, imports, bindings, sinks
                ):
                    analysis.nodes.append(sink_node)
                    analysis.edges.append(edge)
                    log_call_count += 1

        analysis.nodes.sort(key=lambda node: node.id)
        analysis.edges.sort(key=lambda edge: (edge.source, edge.target, edge.kind))
        analysis.metrics = {
            "logger_bindings": binding_count,
            "log_calls": log_call_count,
            "discovered_registrations": binding_count,
            "resolved_handlers": log_call_count,
            "unresolved_registrations": 0,
        }
        return analysis

    def _logger_bindings(
        self,
        file_record: FileRecord,
        tree: ast.Module,
        imports: ImportAliases,
    ) -> dict[str, list[LoggerBinding]]:
        bindings: dict[str, list[LoggerBinding]] = {}

        def add_binding(name: str, call: ast.Call, source_id: str) -> None:
            bindings.setdefault(name, []).append(
                LoggerBinding(name=name, call=call, source_id=source_id)
            )

        module_source = module_node_id(file_record)
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                class_source = class_id(f"{file_record.module}.{stmt.name}")
                for child in stmt.body:
                    if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self._collect_logger_binding(
                            child, imports, class_source, add_binding
                        )
            elif not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._collect_logger_binding(stmt, imports, module_source, add_binding)

        for stmt, qualified_name, id_factory in iter_symbol_defs(tree):
            source_id = symbol_id_for_def(file_record, qualified_name, id_factory)
            for node in ast.walk(stmt):
                self._collect_logger_binding(node, imports, source_id, add_binding)
        return bindings

    def _collect_logger_binding(
        self,
        node: ast.AST,
        imports: ImportAliases,
        source_id: str,
        add_binding: Callable[[str, ast.Call, str], None],
    ) -> None:
        value: ast.AST | None = None
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            value = node.value
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            value = node.value
            targets = [node.target]
        if not isinstance(value, ast.Call):
            return
        factory = resolve_alias(call_name(value.func), imports)
        if factory not in self.LOGGER_FACTORIES:
            return
        for target in targets:
            if name := call_name(target):
                add_binding(name, value, source_id)

    @staticmethod
    def _all_bindings(
        bindings: dict[str, list[LoggerBinding]],
    ) -> list[LoggerBinding]:
        items: list[LoggerBinding] = []
        for values in bindings.values():
            items.extend(values)
        return items

    def _log_edges(
        self,
        source_id: str,
        stmt: ast.AST,
        file_record: FileRecord,
        imports: ImportAliases,
        bindings: dict[str, list[LoggerBinding]],
        sinks: dict[str, Node],
    ) -> list[tuple[Edge, Node]]:
        edges: list[tuple[Edge, Node]] = []
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call):
                continue
            func_name = call_name(node.func)
            if not func_name or "." not in func_name:
                continue
            receiver, level = func_name.rsplit(".", 1)
            if level not in self.LOG_METHODS:
                continue
            resolved_receiver = resolve_alias(receiver, imports)
            if not self._is_logger_receiver(
                receiver, resolved_receiver, source_id, file_record, bindings
            ):
                continue
            sink_name = self._sink_name_for_receiver(
                receiver, resolved_receiver, source_id, file_record, bindings
            )
            sink = self._sink_node(sink_name, file_record, node.lineno, sinks)
            edges.append(
                (
                    self._edge(
                        source_id,
                        sink.id,
                        file_record,
                        node,
                        f"{func_name}(...)",
                        f"logs:{level}",
                    ),
                    sink,
                )
            )
        return edges

    def _sink_name_for_receiver(
        self,
        receiver: str,
        resolved_receiver: str | None,
        source_id: str,
        file_record: FileRecord,
        bindings: dict[str, list[LoggerBinding]],
    ) -> str:
        binding = self._binding_for_receiver(receiver, source_id, file_record, bindings)
        if binding is not None:
            return (
                self._logger_name_from_call(file_record, binding.call)
                or file_record.module
            )
        if resolved_receiver in {"logging", "structlog"}:
            return file_record.module
        return f"{file_record.module}.{receiver}"

    @staticmethod
    def _logger_name_from_call(file_record: FileRecord, call: ast.Call) -> str | None:
        if not call.args:
            return file_record.module
        first_arg = call.args[0]
        if isinstance(first_arg, ast.Name) and first_arg.id == "__name__":
            return file_record.module
        return literal_str(first_arg)

    def _is_logger_receiver(
        self,
        receiver: str,
        resolved_receiver: str | None,
        source_id: str,
        file_record: FileRecord,
        bindings: dict[str, list[LoggerBinding]],
    ) -> bool:
        if self._binding_for_receiver(receiver, source_id, file_record, bindings):
            return True
        if resolved_receiver in {"logging", "structlog"}:
            return True
        lowered = receiver.lower()
        return lowered.endswith("logger") or lowered in {"log", "logger"}

    @staticmethod
    def _binding_for_receiver(
        receiver: str,
        source_id: str,
        file_record: FileRecord,
        bindings: dict[str, list[LoggerBinding]],
    ) -> LoggerBinding | None:
        candidates = list(bindings.get(receiver, []))
        short_name = receiver.rsplit(".", 1)[-1]
        if short_name != receiver:
            candidates.extend(bindings.get(short_name, []))
        if not candidates:
            return None
        for candidate_source in LoggingAdapter._source_candidates(
            source_id, file_record
        ):
            for binding in candidates:
                if binding.source_id == candidate_source:
                    return binding
        return candidates[0]

    @staticmethod
    def _source_candidates(source_id: str, file_record: FileRecord) -> list[str]:
        candidates = [source_id]
        if source_id.startswith("method:"):
            qualname = source_id.removeprefix("method:")
            if "." in qualname:
                candidates.append(class_id(qualname.rsplit(".", 1)[0]))
        candidates.append(module_node_id(file_record))
        return candidates

    @staticmethod
    def _sink_node(
        name: str | ast.Call,
        file_record: FileRecord,
        start_line: int | None,
        sinks: dict[str, Node],
    ) -> Node:
        if isinstance(name, ast.Call):
            sink_name = LoggingAdapter._logger_name_from_call(file_record, name)
        else:
            sink_name = name
        sink_name = sink_name or file_record.module
        node_id = log_sink_id(sink_name)
        if node_id not in sinks:
            sinks[node_id] = Node(
                id=node_id,
                kind="log_sink",
                name=sink_name,
                qualname=sink_name,
                path=file_record.path,
                start_line=start_line,
                properties={"framework": "python_logging"},
            )
        return sinks[node_id]

    @staticmethod
    def _edge(
        source: str,
        target: str,
        file_record: FileRecord,
        node: ast.AST,
        detail: str,
        semantic_role: str,
    ) -> Edge:
        return make_adapter_edge(
            source=source,
            target=target,
            kind="logs",
            confidence="heuristic",
            evidence_kind="python_logging",
            file_record=file_record,
            node=node,
            detail=detail,
            semantic_role=semantic_role,
        )
