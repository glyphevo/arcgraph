"""Shared helpers for framework adapters."""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Iterable

from arcgraph.core.ids import class_id, function_id, method_id, module_id
from arcgraph.core.schemas import (
    BuildWarning,
    Confidence,
    Edge,
    Evidence,
    FileRecord,
    Node,
)


@dataclass
class AdapterAnalysis:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    warnings: list[BuildWarning] = field(default_factory=list)
    diagnostics: list[BuildWarning] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class SemanticAdapter:
    """Stable extension contract for framework and DSL-specific extraction."""

    name = "semantic_adapter"
    capabilities: tuple[str, ...] = ()
    required_facts: tuple[str, ...] = ()

    def detect(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> bool:
        del files, parsed_files, nodes, module_names
        return True

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        raise NotImplementedError


class AdapterRegistry:
    """Run semantic adapters with incremental graph context and failure isolation."""

    def __init__(
        self,
        adapters: Iterable[SemanticAdapter],
        *,
        dedupe_nodes: Callable[[list[Node]], list[Node]] | None = None,
    ) -> None:
        self._adapters = tuple(adapters)
        self._dedupe_nodes = dedupe_nodes or self._stable_dedupe_nodes

    @property
    def adapters(self) -> tuple[SemanticAdapter, ...]:
        return self._adapters

    def analyze(
        self,
        files: list[FileRecord],
        parsed_files: dict[str, ast.Module],
        nodes: list[Node],
        module_names: set[str],
    ) -> AdapterAnalysis:
        aggregate = AdapterAnalysis(metrics={"adapters": {}})
        context = self._dedupe_nodes(list(nodes))
        adapter_metrics: dict[str, dict[str, Any]] = aggregate.metrics["adapters"]

        for adapter in self._adapters:
            name = adapter.name
            try:
                if not adapter.detect(files, parsed_files, context, module_names):
                    adapter_metrics[name] = {
                        "status": "skipped",
                        "nodes": 0,
                        "edges": 0,
                        "warnings": 0,
                    }
                    continue
                analysis = adapter.analyze(files, parsed_files, context, module_names)
            except Exception as exc:
                aggregate.warnings.append(
                    BuildWarning(
                        kind="adapter_error",
                        message=f"{name}: {exc.__class__.__name__}: {exc}",
                    )
                )
                adapter_metrics[name] = {
                    "status": "error",
                    "nodes": 0,
                    "edges": 0,
                    "warnings": 1,
                }
                continue

            aggregate.nodes.extend(analysis.nodes)
            aggregate.edges.extend(analysis.edges)
            aggregate.warnings.extend(analysis.warnings)
            aggregate.warnings.extend(analysis.diagnostics)
            unresolved_registrations = sum(
                1
                for node in analysis.nodes
                if node.properties.get("resolution_status") == "unresolved"
            )
            metrics = {
                "discovered_registrations": (
                    len(analysis.nodes) - unresolved_registrations
                ),
                "resolved_handlers": self._resolved_handler_count(analysis.edges),
                "unresolved_registrations": unresolved_registrations,
                **analysis.metrics,
            }
            adapter_metrics[name] = {
                "status": "available",
                "nodes": len(analysis.nodes),
                "edges": len(analysis.edges),
                "warnings": len(analysis.warnings) + len(analysis.diagnostics),
                **metrics,
            }
            context = self._dedupe_nodes([*context, *analysis.nodes])

        return aggregate

    @staticmethod
    def _resolved_handler_count(edges: list[Edge]) -> int:
        return sum(
            1
            for edge in edges
            if edge.kind
            in {
                "invokes",
                "provides",
                "injects",
                "calls",
                "maps_to",
                "reads",
                "writes",
                "enqueues",
                "consumes",
                "declares",
                "configures",
                "logs",
            }
            and edge.resolution.status == "resolved"
            and edge.confidence != "unresolved"
        )

    @staticmethod
    def _stable_dedupe_nodes(nodes: list[Node]) -> list[Node]:
        deduped: dict[str, Node] = {}
        for node in nodes:
            deduped.setdefault(node.id, node)
        return list(deduped.values())


@dataclass
class ImportAliases:
    aliases: dict[str, str] = field(default_factory=dict)
    modules: dict[str, str] = field(default_factory=dict)


class NodeIndex:
    def __init__(self, nodes: Iterable[Node]) -> None:
        self.by_id = {node.id: node for node in nodes}
        self.classes_by_name: dict[str, list[Node]] = {}
        self.functions_by_name: dict[str, list[Node]] = {}
        self.methods_by_class_and_name: dict[tuple[str, str], Node] = {}
        for node in nodes:
            if node.kind == "class":
                self.classes_by_name.setdefault(node.name, []).append(node)
            elif node.kind == "function":
                self.functions_by_name.setdefault(node.name, []).append(node)
            elif node.kind == "method" and node.qualname:
                class_qualname = node.qualname.rsplit(".", 1)[0]
                self.methods_by_class_and_name[(class_qualname, node.name)] = node

    def function_id_for(
        self, name: str, file_record: FileRecord, imports: ImportAliases
    ) -> str | None:
        qualname = resolve_name(name, file_record, imports)
        candidates = [function_id(qualname)]
        if "." not in name:
            candidates.append(function_id(f"{file_record.module}.{name}"))
        for candidate in candidates:
            if candidate in self.by_id:
                return candidate
        same_name = self.functions_by_name.get(name.rsplit(".", 1)[-1], [])
        if len(same_name) == 1:
            return same_name[0].id
        return None

    def class_for_annotation(
        self, annotation: str | None, file_record: FileRecord, imports: ImportAliases
    ) -> Node | None:
        if not annotation:
            return None
        name = clean_annotation_name(annotation)
        if not name:
            return None
        qualname = resolve_name(name, file_record, imports)
        candidate = self.by_id.get(class_id(qualname))
        if candidate:
            return candidate
        matches = self.classes_by_name.get(name.rsplit(".", 1)[-1], [])
        if len(matches) == 1:
            return matches[0]
        return None

    def method_for_class(self, class_node: Node, method_name: str) -> Node | None:
        if not class_node.qualname:
            return None
        return self.methods_by_class_and_name.get((class_node.qualname, method_name))


def import_aliases(
    file_record: FileRecord, tree: ast.Module, module_names: set[str]
) -> ImportAliases:
    aliases: dict[str, str] = {}
    modules: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                aliases[local] = alias.name
                modules[local] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = resolve_import_from_base(file_record, node)
            if not base:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                candidate = f"{base}.{alias.name}"
                aliases[local] = candidate
                modules[local] = candidate if candidate in module_names else base
    return ImportAliases(aliases=aliases, modules=modules)


def resolve_name(name: str, file_record: FileRecord, imports: ImportAliases) -> str:
    if not name:
        return name
    parts = name.split(".")
    if parts[0] in imports.aliases:
        return ".".join([imports.aliases[parts[0]], *parts[1:]])
    if "." in name:
        return name
    return f"{file_record.module}.{name}"


def resolve_alias(name: str | None, imports: ImportAliases) -> str | None:
    if not name:
        return None
    parts = name.split(".")
    if parts[0] in imports.aliases:
        return ".".join([imports.aliases[parts[0]], *parts[1:]])
    return name


def resolve_module_name(name: str, imports: ImportAliases) -> str | None:
    parts = name.split(".")
    if parts[0] in imports.modules:
        resolved = imports.modules[parts[0]]
        return ".".join([resolved, *parts[1:-1]]) if len(parts) > 1 else resolved
    return None


def resolve_import_from_base(
    file_record: FileRecord, node: ast.ImportFrom
) -> str | None:
    if node.level == 0:
        return node.module

    module_parts = file_record.module.split(".")
    package_parts = module_parts if file_record.is_package else module_parts[:-1]
    keep_count = len(package_parts) - node.level + 1
    if keep_count < 0:
        return None

    base_parts = package_parts[:keep_count]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(part for part in base_parts if part)


def iter_symbol_defs(
    tree: ast.Module,
) -> Iterable[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, Callable[[str], str]]]:
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield stmt, stmt.name, function_id
        elif isinstance(stmt, ast.ClassDef):
            class_qualname = stmt.name
            for child in stmt.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield child, f"{class_qualname}.{child.name}", method_id


