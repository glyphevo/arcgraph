"""Semantic fact compatibility helpers for the platform 0.6 graph store."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from arcgraph.core.ids import canonical_identity, diagnostic_id, semantic_fact_id
from arcgraph.core.schemas import (
    BuildWarning,
    Edge,
    FactEvidence,
    IndexMetadata,
    MergeMetrics,
    Node,
    SchemaMigrationRecord,
    SemanticDiagnostic,
    SemanticFact,
)

COMPAT_FRONTEND_NAME = "python-v1-compat-shim"
COMPAT_FRONTEND_VERSION = "0.1.0"
DEFAULT_REPO_ID = "default"

_WARNING_FRONTEND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("typescript_", "typescript-static"),
    ("nextjs_", "typescript-static"),
    ("vue_", "typescript-static"),
)


def ensure_node_canonical_identities(
    nodes: list[Node],
    *,
    repo_root: Path,
    project_name: str | None = None,
    project_version: str | None = None,
    # Deprecated: kept for backward compatibility but ignored.
    version: str | None = None,
) -> None:
    package = project_name or repo_root.name or "workspace"
    identity_version = project_version or "working-tree"
    for node in nodes:
        if node.canonical_identity is None:
            node.canonical_identity = canonical_identity(
                node.id,
                package=package,
                version=identity_version,
            )


def semantic_facts_from_graph(
    metadata: IndexMetadata,
    nodes: list[Node],
    edges: list[Edge],
    *,
    repo_id: str = DEFAULT_REPO_ID,
) -> list[SemanticFact]:
    facts = [
        _node_fact(metadata, node, repo_id=repo_id)
        for node in sorted(nodes, key=lambda item: item.id)
    ]
    facts.extend(
        _edge_fact(metadata, edge, repo_id=repo_id)
        for edge in sorted(
            edges,
            key=lambda item: (
                item.source,
                item.target,
                item.kind,
                item.semantic_role or "",
                item.confidence,
            ),
        )
    )
    return facts


def diagnostics_from_warnings(
    metadata: IndexMetadata,
    warnings: list[BuildWarning],
    *,
    repo_id: str = DEFAULT_REPO_ID,
) -> list[SemanticDiagnostic]:
    diagnostics: list[SemanticDiagnostic] = []
    for warning in warnings:
        diagnostics.append(
            SemanticDiagnostic(
                diagnostic_id=diagnostic_id(
                    repo_id,
                    warning.kind,
                    warning.path,
                    warning.message,
                    # Two frontends may emit the same kind/path/message; the
                    # id must keep them distinct or the SQLite primary key
                    # collapses them while the JSONL keeps both.
                    _warning_frontend_name(warning),
                    metadata.index_version,
                ),
                repo_id=repo_id,
                index_version=metadata.index_version,
                diagnostic_kind=warning.kind,
                message=warning.message,
                severity="warning",
                path=warning.path,
                frontend_name=_warning_frontend_name(warning),
                first_seen_index=metadata.index_version,
                last_seen_index=metadata.index_version,
                properties={
                    "warning_subject": {
                        "path": warning.path,
                        "message": " ".join(warning.message.split()),
                    }
                },
            )
        )
    return diagnostics


def _warning_frontend_name(warning: BuildWarning) -> str:
    # The producing frontend is authoritative; the kind-prefix table below is a
    # fallback for warnings recorded before `frontend_name` was carried, and
    # cannot distinguish kinds that more than one frontend emits.
    if warning.frontend_name:
        return warning.frontend_name
    for prefix, frontend_name in _WARNING_FRONTEND_PREFIXES:
        if warning.kind.startswith(prefix):
            return frontend_name
    return COMPAT_FRONTEND_NAME


def merge_metrics_from_graph(
    metadata: IndexMetadata,
    facts: list[SemanticFact],
    nodes: list[Node],
    edges: list[Edge],
    diagnostics: list[SemanticDiagnostic],
    *,
    repo_id: str = DEFAULT_REPO_ID,
) -> MergeMetrics:
    confidence_counts = Counter(edge.confidence for edge in edges)
    edge_kind_counts = Counter(edge.kind for edge in edges)
    return MergeMetrics(
        repo_id=repo_id,
        index_version=metadata.index_version,
        facts_in=len(facts),
        facts_merged=len(nodes) + len(edges),
        nodes_out=len(nodes),
        edges_out=len(edges),
        diagnostics_count=len(diagnostics),
        conflicts_count=sum(
            1
            for diagnostic in diagnostics
            if diagnostic.diagnostic_kind == "merge_conflict"
        ),
        unresolved_count=(
            sum(1 for fact in facts if fact.confidence == "unresolved")
            + sum(
                1
                for diagnostic in diagnostics
                if diagnostic.diagnostic_kind == "unresolved_callsite"
            )
        ),
        confidence_counts=dict(sorted(confidence_counts.items())),
        edge_kind_counts=dict(sorted(edge_kind_counts.items())),
    )


def schema_migration_records(metadata: IndexMetadata) -> list[SchemaMigrationRecord]:
    return [
        SchemaMigrationRecord(
            migration_id=f"schema:{metadata.schema_version}:full-rebuild",
            from_schema_version=None,
            to_schema_version=metadata.schema_version,
            strategy="full-rebuild",
            required_rebuild=True,
            notes="0.5.x indexes require a full rebuild for platform semantic facts.",
        )
    ]


def _node_fact(metadata: IndexMetadata, node: Node, *, repo_id: str) -> SemanticFact:
    language = str(node.properties.get("language") or "python")
    frontend_name = str(node.properties.get("frontend_name") or COMPAT_FRONTEND_NAME)
    frontend_version = str(
        node.properties.get("frontend_version") or COMPAT_FRONTEND_VERSION
    )
    return SemanticFact(
        fact_id=semantic_fact_id(
            frontend_name,
            language,
            "node",
            node.id,
            node.kind,
            node.path,
            node.start_line,
        ),
        repo_id=repo_id,
        index_version=metadata.index_version,
        language=language,
        frontend_name=frontend_name,
        frontend_version=frontend_version,
        fact_kind="node",
        node_id=node.id,
        confidence="confirmed",
        path=node.path,
        start_line=node.start_line,
        end_line=node.end_line,
        properties={
            "kind": node.kind,
            "name": node.name,
            "qualname": node.qualname,
            "canonical_identity": node.canonical_identity,
            "node_properties": node.properties,
        },
    )


def _edge_fact(metadata: IndexMetadata, edge: Edge, *, repo_id: str) -> SemanticFact:
    primary_evidence = edge.evidence[0] if edge.evidence else None
    path = primary_evidence.path if primary_evidence else None
    start_line = primary_evidence.start_line if primary_evidence else None
    end_line = primary_evidence.end_line if primary_evidence else None
    language = str(edge.properties.get("language") or "python")
    frontend_name = str(edge.properties.get("frontend_name") or COMPAT_FRONTEND_NAME)
    frontend_version = str(
        edge.properties.get("frontend_version") or COMPAT_FRONTEND_VERSION
    )
    return SemanticFact(
        fact_id=semantic_fact_id(
            frontend_name,
            language,
            "edge",
            edge.source,
            edge.target,
            edge.kind,
            path,
            start_line,
            edge.semantic_role,
        ),
        repo_id=repo_id,
        index_version=metadata.index_version,
        language=language,
        frontend_name=frontend_name,
        frontend_version=frontend_version,
        fact_kind="edge",
        source=edge.source,
        target=edge.target,
        edge_kind=edge.kind,
        confidence=edge.confidence,
        semantic_role=edge.semantic_role,
        path=path,
        start_line=start_line,
        end_line=end_line,
        resolution=edge.resolution,
        evidence=[
            FactEvidence(**item.model_dump(exclude_none=True)) for item in edge.evidence
        ],
        properties={
            **edge.properties,
            "confidence_sources": edge.confidence_sources,
        },
    )
