"""Optional SCIP/Pyright importers for precise reference evidence."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from arcgraph.analyzers.precision_positions import PrecisionPositions
from arcgraph.core.ids import callsite_id, class_id, module_id, type_ref_id
from arcgraph.core.schemas import BuildWarning, Edge, Evidence, Node
from arcgraph.core.utils import int_or_none


@dataclass
class PreciseReferenceAnalysis:
    edges: list[Edge] = field(default_factory=list)
    warnings: list[BuildWarning] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    status: str = "unavailable"


@dataclass(frozen=True)
class PrecisionInputConfig:
    """External precision input contract for Pyright/SCIP-derived facts."""

    scip_index_path: Path | None = None
    pyright_export_path: Path | None = None
    source_roots: tuple[str, ...] = ()
    package: str | None = None
    version: str | None = None


@dataclass
class _SourceAnalysis:
    edges: list[Edge] = field(default_factory=list)
    warnings: list[BuildWarning] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    status: str = "unavailable"
    payload: Any | None = None
    path: Path | None = None


class PreciseReferenceAnalyzer:
    """Import confirmed references, calls, definitions, and type facts.

    ArcGraph does not run Pyright or SCIP directly during graph builds.
    Production setups can generate either ArcGraph's small precision JSON
    contract or the stable JSON emitted by ``scip print --json`` and pass that
    file to the indexer. Invalid or missing optional inputs are surfaced as
    explicit capability downgrades while the AST/adapter graph remains
    available.
    """

    DEFAULT_CANDIDATES = (
        ".scip/index.json",
        "scip-index.json",
        "output/arcgraph/scip-index.json",
    )

    def __init__(
        self,
        repo_root: Path,
        scip_index_path: str | Path | None = None,
        *,
        pyright_export_path: str | Path | None = None,
        source_roots: tuple[str, ...] = (),
        package: str | None = None,
        version: str | None = None,
        input_config: PrecisionInputConfig | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.input_config = input_config or PrecisionInputConfig(
            scip_index_path=Path(scip_index_path) if scip_index_path else None,
            pyright_export_path=(
                Path(pyright_export_path) if pyright_export_path else None
            ),
            source_roots=tuple(source_roots),
            package=package,
            version=version,
        )

    def analyze(self, nodes: list[Node]) -> PreciseReferenceAnalysis:
        self._positions = PrecisionPositions(self.repo_root)
        node_by_id = {node.id: node for node in nodes}
        node_by_qualname = {node.qualname: node for node in nodes if node.qualname}

        scip = self._analyze_scip(nodes, node_by_id, node_by_qualname)
        pyright = self._analyze_pyright(nodes, node_by_id, node_by_qualname)
        status = self._combined_status([scip.status, pyright.status])
        metrics = {
            **self._base_metrics(
                status=status,
                scip_path=scip.path,
                pyright_path=pyright.path,
                payload=scip.payload if scip.payload is not None else pyright.payload,
            ),
            **self._combined_precision_metrics(scip.metrics, pyright.metrics),
            "scip_status": scip.status,
            "pyright_status": pyright.status,
        }
        return PreciseReferenceAnalysis(
            edges=sorted(
                [*scip.edges, *pyright.edges],
                key=lambda edge: (
                    edge.source,
                    edge.target,
                    edge.kind,
                    edge.semantic_role or "",
                ),
            ),
            warnings=[*scip.warnings, *pyright.warnings],
            metrics=metrics,
            status=status,
        )

    def _analyze_scip(
        self,
        nodes: list[Node],
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
    ) -> _SourceAnalysis:
        path = self._resolve_scip_path()
        if path is None:
            return _SourceAnalysis()
        if not path.exists():
            return _SourceAnalysis(
                warnings=[
                    BuildWarning(
                        kind="scip_missing",
                        path=self._display_path(path),
                        message=f"SCIP index file does not exist: {path}",
                    )
                ],
                metrics=self._empty_occurrence_metrics(),
                status="partial",
                path=path,
            )

        payload, warning = self._load_json(path, "scip")
        if warning is not None:
            return _SourceAnalysis(
                warnings=[warning],
                metrics=self._empty_occurrence_metrics(),
                status="partial",
                path=path,
            )

        edges, metrics, diagnostics = self._edges_from_scip_payload(
            payload, path, node_by_id, node_by_qualname
        )
        return _SourceAnalysis(
            edges=edges,
            warnings=diagnostics,
            metrics=metrics,
            status=(
                "partial"
                if any(
                    w.kind in {"scip_position_unmappable", "scip_callsite_unmatched"}
                    for w in diagnostics
                )
                else "available"
            ),
            payload=payload,
            path=path,
        )

    def _analyze_pyright(
        self,
        nodes: list[Node],
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
    ) -> _SourceAnalysis:
        path = self.input_config.pyright_export_path
        if path is None:
            return _SourceAnalysis()
        absolute = self._absolute_path(path)
        if not absolute.exists():
            return _SourceAnalysis(
                warnings=[
                    BuildWarning(
                        kind="pyright_missing",
                        path=self._display_path(absolute),
                        message=f"Pyright export file does not exist: {absolute}",
                    )
                ],
                metrics=self._empty_pyright_metrics(),
                status="partial",
                path=absolute,
            )

        payload, warning = self._load_json(absolute, "pyright")
        if warning is not None:
            return _SourceAnalysis(
                warnings=[warning],
                metrics=self._empty_pyright_metrics(),
                status="partial",
                path=absolute,
            )

        edges, metrics, diagnostics = self._edges_from_pyright_payload(
            payload,
            absolute,
            nodes,
            node_by_id,
            node_by_qualname,
        )
        status = (
            str(payload.get("status", "available"))
            if isinstance(payload, dict)
            else "available"
        )
        if status not in {"available", "partial", "unavailable"}:
            status = "available"
        if any(w.kind == "pyright_callsite_unmatched" for w in diagnostics):
            status = "partial"
        return _SourceAnalysis(
            edges=edges,
            warnings=diagnostics,
            metrics=metrics,
            status=status,
            payload=payload,
            path=absolute,
        )

    def _resolve_scip_path(self) -> Path | None:
        if self.input_config.scip_index_path is not None:
            return self._absolute_path(self.input_config.scip_index_path)
        for candidate in self.DEFAULT_CANDIDATES:
            path = self.repo_root / candidate
            if path.exists():
                return path
        return None

    def _edges_from_scip_payload(
        self,
        payload: Any,
        scip_path: Path,
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
    ) -> tuple[list[Edge], dict[str, Any], list[BuildWarning]]:
        edges: list[Edge] = []
        warnings = self._payload_diagnostics(payload, scip_path, prefix="scip")
        documents = self._documents(payload)
        nodes_by_path = self._nodes_by_path(node_by_id.values())
        occurrence_total = 0
        unresolved_occurrences = 0
        counts = self._edge_count_metrics()
        for document in documents:
            doc_path = self._document_path(document)
            path_nodes = nodes_by_path.get(doc_path or "", [])
            source_path = next(
                (node.path for node in path_nodes if node.path), doc_path
            )
            for occurrence in document.get("occurrences", []):
                if not isinstance(occurrence, dict):
                    continue
                occurrence_total += 1
                try:
                    occurrence = self._positions.normalize_scip(
                        occurrence, document, source_path or ""
                    )
                except ValueError as exc:
                    warnings.append(
                        BuildWarning(
                            kind="scip_position_unmappable",
                            path=doc_path or self._display_path(scip_path),
                            message=f"Line {self._record_start_line(occurrence)}: {exc}",
                        )
                    )
                    unresolved_occurrences += 1
                    continue
                occurrence = self._scip_record_with_source(
                    occurrence,
                    doc_path=doc_path,
                    node_by_id=node_by_id,
                    node_by_qualname=node_by_qualname,
                    nodes_by_path=nodes_by_path,
                )
                edge = self._edge_from_record(
                    occurrence,
                    node_by_id=node_by_id,
                    node_by_qualname=node_by_qualname,
                    path=doc_path or self._display_path(scip_path),
                    evidence_prefix="scip",
                    source_keys=("enclosing_symbol", "source", "container"),
                    target_keys=("symbol", "target", "arcgraph_symbol"),
                )
                if edge is None:
                    unresolved_occurrences += 1
                    continue
                self._callsite_diagnostic(edge, warnings, "scip")
                self._increment_edge_counts(counts, edge)
                edges.append(edge)
        resolved_occurrences = len(edges)
        metrics = {
            "documents_total": len(documents),
            "occurrences_total": occurrence_total,
            "resolved_occurrences": resolved_occurrences,
            "unresolved_occurrences": unresolved_occurrences,
            "coverage": (
                round(resolved_occurrences / occurrence_total, 4)
                if occurrence_total
                else 1.0
            ),
            **counts,
            "diagnostics": len(warnings),
        }
        return (
            sorted(edges, key=lambda edge: (edge.source, edge.target, edge.kind)),
            metrics,
            warnings,
        )

    def _edges_from_pyright_payload(
        self,
        payload: Any,
        pyright_path: Path,
        nodes: list[Node],
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
    ) -> tuple[list[Edge], dict[str, Any], list[BuildWarning]]:
        records = self._pyright_reference_records(payload)
        type_infos = self._pyright_type_infos(payload)
        warnings = self._payload_diagnostics(payload, pyright_path, prefix="pyright")
        edges: list[Edge] = []
        unresolved_records = 0
        counts = self._edge_count_metrics()
        for record in records:
            edge = self._edge_from_record(
                record,
                node_by_id=node_by_id,
                node_by_qualname=node_by_qualname,
                path=self._record_path(record, pyright_path),
                evidence_prefix="pyright",
                source_keys=("source", "enclosing_symbol", "container", "subject"),
                target_keys=("target", "symbol", "arcgraph_symbol", "type_id"),
            )
            if edge is None:
                unresolved_records += 1
                continue
            self._callsite_diagnostic(edge, warnings, "pyright")
            self._increment_edge_counts(counts, edge)
            edges.append(edge)

        resolved_type_infos = 0
        unresolved_type_infos = 0
        type_info_edges = 0
        for item in type_infos:
            attached = self._attach_pyright_type_info(
                item,
                nodes=nodes,
                node_by_id=node_by_id,
                node_by_qualname=node_by_qualname,
                pyright_path=pyright_path,
            )
            if attached:
                resolved_type_infos += 1
            else:
                unresolved_type_infos += 1
            edge = self._type_info_edge(
                item,
                node_by_id=node_by_id,
                node_by_qualname=node_by_qualname,
                pyright_path=pyright_path,
            )
            if edge is not None:
                type_info_edges += 1
                self._increment_edge_counts(counts, edge)
                edges.append(edge)

        resolved_records = len(records) - unresolved_records
        metrics = {
            "pyright_records_total": len(records),
            "pyright_resolved_records": resolved_records,
            "pyright_unresolved_records": unresolved_records,
            "pyright_type_info_total": len(type_infos),
            "pyright_resolved_type_info": resolved_type_infos,
            "pyright_unresolved_type_info": unresolved_type_infos,
            "pyright_type_info_edges": type_info_edges,
            "pyright_diagnostics": len(warnings),
            "pyright_lsp_probe_total": self._payload_lsp_int(payload, "probe_total"),
            "pyright_lsp_requestable_probe_total": self._payload_lsp_int(
                payload, "requestable_probe_total"
            ),
            "pyright_lsp_skipped_unmappable_total": self._payload_lsp_int(
                payload, "skipped_unmappable_total"
            ),
            "pyright_lsp_requests_total": self._payload_lsp_int(
                payload, "requests_total"
            ),
            "pyright_lsp_type_info_total": self._payload_lsp_int(
                payload, "type_info_total"
            ),
            "pyright_lsp_unresolved_total": self._payload_lsp_int(
                payload, "unresolved_total"
            ),
            **{f"pyright_{key}": value for key, value in counts.items()},
        }
        return (
            sorted(edges, key=lambda edge: (edge.source, edge.target, edge.kind)),
            metrics,
            warnings,
        )

    def _edge_from_record(
        self,
        record: dict[str, Any],
        *,
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
        path: str,
        evidence_prefix: str,
        source_keys: tuple[str, ...],
        target_keys: tuple[str, ...],
    ) -> Edge | None:
        source = self._resolve_node(
            self._first_record_value(record, source_keys),
            node_by_id,
            node_by_qualname,
        )
        target = self._resolve_node(
            self._first_record_value(record, target_keys),
            node_by_id,
            node_by_qualname,
        )
        if source is None or target is None:
            return None
        kind, evidence_kind, semantic_role = self._edge_classification(
            record, evidence_prefix, source=source, target=target
        )
        if kind == "defines" and source.id == target.id:
            return None
        matched_callsite = (
            self._matching_callsite(source, target, record) if kind == "calls" else None
        )
        resolution: dict[str, Any] = {
            "status": "resolved",
            "strategy": evidence_prefix,
            "candidate_count": 1,
        }
        if matched_callsite is not None:
            raw_name = str(matched_callsite.get("name") or target.name)
            resolution["callsite_id"] = callsite_id(
                source.id,
                source.path,
                int_or_none(matched_callsite.get("line")),
                int_or_none(matched_callsite.get("column")),
                raw_name,
            )
        elif kind == "calls":
            resolution["detail"] = (
                "No unique AST callsite matches this precision record"
            )
        properties = self._edge_properties(record, evidence_prefix)
        if matched_callsite is not None:
            fact = {
                "callsite_id": resolution["callsite_id"],
                "raw_expression": matched_callsite.get("name"),
                "call_expression": matched_callsite.get("call_expression"),
                "context": matched_callsite.get("context"),
                "path": source.path,
                "line": matched_callsite.get("line"),
                "column": matched_callsite.get("column"),
                "edge_kind": kind,
                "resolution_strategy": evidence_prefix,
                "candidate_count": 1,
            }
            properties.update(callsite=fact, callsites=[fact])
        return Edge(
            source=source.id,
            target=target.id,
            kind=kind,
            confidence="confirmed",
            semantic_role=semantic_role,
            resolution=resolution,
            evidence=[
                Evidence(
                    kind=evidence_kind,
                    path=path,
                    start_line=self._record_start_line(record),
                    end_line=self._record_end_line(record),
                    column=self._record_column(record),
                    detail=self._record_detail(record),
                )
            ],
            properties=properties,
        )

    def _type_info_edge(
        self,
        item: dict[str, Any],
        *,
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
        pyright_path: Path,
    ) -> Edge | None:
        source = self._resolve_node(
            self._first_record_value(item, ("subject", "source", "scope")),
            node_by_id,
            node_by_qualname,
        )
        target = self._resolve_node(
            self._first_record_value(item, ("target", "type_id", "symbol_id")),
            node_by_id,
            node_by_qualname,
        )
        if source is None or target is None or source.id == target.id:
            return None
        return Edge(
            source=source.id,
            target=target.id,
            kind="references",
            confidence="confirmed",
            semantic_role="type_info",
            evidence=[
                Evidence(
                    kind="pyright_type_info",
                    path=self._record_path(item, pyright_path),
                    start_line=self._record_start_line(item),
                    end_line=self._record_end_line(item),
                    column=self._record_column(item),
                    detail=self._record_detail(item),
                )
            ],
            properties=self._edge_properties(item, "pyright"),
        )

    def _attach_pyright_type_info(
        self,
        item: dict[str, Any],
        *,
        nodes: list[Node],
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
        pyright_path: Path,
    ) -> bool:
        subject = self._resolve_node(
            self._first_record_value(item, ("subject", "source", "scope")),
            node_by_id,
            node_by_qualname,
        )
        if subject is None:
            return False

        type_id = self._string_or_none(
            item.get("type_id") or item.get("target") or item.get("symbol_id")
        )
        type_expression = self._string_or_none(
            item.get("type") or item.get("type_expression") or type_id
        )
        if type_expression is None and type_id is None:
            return False

        name = self._string_or_none(item.get("name")) or subject.name
        line = self._record_start_line(item)
        column = self._record_column(item)
        path = self._record_path(item, pyright_path)
        record: dict[str, Any] = {
            "type_ref_id": type_ref_id(
                subject.id,
                name,
                "pyright_type_info",
                "pyright",
                path,
                line,
                column,
                type_expression or type_id,
            ),
            "scope_id": subject.id,
            "scope_kind": subject.kind,
            "name": name,
            "subject_kind": "pyright_type_info",
            "strategy": "pyright",
            "confidence": "confirmed",
            "path": path,
            "line": line,
            "column": column,
            "source_expression": self._string_or_none(item.get("expression")) or name,
            "type_expression": type_expression or type_id,
            "resolution_status": "resolved" if type_id else "partial",
            "fallbacks": ["pyright"],
            "evidence": Evidence(
                kind="pyright_type_info",
                path=path,
                start_line=line,
                end_line=self._record_end_line(item) or line,
                column=column,
                detail=name,
            ).model_dump(exclude_none=True),
        }
        self._set_optional(record, "type_id", type_id)
        self._set_optional(
            record,
            "symbol_id",
            self._string_or_none(item.get("symbol_id")) or type_id,
        )
        self._set_optional(record, "origin", self._string_or_none(item.get("origin")))

        for node in nodes:
            if node.id != subject.id:
                continue
            existing = [
                value
                for value in node.properties.get("type_refs", [])
                if isinstance(value, dict)
            ]
            if not any(
                value.get("type_ref_id") == record["type_ref_id"] for value in existing
            ):
                existing.append(record)
            node.properties["type_refs"] = sorted(
                existing,
                key=lambda value: (
                    str(value.get("path") or ""),
                    int(value.get("line") or 0),
                    int(value.get("column") or 0),
                    str(value.get("name") or ""),
                    str(value.get("strategy") or ""),
                ),
            )
            return True
        return False

    @staticmethod
    def _documents(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict) and isinstance(payload.get("documents"), list):
            return [item for item in payload["documents"] if isinstance(item, dict)]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    @staticmethod
    def _pyright_reference_records(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            records: list[dict[str, Any]] = []
            for key in ("references", "edges", "occurrences"):
                values = payload.get(key, [])
                if isinstance(values, list):
                    records.extend(item for item in values if isinstance(item, dict))
            return records
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    @staticmethod
    def _pyright_type_infos(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        values = payload.get("type_info") or payload.get("types") or []
        if not isinstance(values, list):
            return []
        return [item for item in values if isinstance(item, dict)]

    @staticmethod
    def _document_path(document: dict[str, Any]) -> str | None:
        value = (
            document.get("relative_path")
            or document.get("relativePath")
            or document.get("path")
            or document.get("filename")
        )
        return value.replace("\\", "/") if isinstance(value, str) and value else None

    def _record_path(self, record: dict[str, Any], fallback: Path) -> str:
        value = (
            record.get("path") or record.get("relative_path") or record.get("filename")
        )
        if isinstance(value, str) and value:
            return value.replace("\\", "/")
        return self._display_path(fallback)

    def _resolve_node(
        self,
        value: Any,
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
    ) -> Node | None:
        if not isinstance(value, str) or not value:
            return None
        for candidate in self._symbol_candidates(value):
            if candidate in node_by_id:
                return node_by_id[candidate]
            if candidate in node_by_qualname:
                return node_by_qualname[candidate]
        return None

    def _symbol_candidates(self, value: str) -> list[str]:
        candidates: list[str] = []

        def add(candidate: str | None) -> None:
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        add(value)
        add(value.rstrip("#()."))

        qualname = self._scip_symbol_qualname(value)
        for variant in self._qualname_variants(qualname):
            add(variant)
            add(f"method:{variant}")
            add(f"fn:{variant}")
            add(f"class:{variant}")
            add(f"mod:{variant}")
            if variant.endswith(".__init__"):
                module_variant = variant.removesuffix(".__init__")
                add(module_variant)
                add(f"mod:{module_variant}")
        return candidates

    def _qualname_variants(self, qualname: str | None) -> list[str]:
        if not qualname:
            return []
        variants = [qualname]
        prefixes = [
            root.replace("\\", "/").strip("/").replace("/", ".")
            for root in self.input_config.source_roots
            if root
        ]
        for prefix in prefixes:
            if qualname == prefix:
                continue
            if qualname.startswith(f"{prefix}."):
                variants.append(qualname[len(prefix) + 1 :])
        return list(dict.fromkeys(variants))

    @staticmethod
    def _scip_symbol_qualname(value: str) -> str | None:
        if value.startswith("local "):
            return None
        descriptor = value
        parts = value.split(" ", 4)
        if len(parts) == 5:
            descriptor = parts[4]
        if not any(marker in descriptor for marker in ("/", "#", ":", "().", "`")):
            return None
        descriptor = unquote(descriptor).replace("`", "")
        descriptor = re.sub(r"\([^)]*\)\.", ".", descriptor)
        descriptor = descriptor.rstrip(":")
        descriptor = descriptor.replace("/", ".").replace("#", ".")
        descriptor = descriptor.replace(".py.", ".")
        if descriptor.endswith(".py"):
            descriptor = descriptor[:-3]
        descriptor = descriptor.rstrip(".")
        descriptor = re.sub(r"\.+", ".", descriptor)
        if not descriptor:
            return None
        return descriptor

    @staticmethod
    def _first_record_value(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            value = record.get(key)
            if value:
                return value
        return None

    @staticmethod
    def _is_definition(record: dict[str, Any]) -> bool:
        role = (
            record.get("role") or record.get("symbol_role") or record.get("symbolRole")
        )
        roles = (
            record.get("roles")
            or record.get("symbol_roles")
            or record.get("symbolRoles")
        )
        if isinstance(role, str) and role.lower() == "definition":
            return True
        if isinstance(roles, list):
            return any(str(item).lower() == "definition" for item in roles)
        if isinstance(roles, int):
            return bool(roles & 1)
        if isinstance(role, int):
            return bool(role & 1)
        return False

    def _edge_classification(
        self,
        record: dict[str, Any],
        evidence_prefix: str,
        *,
        source: Node | None = None,
        target: Node | None = None,
    ) -> tuple[str, str, str | None]:
        labels = PreciseReferenceAnalyzer._record_labels(record)
        if PreciseReferenceAnalyzer._is_definition(record):
            return "defines", f"{evidence_prefix}_definition", "definition"
        if labels & {"implementation", "implementations", "implements"}:
            return "implements", f"{evidence_prefix}_implementation", "implementation"
        if labels & {"type", "type_occurrence", "type_reference", "typeref"}:
            return (
                "references",
                f"{evidence_prefix}_type_occurrence",
                "type_occurrence",
            )
        if labels & {"call", "calls"}:
            return "calls", f"{evidence_prefix}_call", "call"
        if (
            evidence_prefix == "scip"
            and source is not None
            and target is not None
            and self._matching_callsite(source, target, record) is not None
        ):
            return "calls", f"{evidence_prefix}_call", "call"
        return "references", f"{evidence_prefix}_reference", "reference"

    @staticmethod
    def _record_labels(record: dict[str, Any]) -> set[str]:
        values = [
            record.get("kind"),
            record.get("edge_kind"),
            record.get("role"),
            record.get("symbol_role"),
            record.get("symbolRole"),
        ]
        tags = record.get("tags")
        if isinstance(tags, list):
            values.extend(tags)
        roles = (
            record.get("roles")
            or record.get("symbol_roles")
            or record.get("symbolRoles")
        )
        if isinstance(roles, list):
            values.extend(roles)
        return {str(value).lower() for value in values if value is not None}

    @staticmethod
    def _edge_properties(record: dict[str, Any], source: str) -> dict[str, Any]:
        properties: dict[str, Any] = {"precision_source": source}
        for key in (
            "symbol",
            "enclosing_symbol",
            "symbol_roles",
            "symbolRoles",
            "syntax_kind",
            "syntaxKind",
            "source",
            "target",
            "type",
            "type_expression",
        ):
            value = record.get(key)
            if isinstance(value, str) and value:
                properties[key] = value
            elif isinstance(value, int):
                properties[key] = value
        return properties

    def _scip_record_with_source(
        self,
        record: dict[str, Any],
        *,
        doc_path: str | None,
        node_by_id: dict[str, Node],
        node_by_qualname: dict[str, Node],
        nodes_by_path: dict[str, list[Node]],
    ) -> dict[str, Any]:
        if self._first_record_value(
            record, ("enclosing_symbol", "source", "container")
        ):
            return record

        source_id: str | None = None
        target = self._resolve_node(
            self._first_record_value(record, ("symbol", "target", "arcgraph_symbol")),
            node_by_id,
            node_by_qualname,
        )
        if target is not None and self._is_definition(record):
            source_id = self._definition_container_id(target, node_by_id)
        if source_id is None:
            source_id = self._enclosing_source_id(doc_path, record, nodes_by_path)
        if source_id is None:
            return record
        return {**record, "enclosing_symbol": source_id}

    @staticmethod
    def _definition_container_id(
        target: Node, node_by_id: dict[str, Node]
    ) -> str | None:
        if not target.qualname:
            return None
        if target.kind == "method":
            parent = ".".join(target.qualname.split(".")[:-1])
            parent_id = class_id(parent)
        elif target.kind in {"class", "function"}:
            parent = ".".join(target.qualname.split(".")[:-1])
            parent_id = module_id(parent)
        else:
            return None
        return parent_id if parent_id in node_by_id else None

    def _nodes_by_path(self, nodes: Any) -> dict[str, list[Node]]:
        by_path: dict[str, list[Node]] = {}
        for node in nodes:
            path = node.path
            if not isinstance(path, str) or not path:
                continue
            for alias in self._path_aliases(path):
                by_path.setdefault(alias, []).append(node)
        for path, path_nodes in by_path.items():
            unique_nodes = list({node.id: node for node in path_nodes}.values())
            by_path[path] = sorted(
                unique_nodes,
                key=lambda node: (
                    0 if node.kind == "module" else 1,
                    -(node.end_line or node.start_line or 0) + (node.start_line or 0),
                ),
            )
        return by_path

    def _path_aliases(self, path: str) -> list[str]:
        normalized = path.replace("\\", "/").strip("/")
        aliases = [normalized]
        for root in self.input_config.source_roots:
            prefix = root.replace("\\", "/").strip("/")
            if prefix and normalized.startswith(f"{prefix}/"):
                aliases.append(normalized[len(prefix) + 1 :])
        return list(dict.fromkeys(alias for alias in aliases if alias))

    def _enclosing_source_id(
        self,
        doc_path: str | None,
        record: dict[str, Any],
        nodes_by_path: dict[str, list[Node]],
    ) -> str | None:
        if not doc_path:
            return None
        path_nodes = nodes_by_path.get(doc_path.replace("\\", "/"), [])
        if not path_nodes:
            return None
        line = self._record_start_line(record)
        if line is None:
            return self._module_node_id(path_nodes)
        candidates: list[Node] = []
        for node in path_nodes:
            if node.kind == "module":
                continue
            start = node.start_line or 0
            end = node.end_line or start
            if start <= line <= end:
                candidates.append(node)
        if candidates:
            candidates.sort(
                key=lambda node: (
                    (node.end_line or line) - (node.start_line or line),
                    node.kind != "function" and node.kind != "method",
                    node.id,
                )
            )
            return candidates[0].id
        return self._module_node_id(path_nodes)

    @staticmethod
    def _module_node_id(path_nodes: list[Node]) -> str | None:
        for node in path_nodes:
            if node.kind == "module":
                return node.id
        return None

    def _matching_callsite(
        self, source: Node, target: Node, record: dict[str, Any]
    ) -> dict[str, Any] | None:
        callsites = source.properties.get("callsites", [])
        if not isinstance(callsites, list):
            return None
        line = self._record_start_line(record)
        column = self._record_column(record)
        if line is None and column is not None:
            return None
        target_names = {target.name}
        if target.qualname:
            target_names.add(target.qualname.rsplit(".", 1)[-1])
        matches = []
        for callsite in callsites:
            if not isinstance(callsite, dict):
                continue
            raw_name = str(callsite.get("name") or "")
            if not raw_name:
                continue
            call_column = int_or_none(callsite.get("column"))
            call_line = int_or_none(callsite.get("line"))
            if line is not None and column is not None:
                if call_line is None or call_column is None or source.path is None:
                    continue
                span = self._positions.callee_span(source.path, call_line, call_column)
                if span is None or (line, column) != span[:2]:
                    continue
                value = record.get("range")
                if isinstance(value, list) and len(value) in {3, 4}:
                    end = (self._record_end_line(record), value[-1])
                    if end != span[2:]:
                        continue
                matches.append(callsite)
                continue
            if line is not None and call_line != line:
                continue
            call_targets = {raw_name.rsplit(".", 1)[-1]}
            attribute = callsite.get("attribute")
            if isinstance(attribute, str) and attribute:
                call_targets.add(attribute)
            if target_names & call_targets:
                matches.append(callsite)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _callsite_diagnostic(
        edge: Edge, warnings: list[BuildWarning], prefix: str
    ) -> None:
        if edge.kind == "calls" and edge.resolution.callsite_id is None:
            warnings.append(
                BuildWarning(
                    kind=f"{prefix}_callsite_unmatched",
                    path=edge.evidence[0].path,
                    message=(
                        f"Line {edge.evidence[0].start_line}: "
                        f"{edge.resolution.detail or 'Callsite could not be matched'}"
                    ),
                )
            )

    def _payload_diagnostics(
        self, payload: Any, path: Path, *, prefix: str
    ) -> list[BuildWarning]:
        diagnostics: list[BuildWarning] = []
        if not isinstance(payload, dict):
            return diagnostics
        for item in payload.get("diagnostics", []):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or f"{prefix}_diagnostic")
            message = str(item.get("message") or f"{prefix} diagnostic")
            diagnostic_path = item.get("path") or self._display_path(path)
            diagnostics.append(
                BuildWarning(
                    kind=kind if kind.startswith(f"{prefix}_") else f"{prefix}_{kind}",
                    path=(
                        diagnostic_path
                        if isinstance(diagnostic_path, str)
                        else self._display_path(path)
                    ),
                    message=message,
                )
            )
        return diagnostics

    def _load_json(
        self, path: Path, prefix: str
    ) -> tuple[Any | None, BuildWarning | None]:
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except (OSError, json.JSONDecodeError) as exc:
            return None, BuildWarning(
                kind=f"{prefix}_parse_error",
                path=self._display_path(path),
                message=str(exc),
            )

    def _base_metrics(
        self,
        *,
        status: str,
        scip_path: Path | None = None,
        pyright_path: Path | None = None,
        payload: Any | None = None,
    ) -> dict[str, Any]:
        payload_source_roots: tuple[str, ...] = ()
        payload_package: str | None = None
        payload_version: str | None = None
        if isinstance(payload, dict):
            roots = payload.get("source_roots")
            if isinstance(roots, list):
                payload_source_roots = tuple(str(item) for item in roots if item)
            package = payload.get("package")
            if isinstance(package, dict):
                payload_package = self._string_or_none(package.get("name"))
                payload_version = self._string_or_none(package.get("version"))
            else:
                payload_package = self._string_or_none(package)
            payload_version = payload_version or self._string_or_none(
                payload.get("version")
            )
        configured_pyright = self.input_config.pyright_export_path
        return {
            "status": status,
            "scip_index_path": self._display_path(scip_path) if scip_path else None,
            "pyright_export_path": (
                self._display_path(
                    pyright_path or self._absolute_path(configured_pyright)
                )
                if configured_pyright or pyright_path
                else None
            ),
            "source_roots": list(
                self.input_config.source_roots or payload_source_roots
            ),
            "package": self.input_config.package or payload_package,
            "version": self.input_config.version or payload_version,
        }

    @staticmethod
    def _combined_status(statuses: list[str]) -> str:
        if "partial" in statuses:
            return "partial"
        if "available" in statuses:
            return "available"
        return "unavailable"

    @classmethod
    def _combined_precision_metrics(
        cls, scip: dict[str, Any], pyright: dict[str, Any]
    ) -> dict[str, Any]:
        scip_records = int(scip.get("occurrences_total", 0) or 0)
        pyright_records = int(pyright.get("pyright_records_total", 0) or 0)
        scip_resolved = int(scip.get("resolved_occurrences", 0) or 0)
        pyright_resolved = int(pyright.get("pyright_resolved_records", 0) or 0)
        occurrence_total = scip_records + pyright_records
        resolved_occurrences = scip_resolved + pyright_resolved
        unresolved_occurrences = int(scip.get("unresolved_occurrences", 0) or 0) + int(
            pyright.get("pyright_unresolved_records", 0) or 0
        )
        diagnostics = int(scip.get("diagnostics", 0) or 0) + int(
            pyright.get("pyright_diagnostics", 0) or 0
        )
        counts = {
            key: int(scip.get(key, 0) or 0) + int(pyright.get(f"pyright_{key}", 0) or 0)
            for key in cls._edge_count_metrics()
        }
        combined = {f"scip_{key}": value for key, value in scip.items()}
        combined.update(pyright)
        combined.update(
            {
                "documents_total": int(scip.get("documents_total", 0) or 0),
                "occurrences_total": occurrence_total,
                "resolved_occurrences": resolved_occurrences,
                "unresolved_occurrences": unresolved_occurrences,
                "coverage": (
                    round(resolved_occurrences / occurrence_total, 4)
                    if occurrence_total
                    else 1.0
                ),
                **counts,
                "diagnostics": diagnostics,
            }
        )
        return combined

    @staticmethod
    def _edge_count_metrics() -> dict[str, int]:
        return {
            "definitions": 0,
            "references": 0,
            "calls": 0,
            "implementations": 0,
            "type_occurrences": 0,
        }

    @staticmethod
    def _increment_edge_counts(counts: dict[str, int], edge: Edge) -> None:
        if edge.semantic_role == "definition":
            counts["definitions"] += 1
        elif edge.semantic_role == "call":
            counts["calls"] += 1
        elif edge.semantic_role == "implementation":
            counts["implementations"] += 1
        elif edge.semantic_role in {"type_occurrence", "type_info"}:
            counts["type_occurrences"] += 1
        else:
            counts["references"] += 1

    @classmethod
    def _empty_occurrence_metrics(cls) -> dict[str, Any]:
        return {
            "documents_total": 0,
            "occurrences_total": 0,
            "resolved_occurrences": 0,
            "unresolved_occurrences": 0,
            "coverage": 1.0,
            **cls._edge_count_metrics(),
            "diagnostics": 0,
        }

    @staticmethod
    def _empty_pyright_metrics() -> dict[str, Any]:
        return {
            "pyright_records_total": 0,
            "pyright_resolved_records": 0,
            "pyright_unresolved_records": 0,
            "pyright_type_info_total": 0,
            "pyright_resolved_type_info": 0,
            "pyright_unresolved_type_info": 0,
            "pyright_type_info_edges": 0,
            "pyright_diagnostics": 0,
            "pyright_lsp_probe_total": 0,
            "pyright_lsp_requestable_probe_total": 0,
            "pyright_lsp_skipped_unmappable_total": 0,
            "pyright_lsp_requests_total": 0,
            "pyright_lsp_type_info_total": 0,
            "pyright_lsp_unresolved_total": 0,
        }

    @staticmethod
    def _payload_lsp_int(payload: Any, key: str) -> int:
        if not isinstance(payload, dict):
            return 0
        lsp = payload.get("lsp")
        if not isinstance(lsp, dict):
            return 0
        value = lsp.get(key)
        return int(value) if isinstance(value, int) else 0

    @staticmethod
    def _record_detail(record: dict[str, Any]) -> str | None:
        value = (
            record.get("detail")
            or record.get("symbol")
            or record.get("target")
            or record.get("type")
            or record.get("type_expression")
        )
        return str(value) if value is not None else None

    @classmethod
    def _record_start_line(cls, record: dict[str, Any]) -> int | None:
        return cls._start_line(record.get("range")) or int_or_none(
            record.get("start_line") or record.get("line")
        )

    @classmethod
    def _record_end_line(cls, record: dict[str, Any]) -> int | None:
        return cls._end_line(record.get("range")) or int_or_none(record.get("end_line"))

    @classmethod
    def _record_column(cls, record: dict[str, Any]) -> int | None:
        column = cls._column(record.get("range"))
        return column if column is not None else int_or_none(record.get("column"))

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        return str(value) if value else None

    @staticmethod
    def _start_line(value: Any) -> int | None:
        if isinstance(value, list) and value and isinstance(value[0], int):
            return value[0] + 1
        return None

    @staticmethod
    def _end_line(value: Any) -> int | None:
        if isinstance(value, list) and len(value) in {3, 4}:
            line = value[0] if len(value) == 3 else value[2]
            if isinstance(line, int):
                return line + 1
        return None

    @staticmethod
    def _column(value: Any) -> int | None:
        if isinstance(value, list) and len(value) >= 2 and isinstance(value[1], int):
            return value[1]
        return None

    @staticmethod
    def _set_optional(record: dict[str, Any], key: str, value: Any | None) -> None:
        if value is not None:
            record[key] = value

    def _absolute_path(self, path: Path) -> Path:
        return path if path.is_absolute() else self.repo_root / path

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)
