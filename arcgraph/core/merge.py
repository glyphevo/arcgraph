"""Evidence merge engine for semantic graph artifacts."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from arcgraph.core.ids import diagnostic_id
from arcgraph.core.passes import AnalysisPassContext, PassDispatcher
from arcgraph.core.semantic import (
    COMPAT_FRONTEND_NAME,
    DEFAULT_REPO_ID,
    ensure_node_canonical_identities,
    merge_metrics_from_graph,
    schema_migration_records,
)
from arcgraph.core.semantic_metrics import unresolved_callsite_diagnostics
from arcgraph.core.schemas import (
    CONFIDENCE_RANK,
    BuildWarning,
    Edge,
    Evidence,
    IndexMetadata,
    MergeMetrics,
    Node,
    SchemaMigrationRecord,
    SemanticDiagnostic,
    SemanticFact,
    evidence_key,
)


@dataclass(slots=True)
class MergeArtifacts:
    nodes: list[Node]
    edges: list[Edge]
    semantic_facts: list[SemanticFact]
    diagnostics: list[SemanticDiagnostic]
    merge_metrics: MergeMetrics
    schema_migrations: list[SchemaMigrationRecord]
    pass_metrics: dict[str, Any]


class GraphIntegrityError(RuntimeError):
    """Raised when a merged edge does not reference two materialized nodes."""


class EvidenceMergeEngine:
    """Centralize graph dedupe, evidence merge, and semantic artifacts."""

    def __init__(
        self,
        repo_root: Path,
        *,
        repo_id: str = DEFAULT_REPO_ID,
        dispatcher: PassDispatcher | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.repo_id = repo_id
        self.dispatcher = dispatcher or PassDispatcher()

    def merge_graph(
        self,
        metadata: IndexMetadata,
        nodes: list[Node],
        edges: list[Edge],
        warnings: list[BuildWarning],
        *,
        changed_files: tuple[str, ...] = (),
        full_rebuild: bool = True,
        extra_diagnostics: list[SemanticDiagnostic] | None = None,
    ) -> MergeArtifacts:
        merged_nodes = self.dedupe_nodes(nodes)
        ensure_node_canonical_identities(
            merged_nodes,
            repo_root=self.repo_root,
            project_name=metadata.project_name,
            project_version=metadata.project_version,
        )
        merged_edges = self.dedupe_edges(edges)
        self._validate_edge_endpoints(merged_nodes, merged_edges)
        context = AnalysisPassContext(
            metadata=metadata,
            repo_root=self.repo_root,
            repo_id=self.repo_id,
            nodes=merged_nodes,
            edges=merged_edges,
            warnings=warnings,
            changed_files=changed_files,
            full_rebuild=full_rebuild,
        )
        pass_results = self.dispatcher.run(context)
        diagnostics = [
            *pass_results.diagnostics,
            *(extra_diagnostics or []),
            *self._conflict_diagnostics(metadata, merged_edges),
            *unresolved_callsite_diagnostics(
                metadata,
                merged_nodes,
                merged_edges,
                repo_id=self.repo_id,
                frontend_name=COMPAT_FRONTEND_NAME,
            ),
        ]
        merge_metrics = merge_metrics_from_graph(
            metadata,
            pass_results.facts,
            merged_nodes,
            merged_edges,
            diagnostics,
            repo_id=self.repo_id,
        )
        return MergeArtifacts(
            nodes=merged_nodes,
            edges=merged_edges,
            semantic_facts=pass_results.facts,
            diagnostics=diagnostics,
            merge_metrics=merge_metrics,
            schema_migrations=schema_migration_records(metadata),
            pass_metrics=pass_results.metrics,
        )

    @staticmethod
    def _validate_edge_endpoints(nodes: list[Node], edges: list[Edge]) -> None:
        node_ids = {node.id for node in nodes}
        dangling = sorted(
            {
                endpoint
                for edge in edges
                for endpoint in (edge.source, edge.target)
                if endpoint not in node_ids
            }
        )
        if dangling:
            sample = ", ".join(repr(value) for value in dangling[:5])
            suffix = "" if len(dangling) <= 5 else ", ..."
            raise GraphIntegrityError(
                f"merged graph has {len(dangling)} dangling edge endpoint(s): "
                f"{sample}{suffix}"
            )

    @staticmethod
    def dedupe_nodes(nodes: list[Node]) -> list[Node]:
        deduped: dict[str, Node] = {}
        for node in nodes:
            node = EvidenceMergeEngine._config_resource(node)
            existing = deduped.get(node.id)
            if existing is None or EvidenceMergeEngine._node_resolution_rank(
                node
            ) > EvidenceMergeEngine._node_resolution_rank(existing):
                deduped[node.id] = node
        return sorted(deduped.values(), key=lambda node: node.id)

    @staticmethod
    def _config_resource(node: Node) -> Node:
        """Resource identity is shared; reference locations belong to edges.

        Normalize before dedupe and semantic passes so producer order and
        incremental retention cannot select a fictitious resource definition.
        """
        if node.kind != "config":
            return node
        if node.id.startswith("config:env:"):
            key = node.properties.get("key", node.id.removeprefix("config:env:"))
            properties = {"config_kind": "env", "key": key}
        elif node.id.startswith("config:browser_storage:"):
            properties = {
                key: node.properties[key]
                for key in ("config_kind", "storage", "key")
                if key in node.properties
            }
        else:
            return node
        properties.update(
            {
                "frontend_name": "shared-config-resource",
                "frontend_version": "1",
                "language": "neutral",
            }
        )
        return node.model_copy(
            update={
                "name": node.id.removeprefix("config:"),
                "qualname": node.id.removeprefix("config:"),
                "path": None,
                "start_line": None,
                "end_line": None,
                "properties": properties,
            }
        )

    @staticmethod
    def _node_resolution_rank(node: Node) -> tuple[int, int]:
        """Prefer a concrete definition over an older unresolved placeholder."""

        unresolved = (
            node.properties.get("resolution_status") == "unresolved"
            or node.properties.get("external_reference") is True
        )
        return (0 if unresolved else 1, 1 if node.path else 0)

    @classmethod
    def dedupe_edges(cls, edges: list[Edge]) -> list[Edge]:
        deduped: dict[tuple[Any, ...], Edge] = {}
        owned: set[tuple[Any, ...]] = set()
        for edge in edges:
            candidate = cls._normalized_edge(edge)
            key = cls._edge_merge_key(candidate)
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = candidate
                if candidate is not edge:
                    owned.add(key)
                continue
            # The fast path keeps non-colliding, already-normalized edges by
            # reference. Copy only when this accumulator is about to be mutated
            # so callers' input models retain the old non-mutating contract.
            if key not in owned:
                existing = existing.model_copy(deep=True)
                deduped[key] = existing
                owned.add(key)
            cls._merge_edge(existing, candidate)
        return sorted(
            deduped.values(),
            key=lambda edge: (
                edge.source,
                edge.target,
                edge.kind,
                edge.semantic_role or "",
                edge.confidence,
            ),
        )

    @staticmethod
    def has_evidence(edge: Edge, evidence: Evidence) -> bool:
        candidate_key = evidence_key(evidence)
        return any(evidence_key(item) == candidate_key for item in edge.evidence)

    @staticmethod
    def _edge_merge_key(edge: Edge) -> tuple[Any, ...]:
        if edge.kind == "similar_to":
            return (
                edge.source,
                edge.target,
                edge.kind,
                edge.properties.get("algorithm", "ast_similarity"),
                edge.properties.get("profile_version", "v1"),
            )
        return (edge.source, edge.target, edge.kind, edge.semantic_role)

    @classmethod
    def _normalized_edge(cls, edge: Edge) -> Edge:
        if edge.kind != "similar_to" or edge.source <= edge.target:
            return edge
        candidate = edge.model_copy(deep=True)
        candidate.source, candidate.target = candidate.target, candidate.source
        source_hash = candidate.properties.get("source_structure_hash")
        target_hash = candidate.properties.get("target_structure_hash")
        if source_hash is not None or target_hash is not None:
            cls._set_optional_property(
                candidate.properties,
                "source_structure_hash",
                target_hash,
            )
            cls._set_optional_property(
                candidate.properties,
                "target_structure_hash",
                source_hash,
            )
        return candidate

    @staticmethod
    def _set_optional_property(
        properties: dict[str, Any], key: str, value: Any | None
    ) -> None:
        if value is None:
            properties.pop(key, None)
        else:
            properties[key] = value

    @classmethod
    def _merge_edge(cls, existing: Edge, candidate: Edge) -> None:
        if candidate.kind == "similar_to":
            cls._merge_similarity_edge(existing, candidate)
            return
        if candidate.kind == "imports":
            cls._merge_import_properties(existing, candidate)
        cls._merge_callsite_properties(existing, candidate)
        cls._merge_route_registration_properties(existing, candidate)
        if CONFIDENCE_RANK[candidate.confidence] > CONFIDENCE_RANK[existing.confidence]:
            existing.confidence = candidate.confidence
            existing.resolution = candidate.resolution.model_copy(deep=True)
        cls._merge_evidence(existing, candidate)
        cls._merge_confidence_sources(existing, candidate)

    @staticmethod
    def _merge_callsite_properties(existing: Edge, candidate: Edge) -> None:
        def facts(edge: Edge) -> list[dict[str, Any]]:
            values = edge.properties.get("callsites")
            if isinstance(values, list):
                return [value for value in values if isinstance(value, dict)]
            value = edge.properties.get("callsite")
            return [value] if isinstance(value, dict) else []

        def order(fact: dict[str, Any]) -> tuple[Any, ...]:
            return (
                str(fact.get("path") or ""),
                int(fact.get("line") or 0),
                int(fact.get("column") or 0),
                str(fact.get("raw_expression") or ""),
            )

        # A call's fact carries no path, line or column, but its callsite_id
        # is derived from them, so it tells two calls of one name apart; one
        # edge per caller and target keeps a fact for every call it stands for.
        merged: dict[Any, dict[str, Any]] = {}
        for fact in [*facts(existing), *facts(candidate)]:
            key = fact.get("callsite_id") or order(fact)
            merged.setdefault(key, fact)
        if not merged:
            return
        # The sort is stable: among calls of one name, the first seen stays
        # first, so the edge's callsite is the one it was before.
        ordered = sorted(merged.values(), key=order)
        existing.properties["callsites"] = ordered
        existing.properties["callsite"] = ordered[0]

    @staticmethod
    def _merge_route_registration_properties(existing: Edge, candidate: Edge) -> None:
        """Keep every registration when one handler serves a route twice.

        Edge identity is (source, target, kind, semantic_role) here and in the
        change-delta store's primary key, so two registrations of the same
        route that reuse a handler MUST stay one edge. Splitting them would
        collide that key and abort the delta engine. The per-registration
        facts therefore accumulate on the surviving edge, exactly as callsite
        facts do, instead of the later registration being discarded with its
        edge. Order is deterministic so a rebuild does not flap the semantic
        projection.
        """

        def registrations(edge: Edge) -> list[dict[str, Any]]:
            values = edge.properties.get("route_registrations")
            if isinstance(values, list):
                return [value for value in values if isinstance(value, dict)]
            registration_id = edge.properties.get("route_registration_id")
            if not isinstance(registration_id, str) or not registration_id:
                return []
            return [
                {
                    "registration_id": registration_id,
                    "handler_index": edge.properties.get("route_handler_index"),
                    "handler_count": edge.properties.get("route_handler_count"),
                    "role": edge.properties.get("registration_role"),
                    "line": edge.properties.get("route_registration_line"),
                    "column": edge.properties.get("route_registration_column"),
                    "complete": edge.properties.get("route_registration_complete"),
                }
            ]

        # One registration can list the same handler more than once
        # (`get(path, auth, auth, handler)`), so a registration identifies a
        # chain, not a position: the fact identity is the position within it.
        merged: dict[tuple[str, Any], dict[str, Any]] = {}
        for record in [*registrations(existing), *registrations(candidate)]:
            registration_id = record.get("registration_id")
            if isinstance(registration_id, str) and registration_id:
                merged.setdefault(
                    (registration_id, record.get("handler_index")), record
                )
        if not merged:
            return
        ordered = [
            merged[key]
            for key in sorted(
                merged,
                key=lambda key: (
                    int(merged[key].get("line") or 0),
                    int(merged[key].get("column") or 0),
                    key[0],
                    int(merged[key].get("handler_index") or 0),
                ),
            )
        ]
        existing.properties["route_registrations"] = ordered
        # The scalar properties keep naming the first registration so readers
        # that never learned about the list stay correct for it.
        first = ordered[0]
        existing.properties["route_registration_id"] = first["registration_id"]
        for property_name, key in (
            ("route_handler_index", "handler_index"),
            ("route_handler_count", "handler_count"),
            ("registration_role", "role"),
            ("route_registration_line", "line"),
            ("route_registration_column", "column"),
            ("route_registration_complete", "complete"),
        ):
            if first.get(key) is not None:
                existing.properties[property_name] = first[key]

    @staticmethod
    def _merge_import_properties(existing: Edge, candidate: Edge) -> None:
        """Keep a runtime dependency when type and value imports collapse.

        Import edges intentionally dedupe by source, target, kind, and semantic
        role. When the same module imports both types and runtime values from a
        dependency, the runtime dependency is the stronger graph fact and must
        not be hidden merely because the type-only statement appeared first.
        Its kind and specifier describe one import statement, so promote them
        together rather than constructing a mixed fact from two statements.
        """

        existing_kind = existing.properties.get("import_kind")
        candidate_kind = candidate.properties.get("import_kind")
        if (
            existing_kind == "type"
            and isinstance(candidate_kind, str)
            and candidate_kind != "type"
        ):
            for key in ("import_kind", "specifier"):
                if key in candidate.properties:
                    existing.properties[key] = candidate.properties[key]
                else:
                    existing.properties.pop(key, None)

    @classmethod
    def _merge_similarity_edge(cls, existing: Edge, candidate: Edge) -> None:
        existing_score = cls._similarity_score(existing)
        candidate_score = cls._similarity_score(candidate)
        if candidate_score > existing_score:
            existing.properties.update(candidate.properties)
            existing.confidence = candidate.confidence
        cls._merge_evidence(existing, candidate)
        cls._merge_confidence_sources(existing, candidate)

    @classmethod
    def _merge_evidence(cls, existing: Edge, candidate: Edge) -> None:
        existing.evidence.extend(
            evidence
            for evidence in candidate.evidence
            if not cls.has_evidence(existing, evidence)
        )

    @staticmethod
    def _merge_confidence_sources(existing: Edge, candidate: Edge) -> None:
        for source, kinds in candidate.confidence_sources.items():
            existing.confidence_sources.setdefault(source, [])
            existing.confidence_sources[source].extend(
                kind
                for kind in kinds
                if kind not in existing.confidence_sources[source]
            )

    @staticmethod
    def _similarity_score(edge: Edge) -> float:
        score = edge.properties.get("score", 0)
        return float(score) if isinstance(score, (int, float)) else 0.0

    def _conflict_diagnostics(
        self, metadata: IndexMetadata, edges: list[Edge]
    ) -> list[SemanticDiagnostic]:
        by_scope: dict[tuple[Any, ...], set[str]] = defaultdict(set)
        instance_witnesses: dict[tuple[Any, ...], set[str]] = defaultdict(set)
        for edge in edges:
            if edge.confidence != "confirmed":
                continue
            for key in self._conflict_keys(edge):
                by_scope[key].add(edge.target)
                instance_witnesses[key].add(self._conflict_instance_witness(edge, key))

        diagnostics: list[SemanticDiagnostic] = []
        conflict_occurrences: dict[tuple[Any, ...], int] = defaultdict(int)
        conflicts: list[
            tuple[
                tuple[Any, ...],
                set[str],
                dict[str, Any],
                dict[str, Any],
                tuple[Any, ...],
            ]
        ] = []
        for key, targets in by_scope.items():
            if len(targets) < 2:
                continue
            properties = self._conflict_properties(key, targets)
            stable_subject = {
                "source": properties["source"],
                "edge_kind": properties["edge_kind"],
                "semantic_role": properties["semantic_role"],
                "targets": properties["targets"],
            }
            subject_key = (
                stable_subject["source"],
                stable_subject["edge_kind"],
                stable_subject["semantic_role"],
                *stable_subject["targets"],
            )
            conflicts.append((key, targets, properties, stable_subject, subject_key))

        for key, targets, properties, stable_subject, subject_key in sorted(
            conflicts,
            key=lambda item: (
                self._sortable_conflict_key(item[4]),
                tuple(sorted(instance_witnesses[item[0]])),
                self._sortable_conflict_key(item[0]),
            ),
        ):
            conflict_occurrences[subject_key] += 1
            properties["stable_conflict_subject"] = stable_subject
            properties["stable_conflict_occurrence"] = conflict_occurrences[subject_key]
            diagnostics.append(
                SemanticDiagnostic(
                    diagnostic_id=diagnostic_id(
                        self.repo_id,
                        "merge_conflict",
                        *key,
                        metadata.index_version,
                    ),
                    repo_id=self.repo_id,
                    index_version=metadata.index_version,
                    diagnostic_kind="merge_conflict",
                    message=(
                        "Confirmed facts disagree for "
                        f"{properties['conflict_scope']} "
                        f"{properties['conflict_id']}: "
                        f"{', '.join(sorted(targets))}"
                    ),
                    severity="warning",
                    frontend_name="platform-merge",
                    first_seen_index=metadata.index_version,
                    last_seen_index=metadata.index_version,
                    properties=properties,
                )
            )
        return diagnostics

    @staticmethod
    def _sortable_conflict_key(key: tuple[Any, ...]) -> tuple[str, ...]:
        return tuple("" if item is None else str(item) for item in key)

    @staticmethod
    def _conflict_instance_witness(edge: Edge, key: tuple[Any, ...]) -> str:
        """Order repeated semantic conflicts by non-coordinate evidence first."""

        matching_evidence = []
        for evidence in edge.evidence:
            if key[0] == "location" and (
                evidence.path != key[4]
                or evidence.start_line != key[5]
                or evidence.column != key[6]
            ):
                continue
            matching_evidence.append(
                {
                    "kind": evidence.kind,
                    "detail": evidence.detail,
                    "snippet": evidence.snippet,
                }
            )
        projection = {
            "target": edge.target,
            "resolution": {
                "status": edge.resolution.status,
                "strategy": edge.resolution.strategy,
                "candidate_count": edge.resolution.candidate_count,
                "detail": edge.resolution.detail,
                "fallbacks": sorted(edge.resolution.fallbacks),
            },
            "evidence": sorted(
                matching_evidence,
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        }
        return json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _conflict_keys(edge: Edge) -> list[tuple[Any, ...]]:
        if edge.resolution.callsite_id:
            return [
                (
                    "callsite",
                    edge.source,
                    edge.kind,
                    edge.semantic_role,
                    edge.resolution.callsite_id,
                )
            ]

        keys: list[tuple[Any, ...]] = []
        seen: set[tuple[Any, ...]] = set()
        for evidence in edge.evidence:
            if evidence.path is None or evidence.start_line is None:
                continue
            key = (
                "location",
                edge.source,
                edge.kind,
                edge.semantic_role,
                evidence.path,
                evidence.start_line,
                evidence.column,
            )
            if key not in seen:
                keys.append(key)
                seen.add(key)
        return keys

    @staticmethod
    def _conflict_properties(key: tuple[Any, ...], targets: set[str]) -> dict[str, Any]:
        if key[0] == "callsite":
            _, source, edge_kind, semantic_role, callsite_id = key
            return {
                "conflict_scope": "callsite",
                "conflict_id": callsite_id,
                "source": source,
                "edge_kind": edge_kind,
                "semantic_role": semantic_role,
                "callsite_id": callsite_id,
                "targets": sorted(targets),
            }

        _, source, edge_kind, semantic_role, path, start_line, column = key
        return {
            "conflict_scope": "location",
            "conflict_id": f"{path}:{start_line}:{column}",
            "source": source,
            "edge_kind": edge_kind,
            "semantic_role": semantic_role,
            "path": path,
            "start_line": start_line,
            "column": column,
            "targets": sorted(targets),
        }