def symbol_id_for_def(
    file_record: FileRecord, qualified_name: str, id_factory: Callable[[str], str]
) -> str:
    return id_factory(f"{file_record.module}.{qualified_name}")


def call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def literal_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def literal_bool(node: ast.AST | None) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def literal_str_list(node: ast.AST | None) -> list[str]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [value for item in node.elts if (value := literal_str(item))]
    value = literal_str(node)
    return [value] if value else []


def keyword(call: ast.Call, name: str) -> ast.AST | None:
    for item in call.keywords:
        if item.arg == name:
            return item.value
    return None


def make_adapter_edge(
    source: str,
    target: str,
    kind: str,
    confidence: Confidence,
    evidence_kind: str,
    file_record: FileRecord,
    node: ast.AST,
    detail: str,
    *,
    semantic_role: str | None = None,
) -> Edge:
    return Edge(
        source=source,
        target=target,
        kind=kind,
        confidence=confidence,
        semantic_role=semantic_role or f"{kind}:{evidence_kind}",
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


def annotation_name(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Attribute):
        base = annotation_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Subscript):
        root = annotation_name(node.value)
        if root and root.rsplit(".", 1)[-1] == "Annotated":
            if isinstance(node.slice, ast.Tuple) and node.slice.elts:
                return annotation_name(node.slice.elts[0])
        return annotation_name(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return annotation_name(node.left) or annotation_name(node.right)
    return unparse(node)


def clean_annotation_name(name: str) -> str:
    cleaned = name.strip("\"'")
    if "|" in cleaned:
        cleaned = cleaned.split("|", 1)[0].strip()
    if cleaned.startswith("Optional[") and cleaned.endswith("]"):
        cleaned = cleaned.removeprefix("Optional[").removesuffix("]")
    return cleaned.strip("\"'")


def unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return node.__class__.__name__


def combine_paths(prefix: str, path: str) -> str:
    if not prefix:
        combined = path or "/"
    elif not path:
        combined = prefix
    else:
        combined = f"{prefix.rstrip('/')}/{path.lstrip('/')}"
    if not combined.startswith("/"):
        combined = f"/{combined}"
    return combined.replace("//", "/")


def module_node_id(file_record: FileRecord) -> str:
    return module_id(file_record.module)
