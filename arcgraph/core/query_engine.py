"""Read-only ArcGraph query engine."""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcgraph.core.binding_metrics import collect_binding_metrics
from arcgraph.core.assurance import build_assurance, target_coverage_status
from arcgraph.core.confidence_profiles import (
    ConfidenceProfileConfig,
    effective_max_depth,
    get_profile,
    profile_summary,
)
from arcgraph.core.freshness import compute_freshness
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.ids import (
    callsite_id,
    module_id,
    route_id,
)
from arcgraph.core.payload_policy import apply_index_status_contract
from arcgraph.core.precise_references import (
    TYPESCRIPT_REFERENCE_EXTENSIONS,
    typescript_language_service_references,
)
from arcgraph.core.recovery import stale_recovery_action
from arcgraph.core.semantic_metrics import (
    CALLSITE_RESOLUTION_EDGE_KINDS,
    collect_semantic_metrics_from_resolved_callsites,
    resolved_callsite_ids_from_edge_rows,
)
from arcgraph.core.schemas import SCHEMA_VERSION, Freshness
from arcgraph.core.target_resolver import (
    ResolutionPolicy,
    TargetResolution,
    TargetResolver,
    _WILDCARD_ROUTE_METHODS,
    entrypoint_query_aliases,
    route_query_aliases,
)
from arcgraph.core.type_metrics import collect_type_metrics
from arcgraph.core.unresolved_classification import (
    UNRESOLVED_CATEGORIES,
    annotate_unresolved_record,
)
from arcgraph.core.utils import int_or_none
from arcgraph.core.visual_contract import (
    RESOURCE_EDGE_KINDS,
    RESOURCE_NODE_KINDS,
    entrypoint_stat_kinds,
    visual_contract,
)

CALL_QUERY_EDGE_KINDS = [
    "calls",
    "constructs",
    "renders",
    "uses_hook",
    "extends",
    "implements",
    "initializes",
    "logs",
    "configures",
    "declares",
    "invokes",
    "overrides",
    "uses",
]
CALLSITE_QUERY_EDGE_KINDS = [
    "calls",
    "constructs",
    "initializes",
    "logs",
    "configures",
    "declares",
    "uses",
    "dynamic_call",
]
CONFIDENCE_BUCKETS = {
    "confirmed": "confirmed_impact",
    "inferred": "inferred_impact",
    "runtime-only": "runtime_impact",
    "heuristic": "heuristic_impact",
    "unresolved": "unresolved_impact",
}
ENTRYPOINT_FLOW_EDGE_KINDS = [
    "registers",
    "invokes",
    "injects",
    "calls",
    "constructs",
    "renders",
    "uses_hook",
    "provides",
    "enqueues",
    "consumes",
    "reads",
    "writes",
]
SQLITE_IN_BATCH_SIZE = 900
# A similarity component is unbounded in principle (generated code can link
# thousands of look-alike symbols), and every visited node costs a hydrated
# edge row. This caps the walk for every input; hitting it is disclosed.
SIMILARITY_FAMILY_NODE_LIMIT = 500
# Route registration gaps are disclosure, so the list is bounded and the
# omitted count travels with it.
_ROUTE_REGISTRATION_GAP_LIMIT = 25


@dataclass(frozen=True)
class PathQueryLimits:
    """Guardrails for BFS traversal in impact / reverse-call queries.

    * ``max_edges`` — hard cap on total edges collected.
    * ``max_fan_in`` — max edges kept per discovered node during
      expansion.  Prevents a single highly-connected hub from
      dominating the result.
    * ``timeout_seconds`` — wall-clock limit for the traversal loop.
    """

    max_edges: int = 5000
    max_fan_in: int = 200
    timeout_seconds: float = 10.0


DEFAULT_PATH_QUERY_LIMITS = PathQueryLimits()


def _route_query_aliases(query: str) -> list[str]:
    """Compatibility delegate to the unified route resolver."""

    return route_query_aliases(query)


class SchemaVersionError(RuntimeError):
    pass


def _scip_reference_locations(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    locations: dict[tuple[Any, ...], dict[str, Any]] = {}
    for edge in edges:
        for evidence in edge.get("evidence", []):
            if not isinstance(evidence, dict) or not evidence.get("path"):
                continue
            item = {
                "path": evidence.get("path"),
                "line": evidence.get("start_line"),
                "column": evidence.get("column"),
                "end_line": evidence.get("end_line"),
                "is_definition": False,
                "is_write": False,
            }
            key = (item["path"], item["line"], item["column"], item["end_line"])
            locations.setdefault(key, item)
    return [
        locations[key]
        for key in sorted(
            locations,
            key=lambda item: (
                str(item[0]),
                int(item[1] or 0),
                int(item[2] or 0),
            ),
        )
    ]


def _reference_truncation(total: int, max_results: int) -> dict[str, Any]:
    returned = min(total, max_results)
    return {
        "truncated": total > max_results,
        "scope": "response_presentation",
        "reason": "max_results" if total > max_results else None,
        "max_results": max_results,
        "total": total,
        "returned": returned,
        "omitted": max(0, total - returned),
    }


def _route_registration_facts(properties: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every registration recorded on one route-invokes edge.

    A handler reused across registrations of the same route collapses to a
    single edge — edge identity cannot carry the registration — so the merge
    accumulates the per-registration facts in ``route_registrations``. Edges
    written before that list existed still carry the scalar properties.
    """

    records = properties.get("route_registrations")
    if isinstance(records, list):
        facts = [record for record in records if isinstance(record, dict)]
        if facts:
            return facts
    return [
        {
            "registration_id": properties.get("route_registration_id"),
            "handler_index": properties.get("route_handler_index"),
            "handler_count": properties.get("route_handler_count"),
            "role": properties.get("registration_role"),
            "line": properties.get("route_registration_line"),
            "column": properties.get("route_registration_column"),
            "complete": properties.get("route_registration_complete"),
        }
    ]


def _chain_positions_are_complete(chain: dict[str, Any]) -> bool:
    """Return whether a chain holds every position its registration declared."""

    declared = chain.get("handler_count")
    positions = sorted(
        item["position"]
        for item in chain.get("handlers", [])
        if isinstance(item.get("position"), int)
    )
    if len(positions) != len(chain.get("handlers", [])):
        return False
    if not isinstance(declared, int):
        # Without a declared count the only checkable claim is that the
        # positions present form a gapless run from zero.
        declared = len(positions)
    return positions == list(range(declared))


def _similarity_information(profile: dict[str, Any]) -> dict[str, Any]:
    """Classify profiles that are too small to support strong similarity claims."""

    algorithm = str(profile.get("algorithm") or "")
    if isinstance(profile.get("ngram_count"), int):
        feature_count = int(profile["ngram_count"])
    elif isinstance(profile.get("ngrams"), list):
        feature_count = len(profile["ngrams"])
    else:
        feature_count = 0
    low_information = feature_count < 8
    return {
        "low_information": low_information,
        "feature_count": feature_count,
        "algorithm": algorithm or None,
        "reason": (
            "fewer_than_8_normalized_ngrams"
            if low_information
            else "sufficient_normalized_ngram_evidence"
        ),
        "non_claim": (
            "A similarity score is structural evidence, not proof that two "
            "implementations have identical behavior or intent."
        ),
    }


class QueryEngine:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir.resolve()
        self.store = GraphStoreReader.from_current(self.output_dir)
        self._freshness_cache: Freshness | None = None
        self._stale_warning_cache: list[str] | None = None
        self._check_schema_version()
        self.target_resolver = TargetResolver(self.store.metadata.get("repo_root"))

    def _check_schema_version(self) -> None:
        actual = self.store.metadata.get("schema_version")
        if actual != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"ArcGraph schema mismatch: index has {actual}, runtime expects {SCHEMA_VERSION}. Rebuild required."
            )

    def current(self) -> dict[str, Any]:
        return apply_index_status_contract(
            {
                **self._envelope(warnings=self.store.metadata.get("warnings", [])),
                "repo_root": self.store.metadata.get("repo_root"),
                "source_roots": self.store.metadata.get("source_roots", []),
                "created_at": self.store.metadata.get("created_at"),
                "commit_sha": self.store.metadata.get("commit_sha"),
                "file_count": self.store.metadata.get("file_count", 0),
                "node_count": self.store.metadata.get("node_count", 0),
                "edge_count": self.store.metadata.get("edge_count", 0),
                "warning_count": self.store.metadata.get("warning_count", 0),
                "language_tiers": self.store.metadata.get("language_tiers", {}),
                "extractor_metadata": self.store.metadata.get("extractor_metadata", {}),
                "toolchain_status": self.store.metadata.get("toolchain_status", {}),
                "adapter_metrics": self.store.metadata.get("adapter_metrics", {}),
                "precision_metrics": self.store.metadata.get("precision_metrics", {}),
                "coverage_metrics": self.store.metadata.get("coverage_metrics", {}),
                "runtime_metrics": self.store.metadata.get("runtime_metrics", {}),
                "evidence_manifest": self.store.metadata.get("evidence_manifest"),
            }
        )

    def reset_freshness_cache(self) -> None:
        """Force the next freshness query to recompute from store metadata."""
        self._freshness_cache = None

    def set_freshness_override(self, freshness: Freshness) -> None:
        """Use a precomputed freshness value for lightweight/runtime index views."""
        self._freshness_cache = freshness

    def stats(self) -> dict[str, Any]:
        with self.store.connect() as conn:
            counts = {
                "files": conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                "semantic_facts": conn.execute(
                    "SELECT COUNT(*) FROM semantic_facts"
                ).fetchone()[0],
                "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
                "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
                "warnings": conn.execute("SELECT COUNT(*) FROM warnings").fetchone()[0],
                "diagnostics": conn.execute(
                    "SELECT COUNT(*) FROM diagnostics"
                ).fetchone()[0],
            }
            node_kinds = {
                row["kind"]: row["count"]
                for row in conn.execute(
                    "SELECT kind, COUNT(*) AS count FROM nodes GROUP BY kind ORDER BY kind"
                )
            }
            edge_kinds = {
                row["kind"]: row["count"]
                for row in conn.execute(
                    "SELECT kind, COUNT(*) AS count FROM edges GROUP BY kind ORDER BY kind"
                )
            }

        return {
            **self._envelope(warnings=self.store.metadata.get("warnings", [])),
            "counts": counts,
            "node_kinds": node_kinds,
            "edge_kinds": edge_kinds,
            "language_tiers": self.store.metadata.get("language_tiers", {}),
            "toolchain_status": self.store.metadata.get("toolchain_status", {}),
        }

    def workspace_status(self) -> dict[str, Any]:
        """Return repo/workspace status for interactive ArcGraph clients."""

        current = self.current()
        stats = self.stats()
        architecture = self._architecture_summary_from_node_kinds(
            stats.get("node_kinds", {})
        )
        return {
            **current,
            "stats": stats.get("counts", {}),
            "node_kinds": stats.get("node_kinds", {}),
            "edge_kinds": stats.get("edge_kinds", {}),
            "architecture": {
                "entrypoints": architecture.get("entrypoints", {}),
                "resources": architecture.get("resources", {}),
                "import_cycles": [],
                "top_fan_in": [],
                "top_fan_out": [],
                "degraded": (
                    "Status uses lightweight counts; open Architecture/System Map "
                    "for deeper analysis."
                ),
            },
            "visual_contract": visual_contract(),
        }

    def search_nodes(
        self,
        *,
        query: str,
        kind: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search node identity fields without exposing storage details to API code."""

        term = query.strip()
        safe_limit = max(1, min(limit, 100))
        if not term and not kind:
            return {
                "status": "available",
                "items": [],
                "query": query,
                "kind": kind,
                "summary": {"returned": 0, "limit": safe_limit},
            }

        where_params: list[Any] = []
        if term:
            like = f"%{self._escape_like(term)}%"
            prefix = f"{self._escape_like(term)}%"
            where_params = [like, like, like, like]
            where = """
                (
                    id LIKE ? ESCAPE '\\'
                    OR name LIKE ? ESCAPE '\\'
                    OR COALESCE(qualname, '') LIKE ? ESCAPE '\\'
                    OR COALESCE(path, '') LIKE ? ESCAPE '\\'
                )
            """
            if kind:
                where = f"{where} AND kind = ?"
                where_params.append(kind)
            order_params: list[Any] = [prefix, prefix, prefix, prefix]
            order_clause = """
                    CASE
                        WHEN id LIKE ? ESCAPE '\\' THEN 0
                        WHEN name LIKE ? ESCAPE '\\' THEN 1
                        WHEN COALESCE(qualname, '') LIKE ? ESCAPE '\\' THEN 2
                        WHEN COALESCE(path, '') LIKE ? ESCAPE '\\' THEN 3
                        ELSE 4
                    END,
                    id
            """
            all_params = [*where_params, *order_params, safe_limit]
        else:
            where = "kind = ?"
            where_params = [kind]
            order_clause = "name, id"
            all_params = [*where_params, safe_limit]

        with self.store.connect() as conn:
            total = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM nodes
                    WHERE {where}
                    """,
                    where_params,
                ).fetchone()["count"]
            )
            rows = conn.execute(
                f"""
                SELECT id, kind, name, qualname, path, start_line, end_line
                FROM nodes
                WHERE {where}
                ORDER BY {order_clause}
                LIMIT ?
                """,
                all_params,
            ).fetchall()

        items = [
            {
                "id": row["id"],
                "kind": row["kind"],
                "label": row["name"] or row["qualname"] or row["id"],
                "name": row["name"],
                "qualname": row["qualname"],
                "path": row["path"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
            }
            for row in rows
        ]
        return {
            "status": "available",
            "items": items,
            "query": query,
            "kind": kind,
            "summary": {"returned": len(items), "limit": safe_limit, "total": total},
        }

    def node_detail(self, node_id: str) -> dict[str, Any]:
        """Return one node plus local relationship/test/diagnostic context."""

        with self.store.connect() as conn:
            node = self._node_by_id(conn, node_id)
            if node is None:
                return {
                    "status": "unavailable",
                    "node": None,
                    "diagnostics": [],
                    "callers": [],
                    "callees": [],
                    "tests": [],
                    "warnings": [f"No ArcGraph node found for {node_id!r}."],
                }
            node_ref = f"%{self._escape_like(node_id)}%"
            diagnostics = [
                self._diagnostic_row(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM diagnostics
                    WHERE path = ? OR properties_json LIKE ? ESCAPE '\\'
                    ORDER BY severity DESC, diagnostic_id
                    LIMIT 50
                    """,
                    (node.get("path"), node_ref),
                ).fetchall()
            ]

        callers = self.callers(node_id)
        callees = self.callees(node_id)
        tests = self.tests(node_id)
        return {
            "status": "available",
            "node": node,
            "callers": self._summarize_nodes(callers.get("callers", [])),
            "callees": self._summarize_nodes(callees.get("callees", [])),
            "edges": {
                "incoming": self._summarize_edges(callers.get("edges", [])),
                "outgoing": self._summarize_edges(callees.get("edges", [])),
            },
            "diagnostics": diagnostics,
            "tests": tests.get("candidates", [])[:25],
            "test_gaps": tests.get("test_gaps", [])[:25],
            "capabilities": callers.get("capabilities", {}),
            "freshness": callers.get("freshness", {}),
            "warnings": [
                *callers.get("warnings", []),
                *callees.get("warnings", []),
                *tests.get("warnings", []),
            ],
        }

    def semantic_stats(self) -> dict[str, Any]:
        nodes = self.store.read_nodes()
        with self.store.connect() as conn:
            diagnostics = self._diagnostic_rows(conn)
            merge_metrics = self._merge_metrics(conn)
            edge_kind_counts = self._count_edge_field(conn, "kind")
            confidence_counts = self._count_edge_field(conn, "confidence")
            callsite_edge_kinds = sorted(CALLSITE_RESOLUTION_EDGE_KINDS)
            placeholders = ",".join("?" for _ in callsite_edge_kinds)
            resolved_edge_rows = conn.execute(
                f"""
                SELECT source, resolution_json, evidence_json
                FROM edges
                WHERE kind IN ({placeholders})
                """,
                callsite_edge_kinds,
            ).fetchall()

        metrics = collect_semantic_metrics_from_resolved_callsites(
            nodes,
            resolved_callsite_ids_from_edge_rows(resolved_edge_rows),
            edge_kind_counts=edge_kind_counts,
            confidence_counts=confidence_counts,
        )
        metrics["binding_summary"] = collect_binding_metrics(nodes)
        metrics["type_summary"] = collect_type_metrics(nodes)
        metrics["by_diagnostic_kind"] = dict(
            sorted(Counter(item["diagnostic_kind"] for item in diagnostics).items())
        )
        return {
            **self._envelope(
                capabilities=self._capabilities(semantic_stats="available")
            ),
            "metrics": metrics,
            "binding_summary": metrics["binding_summary"],
            "type_summary": metrics["type_summary"],
            "merge_metrics": merge_metrics,
        }

    def unresolved(
        self,
        target: str | None = None,
        limit: int = 100,
        *,
        category: str | None = None,
        release_blocking_only: bool = False,
    ) -> dict[str, Any]:
        if category is not None and category not in UNRESOLVED_CATEGORIES:
            raise ValueError(
                "Unknown unresolved category "
                f"{category!r}. Expected one of: {', '.join(UNRESOLVED_CATEGORIES)}."
            )
        limit = max(1, min(limit, 10000))
        with self.store.connect() as conn:
            target_ids, target_paths, target_qualname_prefixes, resolution = (
                self._unresolved_filter_scope(conn, target)
            )
            target_id_set = set(target_ids)
            has_scope = bool(target_ids or target_paths or target_qualname_prefixes)
            diagnostics = [
                item
                for item in (
                    self._diagnostic_rows(conn, diagnostic_kind="unresolved_callsite")
                    if target is None or has_scope
                    else []
                )
                if target is None
                or self._matches_unresolved_filter(
                    item,
                    target_id_set,
                    target_paths,
                    target_qualname_prefixes,
                )
            ]

        diagnostics.sort(
            key=lambda item: (
                item.get("path") or "",
                item.get("start_line") or 0,
                item.get("properties", {}).get("column") or 0,
                item.get("diagnostic_id") or "",
            )
        )
        annotated = [annotate_unresolved_record(item) for item in diagnostics]
        if category is not None:
            annotated = [item for item in annotated if item.get("category") == category]
        if release_blocking_only:
            annotated = [
                item for item in annotated if item.get("release_blocking") is True
            ]
        returned = annotated[:limit]
        failed_strategies = Counter(
            item.get("properties", {}).get("failed_strategy", "unknown")
            for item in annotated
        )
        status = "available"
        warnings: list[str] = []
        if (
            target is not None
            and not target_ids
            and not target_paths
            and not target_qualname_prefixes
        ):
            status = "partial"
            warnings.append(f"No graph node or indexed path resolved for {target!r}.")

        result = {
            **self._envelope(
                status=status,
                capabilities=self._capabilities(unresolved="available"),
                warnings=warnings,
            ),
            "query": target,
            "target_resolution": resolution.to_dict() if resolution else None,
            "resolved_targets": target_ids,
            "resolved_paths": sorted(target_paths),
            "summary": {
                "total": len(annotated),
                "returned": len(returned),
                "limit": limit,
                "category_filter": category,
                "release_blocking_only": release_blocking_only,
                "by_failed_strategy": dict(sorted(failed_strategies.items())),
            },
            "unresolved": returned,
        }
        # Phase 1.1: inline classification so the HTTP API can serve it directly.
        from arcgraph.core.unresolved_classification import (
            classify_unresolved_records,
        )

        result["classification"] = classify_unresolved_records(result)
        return result

    def bindings(self, target: str, limit: int = 200) -> dict[str, Any]:
        limit = max(1, min(limit, 1000))
        with self.store.connect() as conn:
            resolution = self._resolve_target(conn, target)
            target_ids = list(resolution.resolved_ids)
            nodes = self._nodes_by_ids(conn, target_ids)
        bindings = self._node_property_records(
            nodes,
            property_name="bindings",
            scope_field="scope_id",
            limit=limit,
        )
        by_kind = Counter(item.get("kind", "unknown") for item in bindings["records"])
        by_confidence = Counter(
            item.get("confidence", "unknown") for item in bindings["records"]
        )
        return {
            **self._envelope(
                status="available" if target_ids else "partial",
                capabilities=self._capabilities(bindings="available"),
                warnings=[] if target_ids else [self._resolution_warning(resolution)],
            ),
            "query": target,
            "target_resolution": resolution.to_dict(),
            "resolved_targets": target_ids,
            "summary": {
                **bindings["summary"],
                "by_kind": dict(sorted(by_kind.items())),
                "by_confidence": dict(sorted(by_confidence.items())),
            },
            "bindings": bindings["records"],
        }

    def types(self, target: str, limit: int = 200) -> dict[str, Any]:
        limit = max(1, min(limit, 1000))
        with self.store.connect() as conn:
            resolution = self._resolve_target(conn, target)
            target_ids = list(resolution.resolved_ids)
            nodes = self._nodes_by_ids(conn, target_ids)
        type_refs = self._node_property_records(
            nodes,
            property_name="type_refs",
            scope_field="scope_id",
            limit=limit,
        )
        diagnostics = self._node_property_records(
            nodes,
            property_name="type_diagnostics",
            scope_field="scope_id",
            limit=limit,
        )
        by_strategy = Counter(
            item.get("strategy", "unknown") for item in type_refs["records"]
        )
        by_confidence = Counter(
            item.get("confidence", "unknown") for item in type_refs["records"]
        )
        by_status = Counter(
            item.get("resolution_status", "unknown") for item in type_refs["records"]
        )
        return {
            **self._envelope(
                status="available" if target_ids else "partial",
                capabilities=self._capabilities(types="available"),
                warnings=[] if target_ids else [self._resolution_warning(resolution)],
            ),
            "query": target,
            "target_resolution": resolution.to_dict(),
            "resolved_targets": target_ids,
            "summary": {
                **type_refs["summary"],
                "diagnostics_total": diagnostics["summary"]["total"],
                "by_strategy": dict(sorted(by_strategy.items())),
                "by_confidence": dict(sorted(by_confidence.items())),
                "by_resolution_status": dict(sorted(by_status.items())),
            },
            "type_refs": type_refs["records"],
            "diagnostics": diagnostics["records"],
        }

    def callsites(self, target: str, limit: int = 200) -> dict[str, Any]:
        limit = max(1, min(limit, 1000))
        with self.store.connect() as conn:
            resolution = self._resolve_target(conn, target)
            target_ids = list(resolution.resolved_ids)
            nodes = self._nodes_by_ids(conn, target_ids)
            edges = self._edges_for_sources_kinds(
                conn,
                target_ids,
                CALLSITE_QUERY_EDGE_KINDS,
            )

        edges_by_callsite: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in edges:
            # One edge stands for every call of its caller to its target: the
            # resolution names the first, and each fact names its own call.
            edge_ids = {edge.get("resolution", {}).get("callsite_id")}
            for fact in self._edge_callsite_facts(edge):
                edge_ids.add(fact.get("callsite_id"))
            for current_callsite_id in sorted(
                value for value in edge_ids if isinstance(value, str) and value
            ):
                edges_by_callsite[current_callsite_id].append(edge)

        records: list[dict[str, Any]] = []
        for node in nodes:
            callsites = node.get("properties", {}).get("callsites", [])
            if not isinstance(callsites, list):
                continue
            for callsite in callsites:
                if not isinstance(callsite, dict):
                    continue
                raw_expression = callsite.get("name")
                if not isinstance(raw_expression, str) or not raw_expression:
                    continue
                line = int_or_none(callsite.get("line"))
                column = int_or_none(callsite.get("column"))
                current_callsite_id = callsite_id(
                    node["id"],
                    node.get("path"),
                    line,
                    column,
                    raw_expression,
                )
                resolved_edges = edges_by_callsite.get(current_callsite_id, [])
                records.append(
                    {
                        "callsite_id": current_callsite_id,
                        "source_scope": node["id"],
                        "source_kind": node["kind"],
                        "source_qualname": node.get("qualname"),
                        "path": node.get("path"),
                        "line": line,
                        "column": column,
                        "raw_expression": raw_expression,
                        "context": callsite.get("context"),
                        "expression_kind": callsite.get("expression_kind"),
                        "resolution_status": (
                            "resolved" if resolved_edges else "unresolved"
                        ),
                        "edges": resolved_edges,
                    }
                )

        known_callsite_ids = {item["callsite_id"] for item in records}
        for edge in edges:
            fact_records = self._edge_callsite_facts(edge)
            edge_callsite_id = edge.get("resolution", {}).get("callsite_id")
            for fact in fact_records:
                raw_expression = fact.get("raw_expression")
                if not isinstance(raw_expression, str) or not raw_expression:
                    continue
                line = int_or_none(fact.get("line"))
                column = int_or_none(fact.get("column"))
                fact_callsite_id = fact.get("callsite_id")
                # A fact that names its call is that call. Otherwise the
                # edge-level resolution id names exactly one call site, so it
                # may only be bound to a fact when the edge carries exactly
                # one; a merged multi-fact edge derives one id per fact so no
                # fact is dropped by dedup or paired with a foreign id.
                if isinstance(fact_callsite_id, str) and fact_callsite_id:
                    current_callsite_id = fact_callsite_id
                elif (
                    isinstance(edge_callsite_id, str)
                    and edge_callsite_id
                    and len(fact_records) == 1
                ):
                    current_callsite_id = edge_callsite_id
                else:
                    current_callsite_id = callsite_id(
                        edge["source"],
                        fact.get("path"),
                        line,
                        column,
                        raw_expression,
                    )
                if current_callsite_id in known_callsite_ids:
                    continue
                known_callsite_ids.add(current_callsite_id)
                records.append(
                    {
                        "callsite_id": current_callsite_id,
                        "source_scope": edge["source"],
                        "path": fact.get("path"),
                        "line": line,
                        "column": column,
                        "raw_expression": raw_expression,
                        "context": "typescript_call",
                        "expression_kind": "call",
                        "resolution_status": "resolved",
                        "argument_count": fact.get("argument_count"),
                        "syntactic_argument_count": fact.get(
                            "syntactic_argument_count"
                        ),
                        "argument_count_known": fact.get("argument_count_known"),
                        "has_spread_argument": fact.get("has_spread_argument"),
                        "awaited": fact.get("awaited"),
                        "return_value_usage": fact.get("return_value_usage"),
                        "return_value_used": fact.get("return_value_used"),
                        "edges": [edge],
                    }
                )

        records.sort(
            key=lambda item: (
                item.get("path") or "",
                item.get("line") or 0,
                item.get("column") or 0,
                item.get("raw_expression") or "",
            )
        )
        returned = records[:limit]
        by_status = Counter(item["resolution_status"] for item in records)
        by_expression_kind = Counter(
            item.get("expression_kind") or "unknown" for item in records
        )
        return {
            **self._envelope(
                status="available" if target_ids else "partial",
                capabilities=self._capabilities(callsites="available"),
                warnings=[] if target_ids else [self._resolution_warning(resolution)],
            ),
            "query": target,
            "target_resolution": resolution.to_dict(),
            "resolved_targets": target_ids,
            "summary": {
                "total": len(records),
                "returned": len(returned),
                "limit": limit,
                "by_resolution_status": dict(sorted(by_status.items())),
                "by_expression_kind": dict(sorted(by_expression_kind.items())),
            },
            "callsites": returned,
        }

    @staticmethod
    def _edge_callsite_facts(edge: dict[str, Any]) -> list[dict[str, Any]]:
        properties = edge.get("properties", {})
        facts = properties.get("callsites")
        if not isinstance(facts, list):
            facts = [properties.get("callsite")]
        return [fact for fact in facts if isinstance(fact, dict)]

    def imports(self, module: str) -> dict[str, Any]:
        source_id = module if module.startswith("mod:") else module_id(module)
        with self.store.connect() as conn:
            module_node = self._node_by_id(conn, source_id)
            outgoing = [
                self._edge_row(row)
                for row in conn.execute(
                    """
                    SELECT * FROM edges
                    WHERE source = ? AND kind = 'imports'
                    ORDER BY target
                    """,
                    (source_id,),
                )
            ]
            incoming = [
                self._edge_row(row)
                for row in conn.execute(
                    """
                    SELECT * FROM edges
                    WHERE target = ? AND kind = 'imports'
                    ORDER BY source
                    """,
                    (source_id,),
                )
            ]
        status = "available" if module_node else "partial"
        warnings = [] if module_node else [f"No module node resolved for {module!r}."]
        return {
            **self._envelope(status=status, warnings=warnings),
            "module": source_id,
            "outgoing": outgoing,
            "incoming": incoming,
        }

    def symbol(self, query: str) -> dict[str, Any]:
        with self.store.connect() as conn:
            resolution = self._resolve_target(
                conn, query, policy=ResolutionPolicy.SYMBOL_ONE
            )
            rows = self._nodes_by_ids(conn, list(resolution.resolved_ids))

        status = "available" if rows else "partial"
        warnings = [] if rows else [self._resolution_warning(resolution, "symbol")]
        return {
            **self._envelope(status=status, warnings=warnings),
            "query": query,
            "target_resolution": resolution.to_dict(),
            "matches": rows,
        }

    def callers(self, query: str, profile: str = "review_default") -> dict[str, Any]:
        profile_config = get_profile(profile)
        with self.store.connect() as conn:
            resolution = self._resolve_target(conn, query)
            target_ids = list(resolution.resolved_ids)
            edges = self._filter_edges_by_confidence(
                self._edges_for_targets_kinds(conn, target_ids, CALL_QUERY_EDGE_KINDS),
                profile_config=profile_config,
            )
            caller_ids = sorted({edge["source"] for edge in edges})
            callers = self._nodes_by_ids(conn, caller_ids)

        status = "available" if target_ids else "partial"
        warnings = [] if target_ids else [self._resolution_warning(resolution)]
        return {
            **self._envelope(status=status, warnings=warnings),
            "query": query,
            "target_resolution": resolution.to_dict(),
            "resolved_targets": target_ids,
            "callers": callers,
            "edges": edges,
        }

    def callees(self, query: str, profile: str = "review_default") -> dict[str, Any]:
        profile_config = get_profile(profile)
        with self.store.connect() as conn:
            resolution = self._resolve_target(conn, query)
            source_ids = list(resolution.resolved_ids)
            edges = self._filter_edges_by_confidence(
                self._edges_for_sources_kinds(conn, source_ids, CALL_QUERY_EDGE_KINDS),
                profile_config=profile_config,
            )
            callee_ids = sorted({edge["target"] for edge in edges})
            callees = self._nodes_by_ids(conn, callee_ids)

        status = "available" if source_ids else "partial"
        warnings = [] if source_ids else [self._resolution_warning(resolution)]
        return {
            **self._envelope(status=status, warnings=warnings),
            "query": query,
            "target_resolution": resolution.to_dict(),
            "resolved_targets": source_ids,
            "callees": callees,
            "edges": edges,
        }

    def relations(self, query: str, profile: str = "review_default") -> dict[str, Any]:
        """Direct resource/queue relations, separate from callable neighbors."""
        kinds = ["reads", "writes", "enqueues", "consumes"]
        config = get_profile(profile)
        with self.store.connect() as conn:
            resolution = self._resolve_target(conn, query)
            ids = list(resolution.resolved_ids)
            incoming = self._edges_for_targets_kinds(conn, ids, kinds)
            outgoing = self._edges_for_sources_kinds(conn, ids, kinds)
        selected_in = self._filter_edges_by_confidence(incoming, profile_config=config)
        selected_out = self._filter_edges_by_confidence(outgoing, profile_config=config)
        return {
            "incoming": selected_in,
            "outgoing": selected_out,
            "scope": {
                "edge_kinds": kinds,
                "confidence_profile": profile,
                "excluded_by_confidence": {
                    "incoming": len(incoming) - len(selected_in),
                    "outgoing": len(outgoing) - len(selected_out),
                },
                "completeness": "indexed direct relations of these kinds only; not runtime completeness",
            },
        }

    def impact(
        self,
        query: str,
        max_depth: int | None = None,
        profile: str = "review_default",
        include_edge_kinds: list[str] | None = None,
        exclude_edge_kinds: list[str] | None = None,
        path_limits: PathQueryLimits = DEFAULT_PATH_QUERY_LIMITS,
    ) -> dict[str, Any]:
        profile_config = get_profile(profile)
        depth, depth_warnings = effective_max_depth(profile_config, max_depth)
        edge_filter = self._edge_kind_filter(
            include_edge_kinds=include_edge_kinds,
            exclude_edge_kinds=exclude_edge_kinds,
        )
        with self.store.connect() as conn:
            resolution = self._resolve_target(conn, query)
            target_ids = list(resolution.resolved_ids)
            (
                raw_call_edges,
                traversal_truncated,
                traversal_truncation_reasons,
            ) = self._reverse_call_impact(
                conn,
                target_ids,
                depth,
                path_limits=path_limits,
                allowed_confidences=profile_config.allowed_confidences,
            )
            call_edges = self._filter_edges_by_kind(
                raw_call_edges,
                edge_filter=edge_filter,
            )
            affected_symbol_ids = sorted({edge["source"] for edge in call_edges})
            affected_symbols = self._nodes_by_ids(conn, affected_symbol_ids)
            impacted_symbol_ids = sorted({*target_ids, *affected_symbol_ids})
            entrypoint_edges = self._filter_edges_by_confidence(
                self._filter_edges_by_kind(
                    self._edges_for_targets_kinds(
                        conn, impacted_symbol_ids, ["invokes"]
                    ),
                    edge_filter=edge_filter,
                ),
                profile_config=profile_config,
            )
            entrypoint_ids = sorted({edge["source"] for edge in entrypoint_edges})
            entrypoints = self._nodes_by_ids(conn, entrypoint_ids)
            resource_edges = self._filter_edges_by_confidence(
                self._filter_edges_by_kind(
                    self._edges_for_sources_kinds(
                        conn,
                        target_ids,
                        RESOURCE_EDGE_KINDS,
                    ),
                    edge_filter=edge_filter,
                ),
                profile_config=profile_config,
            )
            resource_ids = sorted({edge["target"] for edge in resource_edges})
            resources = self._nodes_by_ids(conn, resource_ids)

            target_module_ids = self._module_ids_for_targets(conn, target_ids)
            import_edges = self._filter_edges_by_confidence(
                self._filter_edges_by_kind(
                    self._edges_for_targets(conn, target_module_ids, "imports"),
                    edge_filter=edge_filter,
                ),
                profile_config=profile_config,
            )
            affected_module_ids = sorted({edge["source"] for edge in import_edges})
            affected_modules = self._nodes_by_ids(conn, affected_module_ids)
            impact_edges = self._unique_edges(
                [
                    *call_edges,
                    *import_edges,
                    *entrypoint_edges,
                    *resource_edges,
                ]
            )
            confidence_impact = self._confidence_impact(impact_edges)
            unresolved_risks = self._unresolved_risks(
                conn,
                target_ids=target_ids,
                affected_symbol_ids=affected_symbol_ids,
                affected_module_ids=affected_module_ids,
                limit=50,
            )
        tests_payload = self.tests(query)
        analysis_limits = {
            "traversal": {
                "truncated": traversal_truncated,
                "reasons": traversal_truncation_reasons,
                "max_depth": depth,
                "max_edges": path_limits.max_edges,
                "max_fan_in": path_limits.max_fan_in,
                "timeout_seconds": path_limits.timeout_seconds,
            },
            "unresolved_risks": {
                "truncated": unresolved_risks.get("summary", {}).get("total", 0)
                > unresolved_risks.get("summary", {}).get("returned", 0),
                "limit": unresolved_risks.get("summary", {}).get("limit", 50),
                "returned": unresolved_risks.get("summary", {}).get("returned", 0),
            },
        }
        assurance = build_assurance(
            current=self.current(),
            target_resolutions=[resolution.to_dict()],
            confidence_summaries=[
                self._confidence_summary(impact_edges, unresolved_risks)
            ],
            coverage_statuses=[
                target_coverage_status(
                    tests_payload.get("coverage", {}).get("status", "unavailable"),
                    bool(tests_payload.get("test_gaps")),
                )
            ],
            analysis_limits=[analysis_limits],
            unresolved_risk_count=unresolved_risks.get("summary", {}).get("total", 0),
        )
        warnings = []
        if not target_ids:
            warnings.append(self._resolution_warning(resolution))
        if traversal_truncated:
            warnings.append(
                f"Impact traversal truncated by guardrails "
                f"(max_edges={path_limits.max_edges}, "
                f"max_fan_in={path_limits.max_fan_in}, "
                f"timeout={path_limits.timeout_seconds}s). "
                f"Result is a partial view of the full blast radius."
            )
        warnings.append(
            "Phase 8 impact combines call, import, framework entrypoint, "
            "queue, and SQLAlchemy resource facts. Dynamic dispatch and "
            "runtime registration remain best-effort; SCIP and coverage "
            "evidence are included only when optional inputs are present."
        )

        return {
            **self._envelope(
                status="available" if target_ids else "partial",
                capabilities=self._capabilities(
                    entrypoints="available",
                    impact="call-import-entrypoint-resource",
                ),
                warnings=warnings,
            ),
            "query": query,
            "target_resolution": resolution.to_dict(),
            "resolved_targets": target_ids,
            "call_impact": {
                "max_depth": depth,
                "edges": call_edges,
                "affected_symbols": affected_symbols,
            },
            "import_impact": {
                "target_modules": target_module_ids,
                "edges": import_edges,
                "affected_modules": affected_modules,
            },
            "entrypoint_impact": {
                "edges": entrypoint_edges,
                "entrypoints": entrypoints,
            },
            "resource_impact": {
                "edges": resource_edges,
                "resources": resources,
            },
            "test_candidates": tests_payload["candidates"],
            "test_gaps": tests_payload["test_gaps"],
            "coverage": tests_payload["coverage"],
            "confidence_profile": profile_summary(profile, profile_config),
            "edge_kind_filter": edge_filter,
            "depth_warnings": depth_warnings,
            "analysis_limits": analysis_limits,
            "confidence_summary": self._confidence_summary(
                impact_edges, unresolved_risks
            ),
            **confidence_impact,
            "unresolved_risks": unresolved_risks,
            "assurance": assurance,
        }

    def route(self, method: str, path: str) -> dict[str, Any]:
        normalized_path = path if path.startswith("/") else f"/{path}"
        node_id = route_id(method, normalized_path)

        def routable(row: dict[str, Any] | None) -> dict[str, Any] | None:
            # A router-local path is not a URL: target resolution and
            # entrypoint lookup both refuse it, and route() must not become
            # the one resolver that disagrees.
            if row is None or not self._is_routable_entrypoint(row):
                return None
            return row

        with self.store.connect() as conn:
            route = routable(self._node_by_id(conn, node_id))
            if route is None:
                # Same fallback vocabulary and precedence as
                # _route_query_aliases: exact first, then the remaining
                # wildcard spellings — including the other wildcard when the
                # query itself is ANY or ALL.
                for wildcard_method in _WILDCARD_ROUTE_METHODS:
                    if wildcard_method == method.upper():
                        continue
                    route = routable(
                        self._node_by_id(
                            conn,
                            route_id(wildcard_method, normalized_path),
                        )
                    )
                    if route is not None:
                        break
            resolved_route_id = route["id"] if route else None
            edges = self._edges_for_sources_kinds(
                conn,
                [resolved_route_id] if resolved_route_id else [],
                ["invokes", "injects"],
            )
            targets = self._nodes_by_ids(
                conn, sorted({edge["target"] for edge in edges})
            )

        targets_by_id = {target["id"]: target for target in targets}
        chain_records: dict[str, dict[str, Any]] = {}
        for fallback_index, edge in enumerate(
            sorted(
                edges,
                key=lambda item: (
                    int(
                        item.get("properties", {}).get("route_registration_line", 10**9)
                    ),
                    int(
                        item.get("properties", {}).get(
                            "route_registration_column", 10**9
                        )
                    ),
                    str(item.get("properties", {}).get("route_registration_id", "")),
                    int(item.get("properties", {}).get("route_handler_index", 10**9)),
                    item["target"],
                ),
            )
        ):
            properties = edge.get("properties", {})
            target = targets_by_id.get(edge["target"], {})
            identity = " ".join(
                str(target.get(field) or "") for field in ("name", "qualname", "id")
            ).lower()
            auth_like = any(
                token in identity
                for token in ("auth", "guard", "permission", "policy", "acl")
            )
            # One handler can serve the same route in several registrations;
            # those collapse to one edge carrying every registration's facts.
            for registration in _route_registration_facts(properties):
                position = registration.get("handler_index")
                if not isinstance(position, int):
                    position = fallback_index
                registration_id = registration.get("registration_id")
                if not isinstance(registration_id, str) or not registration_id:
                    registration_id = f"legacy:{resolved_route_id or node_id}"
                record = chain_records.setdefault(
                    registration_id,
                    {
                        "registration_id": registration_id,
                        "line": registration.get("line"),
                        "column": registration.get("column"),
                        "complete": registration.get("complete") is not False,
                        "handlers": [],
                        "handler_count": registration.get("handler_count"),
                    },
                )
                if registration.get("complete") is False:
                    record["complete"] = False
                record["handlers"].append(
                    {
                        "position": position,
                        "role": registration.get("role") or "unknown",
                        "target": {
                            key: target.get(key)
                            for key in (
                                "id",
                                "name",
                                "qualname",
                                "path",
                                "start_line",
                            )
                            if target.get(key) is not None
                        },
                        "edge_kind": edge["kind"],
                        "authorization_hint": {
                            "matches_name_heuristic": auth_like,
                            "confidence": "heuristic" if auth_like else "unavailable",
                            "non_claim": (
                                "Name matching does not prove that this middleware "
                                "enforces authentication or authorization."
                            ),
                        },
                    }
                )

        route_path = route.get("path") if route else None
        matched_registration_gaps = [
            warning.model_dump(mode="json")
            for warning in self.store.read_warnings()
            if (
                (warning.path is None or warning.path == route_path)
                and warning.kind.startswith("typescript_")
                and any(
                    marker in warning.kind for marker in ("route", "express_handlers")
                )
                and any(
                    marker in warning.kind
                    for marker in ("dynamic", "unresolved", "ambiguous", "truncated")
                )
            )
        ]
        # The list is a sample once it is bounded, and a caller cannot tell a
        # sample from the whole set without being told.
        dynamic_registration_gaps = matched_registration_gaps[
            :_ROUTE_REGISTRATION_GAP_LIMIT
        ]
        dynamic_registration_gap_summary = {
            "total": len(matched_registration_gaps),
            "returned": len(dynamic_registration_gaps),
            "omitted": len(matched_registration_gaps) - len(dynamic_registration_gaps),
        }
        route_properties = route.get("properties", {}) if route else {}
        # One edge can contribute to several chains, so neither chain order
        # nor within-chain order can come from edge iteration order: both are
        # established here from the registration's own coordinates.
        middleware_chains = sorted(
            chain_records.values(),
            key=lambda chain: (
                int(chain.get("line") or 10**9),
                int(chain.get("column") or 10**9),
                str(chain.get("registration_id") or ""),
            ),
        )
        for chain in middleware_chains:
            chain["handlers"].sort(
                key=lambda item: (
                    int(
                        item.get("position") if item.get("position") is not None else 0
                    ),
                    str(item.get("target", {}).get("id") or ""),
                )
            )
        middleware_chain = [
            {
                **item,
                "registration_id": chain["registration_id"],
                "chain_index": chain_index,
            }
            for chain_index, chain in enumerate(middleware_chains)
            for item in chain["handlers"]
        ]
        chain_order_available = (
            bool(middleware_chains)
            # The full match count, not the bounded sample: the claim is
            # about the gaps that exist, not the ones that fit.
            and not matched_registration_gaps
            and all(chain["complete"] for chain in middleware_chains)
            and all(item["role"] != "unknown" for item in middleware_chain)
            # An ordered chain claims to hold every position the registration
            # declared. A chain whose positions do not form 0..count-1 is
            # missing a handler, whatever the reason, and cannot be ordered.
            and all(_chain_positions_are_complete(chain) for chain in middleware_chains)
        )

        return {
            **self._envelope(
                status="available" if route else "unavailable",
                warnings=[] if route else [f"No route node matched {node_id!r}."],
            ),
            "query": {"method": method.upper(), "path": normalized_path},
            "route": route,
            "targets": targets,
            "edges": edges,
            "mount_context": (
                {
                    "router": route_properties.get("router"),
                    "mounted": route_properties.get("route_mounted"),
                    "router_local_path": route_properties.get("router_local_path"),
                    "resolved_path": route_properties.get("route_path"),
                }
                if route
                else None
            ),
            "middleware_chain": middleware_chain,
            "middleware_chains": middleware_chains,
            "dynamic_registration_gaps": dynamic_registration_gaps,
            "dynamic_registration_gap_summary": dynamic_registration_gap_summary,
            "evidence_boundary": {
                "chain_order": "available" if chain_order_available else "partial",
                "chain_order_scope": "per_registration",
                "registration_count": len(middleware_chains),
                "non_claims": [
                    "Dynamic registrations can be absent from this static chain.",
                    "Authorization hints are naming heuristics, not security conclusions.",
                ],
            },
        }

    def worker(self, target: str) -> dict[str, Any]:
        with self.store.connect() as conn:
            resolution = self._resolve_target(
                conn, target, policy=ResolutionPolicy.WORKER_SET
            )
            worker_ids = list(resolution.resolved_ids)
            workers = self._nodes_by_ids(conn, worker_ids)
            outgoing = self._edges_for_sources_kinds(
                conn, worker_ids, ["invokes", "consumes"]
            )
            incoming = self._edges_for_targets_kinds(conn, worker_ids, ["enqueues"])
            related_ids = sorted(
                {
                    *[edge["target"] for edge in outgoing],
                    *[edge["source"] for edge in incoming],
                }
            )
            related = self._nodes_by_ids(conn, related_ids)

        status = "available" if workers else "unavailable"
        warnings = [] if workers else [f"No worker or queue node matched {target!r}."]
        return {
            **self._envelope(status=status, warnings=warnings),
            "query": target,
            "target_resolution": resolution.to_dict(),
            "workers": workers,
            "related": related,
            "outgoing": outgoing,
            "incoming": incoming,
        }

    def entrypoint_flow(
        self, entrypoint: str, max_depth: int = 3, max_results: int = 50
    ) -> dict[str, Any]:
        max_depth = max(0, min(max_depth, 6))
        max_results = max(1, min(max_results, 200))
        with self.store.connect() as conn:
            resolution = self._resolve_target(
                conn, entrypoint, policy=ResolutionPolicy.ENTRYPOINT_SET
            )
            entrypoint_ids = list(resolution.resolved_ids)
            entrypoints_by_id = {
                node["id"]: node for node in self._nodes_by_ids(conn, entrypoint_ids)
            }
            entrypoints = [
                entrypoints_by_id[node_id]
                for node_id in entrypoint_ids
                if node_id in entrypoints_by_id
            ]
            edges, traversal_limits = self._forward_flow_edges(
                conn,
                entrypoint_ids,
                max_depth=max_depth,
                max_results=max_results,
            )
            node_ids = sorted(
                {
                    *entrypoint_ids,
                    *[edge["source"] for edge in edges],
                    *[edge["target"] for edge in edges],
                }
            )
            nodes = self._nodes_by_ids(conn, node_ids)

        return {
            **self._envelope(
                status="available" if entrypoints else "unavailable",
                capabilities=self._capabilities(entrypoint_flow="basic"),
                warnings=(
                    []
                    if entrypoints
                    else [f"No entrypoint node matched {entrypoint!r}."]
                ),
            ),
            "query": entrypoint,
            "target_resolution": resolution.to_dict(),
            "entrypoints": entrypoints,
            "nodes": nodes,
            "edges": edges,
            "analysis_limits": traversal_limits,
            "truncation": {
                "truncated": traversal_limits["traversal_truncated"],
                "scope": "graph_traversal",
                "reasons": traversal_limits["reasons"],
            },
            "max_depth": max_depth,
            "max_results": max_results,
        }

    def architecture(self) -> dict[str, Any]:
        with self.store.connect() as conn:
            node_kinds = {
                row["kind"]: row["count"]
                for row in conn.execute(
                    "SELECT kind, COUNT(*) AS count FROM nodes GROUP BY kind ORDER BY kind"
                )
            }
            edge_kinds = {
                row["kind"]: row["count"]
                for row in conn.execute(
                    "SELECT kind, COUNT(*) AS count FROM edges GROUP BY kind ORDER BY kind"
                )
            }
            entrypoints = {
                kind: node_kinds.get(kind, 0) for kind in entrypoint_stat_kinds()
            }
            resources = {kind: node_kinds.get(kind, 0) for kind in RESOURCE_NODE_KINDS}
            import_edges = [
                edge
                for row in conn.execute("""
                    SELECT * FROM edges
                    WHERE kind = 'imports'
                    ORDER BY source, target
                    """)
                for edge in [self._edge_row(row)]
                if not self._is_local_import_edge(edge)
            ]
            top_fan_out = [
                {"source": row["source"], "kind": row["kind"], "count": row["count"]}
                for row in conn.execute("""
                    SELECT source, kind, COUNT(*) AS count
                    FROM edges
                    GROUP BY source, kind
                    ORDER BY count DESC, source, kind
                    LIMIT 20
                    """)
            ]
            top_fan_in = [
                {"target": row["target"], "kind": row["kind"], "count": row["count"]}
                for row in conn.execute("""
                    SELECT target, kind, COUNT(*) AS count
                    FROM edges
                    GROUP BY target, kind
                    ORDER BY count DESC, target, kind
                    LIMIT 20
                    """)
            ]

        return {
            **self._envelope(capabilities=self._capabilities(architecture="basic")),
            "node_kinds": node_kinds,
            "edge_kinds": edge_kinds,
            "entrypoints": entrypoints,
            "resources": resources,
            "import_cycles": self._import_cycles(import_edges),
            "top_fan_out": top_fan_out,
            "top_fan_in": top_fan_in,
        }

    def tests(self, query: str) -> dict[str, Any]:
        with self.store.connect() as conn:
            resolution = self._resolve_target(conn, query)
            target_ids = list(resolution.resolved_ids)
            target_module_ids = self._module_ids_for_targets(conn, target_ids)
            candidates = self._test_candidates(
                conn, query, target_module_ids, target_ids
            )
            coverage_edges = self._coverage_edges_for_targets(
                conn, target_ids, target_module_ids
            )
            coverage_candidates = self._coverage_test_candidates(conn, coverage_edges)
            candidates = self._merge_test_candidates(candidates, coverage_candidates)
            test_gaps = self._test_gaps(
                target_ids=target_ids,
                target_module_ids=target_module_ids,
                coverage_edges=coverage_edges,
            )

        coverage_status = self.store.metadata.get("capabilities", {}).get(
            "coverage", "unavailable"
        )
        coverage_metrics = self.store.metadata.get("coverage_metrics", {})
        evidence = "coverage+heuristic" if coverage_candidates else "heuristic"
        warnings = []
        if coverage_status == "unavailable":
            warnings.append(
                "Coverage evidence is unavailable. Test recommendations use naming, "
                "path proximity, and import heuristics."
            )
        elif coverage_status == "partial":
            warnings.append(
                "Coverage evidence is partial or stale. Test recommendations include "
                "available coverage edges but need source review."
            )
        if not target_ids:
            warnings.append(self._resolution_warning(resolution))

        return {
            **self._envelope(
                status="available" if target_ids else "partial",
                capabilities=self._capabilities(
                    tests=(
                        "heuristic-and-coverage"
                        if coverage_status in {"available", "partial"}
                        and coverage_edges
                        else "heuristic"
                    )
                ),
                warnings=warnings,
            ),
            "query": query,
            "target_resolution": resolution.to_dict(),
            "resolved_targets": target_ids,
            "evidence": evidence,
            "coverage": {
                "status": coverage_status,
                "metrics": coverage_metrics,
                "edges": coverage_edges,
                "covered_targets": sorted({edge["target"] for edge in coverage_edges}),
            },
            "candidates": candidates,
            "test_gaps": test_gaps,
        }

    def similar(
        self, query: str, max_results: int = 10, detail_level: str = "summary"
    ) -> dict[str, Any]:
        max_results = max(1, min(max_results, 50))
        with self.store.connect() as conn:
            resolution = self._resolve_target(conn, query)
            resolved_ids = list(resolution.resolved_ids)
            # Bound the seed before the first query, not inside the walk it
            # feeds: the queries below are what a file target's full symbol
            # list would overrun, and a family computed from a wider seed
            # than the walk keeps would disagree with its own disclosure.
            # The full set drives direct similarity: it decides which nodes
            # are the query's own members (and so excluded from the answer)
            # and which edges are read at all. Bounding it here made symbol
            # 501 report as similar to its own file and dropped every
            # implementation linked only to a dropped target. Only the family
            # walk is bounded, inside the walk. Edge reads batch, so the full
            # list is safe to bind.
            target_ids = resolved_ids
            target_set = set(target_ids)
            outgoing = self._edges_for_sources(conn, target_ids, "similar_to")
            incoming = self._edges_for_targets(conn, target_ids, "similar_to")
            by_other_id: dict[str, dict[str, Any]] = {}
            for edge in [*outgoing, *incoming]:
                other_id = (
                    edge["target"] if edge["source"] in target_set else edge["source"]
                )
                if other_id in target_set:
                    continue
                current = by_other_id.get(other_id)
                if current is None or self._edge_score(edge) > self._edge_score(
                    current["edge"]
                ):
                    by_other_id[other_id] = {"id": other_id, "edge": edge}
            similar_ids = [
                item["id"]
                for item in sorted(
                    by_other_id.values(),
                    key=lambda item: (-self._edge_score(item["edge"]), item["id"]),
                )[:max_results]
            ]
            nodes_by_id = {
                node["id"]: node for node in self._nodes_by_ids(conn, similar_ids)
            }
            family_ids, family_truncated = self._similarity_family_ids(
                conn,
                target_ids,
                seed_edges=[*outgoing, *incoming],
            )
            profile_nodes = {
                node["id"]: node
                for node in self._nodes_by_ids(conn, sorted(family_ids))
            }

        information_by_id = {
            node_id: _similarity_information(
                node.get("properties", {}).get("similarity", {})
                if isinstance(node.get("properties", {}).get("similarity"), dict)
                else {}
            )
            for node_id, node in profile_nodes.items()
        }
        stale_files = set(self._freshness().stale_files)
        family_members = [
            {
                "id": node_id,
                "name": node.get("name"),
                "path": node.get("path"),
                "start_line": node.get("start_line"),
                "low_information": information_by_id[node_id]["low_information"],
                "modified_since_index": bool(
                    node.get("path") and node["path"] in stale_files
                ),
            }
            for node_id, node in sorted(profile_nodes.items())
        ]
        modified_members = [
            member for member in family_members if member["modified_since_index"]
        ]
        family_profiles = [
            node.get("properties", {}).get("similarity", {})
            for node in profile_nodes.values()
        ]
        algorithms = sorted(
            {
                str(profile["algorithm"])
                for profile in family_profiles
                if isinstance(profile, dict) and profile.get("algorithm")
            }
        )
        buckets = sorted(
            {
                str(profile["bucket"])
                for profile in family_profiles
                if isinstance(profile, dict) and profile.get("bucket") is not None
            }
        )

        similar = []
        for item in sorted(
            by_other_id.values(),
            key=lambda value: (-self._edge_score(value["edge"]), value["id"]),
        )[:max_results]:
            node = nodes_by_id.get(item["id"])
            if node is None:
                continue
            edge = item["edge"]
            similar.append(
                {
                    "node": node,
                    "edge": (
                        edge if detail_level != "summary" else self._compact_edge(edge)
                    ),
                    "score": self._edge_score(edge),
                    "reasons": edge.get("properties", {}).get("reasons", []),
                    "low_information": information_by_id.get(item["id"], {}).get(
                        "low_information", True
                    ),
                    "information_quality": information_by_id.get(item["id"], {}),
                }
            )

        return {
            **self._envelope(
                status="available" if target_ids else "partial",
                capabilities=self._capabilities(
                    similarity=self.store.metadata.get("capabilities", {}).get(
                        "similarity", "unavailable"
                    )
                ),
                warnings=[] if target_ids else [self._resolution_warning(resolution)],
            ),
            "query": query,
            "target_resolution": resolution.to_dict(),
            "resolved_targets": target_ids,
            "detail_level": detail_level,
            "max_results": max_results,
            "similar": similar,
            "pattern_family": {
                "algorithms": algorithms,
                "buckets": buckets,
                "member_count": len(family_members),
                "members": family_members[:100],
                "truncated_member_count": max(0, len(family_members) - 100),
                "family_traversal_truncated": family_truncated,
                "family_traversal_limit": SIMILARITY_FAMILY_NODE_LIMIT,
                "low_information_member_count": sum(
                    1 for member in family_members if member["low_information"]
                ),
                "modified_member_count": len(modified_members),
                "modified_members": modified_members[:25],
                # A universal claim over the family: a truncated traversal has
                # not seen every member, so it cannot be asserted.
                "only_one_member_modified": (
                    not family_truncated
                    and len(family_members) > 1
                    and len(modified_members) == 1
                ),
                "non_claim": (
                    "Family membership is based on indexed structural similarity; "
                    "it does not prove shared behavior or intent."
                ),
            },
        }

    def references(
        self,
        query: str,
        *,
        backend: str = "auto",
        max_results: int = 200,
    ) -> dict[str, Any]:
        if backend not in {"auto", "scip", "typescript_language_service"}:
            raise ValueError(f"Unsupported precise-reference backend: {backend}")
        max_results = max(1, min(max_results, 1000))
        with self.store.connect() as conn:
            resolution = self._resolve_target(
                conn, query, policy=ResolutionPolicy.SYMBOL_ONE
            )
            target_ids = list(resolution.resolved_ids)
            nodes = self._nodes_by_ids(conn, target_ids)
            scip_edges = self._edges_for_targets_kinds(conn, target_ids, ["references"])
        freshness = self._freshness().model_dump(mode="json")
        if not target_ids or not nodes:
            return {
                **self._envelope(
                    status="partial",
                    warnings=[self._resolution_warning(resolution)],
                ),
                "query": query,
                "target_resolution": resolution.to_dict(),
                "backend": None,
                "references": [],
                "summary": {"total": 0, "returned": 0},
            }
        if freshness.get("stale"):
            return {
                **self._envelope(
                    status="partial",
                    warnings=[
                        "Precise reference lookup refused a stale index because target coordinates may have moved."
                    ],
                ),
                "query": query,
                "target_resolution": resolution.to_dict(),
                "backend": None,
                "references": [],
                "summary": {"total": 0, "returned": 0},
            }
        if scip_edges and backend in {"auto", "scip"}:
            references = _scip_reference_locations(scip_edges)
            truncation = _reference_truncation(len(references), max_results)
            return {
                **self._envelope(status="available"),
                "query": query,
                "target_resolution": resolution.to_dict(),
                "backend": "scip",
                "references": references[:max_results],
                "summary": {
                    "total": len(references),
                    "returned": min(len(references), max_results),
                    "truncated": truncation["truncated"],
                },
                "truncation": truncation,
            }
        target = nodes[0]
        path = target.get("path")
        if (
            backend in {"auto", "typescript_language_service"}
            and isinstance(path, str)
            and Path(path).suffix.lower() in TYPESCRIPT_REFERENCE_EXTENSIONS
        ):
            result = typescript_language_service_references(
                repo_root=Path(str(self.store.metadata.get("repo_root"))),
                files=[record.path for record in self.store.read_files()],
                target=target,
            )
            references = list(result.get("references", []))
            result_available = result.get("status") == "available"
            result_warning = str(
                result.get("reason") or "Precise reference lookup unavailable."
            )
            truncation = _reference_truncation(len(references), max_results)
            return {
                **self._envelope(
                    status="available" if result_available else "partial",
                    warnings=[] if result_available else [result_warning],
                ),
                "query": query,
                "target_resolution": resolution.to_dict(),
                "backend": result.get("backend"),
                "references": references[:max_results],
                "summary": {
                    **result.get("summary", {}),
                    "total": len(references),
                    "returned": min(len(references), max_results),
                    "truncated": truncation["truncated"],
                },
                "truncation": truncation,
            }
        return {
            **self._envelope(
                status="partial",
                warnings=["No precise-reference backend is available for this target."],
            ),
            "query": query,
            "target_resolution": resolution.to_dict(),
            "backend": None,
            "references": [],
            "summary": {"total": 0, "returned": 0},
        }

    def _envelope(
        self,
        *,
        status: str = "available",
        capabilities: dict[str, str] | None = None,
        warnings: list[Any] | None = None,
    ) -> dict[str, Any]:
        response_warnings = [] if warnings is None else list(warnings)
        response_warnings.extend(self._stale_target_warnings())
        freshness = self._freshness().model_dump(mode="json")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "index_version": self.store.metadata.get("index_version"),
            "freshness": freshness,
            "status": status,
            "capabilities": (
                capabilities if capabilities is not None else self._capabilities()
            ),
            "warnings": response_warnings,
        }
        recovery_action = stale_recovery_action(freshness)
        if recovery_action is not None:
            payload["recovery_action"] = recovery_action
        return payload

    def _capabilities(self, **overrides: str) -> dict[str, str]:
        return {**self.store.metadata.get("capabilities", {}), **overrides}

    def _freshness(self) -> Freshness:
        if self._freshness_cache is None:
            self._freshness_cache = compute_freshness(self.store)
        return self._freshness_cache

    def _stale_target_warnings(self) -> list[str]:
        if self._stale_warning_cache is not None:
            return self._stale_warning_cache
        try:
            with self.store.connect() as conn:
                rows = conn.execute("""
                    SELECT properties_json
                    FROM diagnostics
                    WHERE diagnostic_kind = 'stale_target'
                    ORDER BY diagnostic_id
                    """).fetchall()
        except Exception:
            self._stale_warning_cache = []
            return self._stale_warning_cache

        total = 0
        recommended_paths: set[str] = set()
        for row in rows:
            properties = json.loads(row["properties_json"])
            count = properties.get("stale_edges_count", 0)
            if isinstance(count, int):
                total += count
            paths = properties.get("reanalysis_recommended_paths", [])
            if isinstance(paths, list):
                recommended_paths.update(str(path) for path in paths if path)

        if not rows:
            self._stale_warning_cache = []
        else:
            suffix = (
                f" Reanalysis recommended for: {', '.join(sorted(recommended_paths)[:5])}."
                if recommended_paths
                else ""
            )
            self._stale_warning_cache = [
                f"target_stale: {total} stale edge(s) were detected in the current index.{suffix}"
            ]
        return self._stale_warning_cache

    def _unresolved_filter_scope(
        self, conn: Any, target: str | None
    ) -> tuple[list[str], set[str], set[str], TargetResolution | None]:
        if target is None:
            return [], set(), set(), None

        target_paths: set[str] = set()
        target_qualname_prefixes: set[str] = set()
        normalized_path = self.target_resolver._normalized_path(target)
        if self.target_resolver._looks_like_path(conn, normalized_path):
            target_paths.add(normalized_path)

        resolution = self._resolve_target(conn, target)
        target_ids = list(resolution.resolved_ids)
        for node in self._nodes_by_ids(conn, target_ids):
            path = node.get("path")
            if node.get("kind") == "module" and isinstance(path, str) and path:
                target_paths.add(path)
            qualname = node.get("qualname")
            if node.get("kind") == "class" and isinstance(qualname, str):
                target_qualname_prefixes.add(f"{qualname}.")
        return target_ids, target_paths, target_qualname_prefixes, resolution

    @staticmethod
    def _matches_unresolved_filter(
        diagnostic: dict[str, Any],
        target_ids: set[str],
        target_paths: set[str],
        target_qualname_prefixes: set[str],
    ) -> bool:
        if not target_ids and not target_paths and not target_qualname_prefixes:
            return False
        properties = diagnostic.get("properties", {})
        source_qualname = properties.get("source_qualname")
        return (
            properties.get("source_scope") in target_ids
            or diagnostic.get("path") in target_paths
            or (
                isinstance(source_qualname, str)
                and any(
                    source_qualname.startswith(prefix)
                    for prefix in target_qualname_prefixes
                )
            )
        )

    def _diagnostic_rows(
        self, conn: Any, *, diagnostic_kind: str | None = None
    ) -> list[dict[str, Any]]:
        if diagnostic_kind:
            rows = conn.execute(
                """
                SELECT *
                FROM diagnostics
                WHERE diagnostic_kind = ?
                ORDER BY path, start_line, diagnostic_id
                """,
                (diagnostic_kind,),
            ).fetchall()
        else:
            rows = conn.execute("""
                SELECT *
                FROM diagnostics
                ORDER BY diagnostic_kind, path, start_line, diagnostic_id
                """).fetchall()
        return [self._diagnostic_row(row) for row in rows]

    @staticmethod
    def _merge_metrics(conn: Any) -> dict[str, Any]:
        row = conn.execute("""
            SELECT metrics_json
            FROM merge_metrics
            ORDER BY created_at DESC
            LIMIT 1
            """).fetchone()
        return json.loads(row["metrics_json"]) if row else {}

    @staticmethod
    def _count_edge_field(conn: Any, field: str) -> dict[str, int]:
        if field not in {"kind", "confidence"}:
            raise ValueError(f"Unsupported edge count field: {field}")
        return {row[field]: row["count"] for row in conn.execute(f"""
                SELECT {field}, COUNT(*) AS count
                FROM edges
                GROUP BY {field}
                ORDER BY {field}
                """)}

    @staticmethod
    def _node_property_records(
        nodes: list[dict[str, Any]],
        *,
        property_name: str,
        scope_field: str,
        limit: int,
    ) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for node in nodes:
            values = node.get("properties", {}).get(property_name, [])
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict):
                    continue
                record = dict(value)
                record.setdefault(scope_field, node["id"])
                record.setdefault("scope_kind", node["kind"])
                record.setdefault("source_qualname", node.get("qualname"))
                records.append(record)
        records.sort(
            key=lambda item: (
                str(item.get("path") or ""),
                int_or_none(item.get("line") or item.get("start_line")) or 0,
                int_or_none(item.get("column")) or 0,
                str(item.get("name") or item.get("kind") or ""),
            )
        )
        returned = records[:limit]
        return {
            "summary": {
                "total": len(records),
                "returned": len(returned),
                "limit": limit,
            },
            "records": returned,
        }

    @classmethod
    def _confidence_impact(
        cls, edges: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        buckets: dict[str, list[dict[str, Any]]] = {
            bucket: [] for bucket in CONFIDENCE_BUCKETS.values()
        }
        for edge in edges:
            bucket = CONFIDENCE_BUCKETS.get(
                edge.get("confidence", ""), "heuristic_impact"
            )
            buckets[bucket].append(edge)
        return buckets

    @staticmethod
    def _confidence_summary(
        edges: list[dict[str, Any]], unresolved_risks: dict[str, Any]
    ) -> dict[str, Any]:
        counts = Counter(edge.get("confidence", "unknown") for edge in edges)
        return {
            "confirmed_edges": counts.get("confirmed", 0),
            "inferred_edges": counts.get("inferred", 0),
            "runtime_only_edges": counts.get("runtime-only", 0),
            "heuristic_edges": counts.get("heuristic", 0),
            "unresolved_edges": counts.get("unresolved", 0),
            "unresolved_risks": unresolved_risks.get("summary", {}).get("total", 0),
            "by_confidence": dict(sorted(counts.items())),
        }

    def _unresolved_risks(
        self,
        conn: Any,
        *,
        target_ids: list[str],
        affected_symbol_ids: list[str],
        affected_module_ids: list[str],
        limit: int,
    ) -> dict[str, Any]:
        scope_ids = sorted({*target_ids, *affected_symbol_ids, *affected_module_ids})
        scope_paths = {
            node.get("path")
            for node in self._nodes_by_ids(conn, scope_ids)
            if node.get("path")
        }
        diagnostics = [
            item
            for item in self._diagnostic_rows(
                conn, diagnostic_kind="unresolved_callsite"
            )
            if item.get("properties", {}).get("source_scope") in scope_ids
            or item.get("path") in scope_paths
        ]
        target_set = set(target_ids)
        affected_set = set(affected_symbol_ids)
        module_set = set(affected_module_ids)
        for item in diagnostics:
            source = item.get("properties", {}).get("source_scope")
            inclusion = (
                "target"
                if source in target_set
                else (
                    "affected_symbol"
                    if source in affected_set
                    else "affected_module" if source in module_set else "same_file"
                )
            )
            item["inclusion_scope"] = inclusion
        priorities = {
            "target": 0,
            "affected_symbol": 1,
            "affected_module": 2,
            "same_file": 3,
        }
        diagnostics.sort(
            key=lambda item: (
                priorities[item["inclusion_scope"]],
                item.get("path") or "",
                item.get("start_line") or 0,
                item.get("properties", {}).get("column") or 0,
                item.get("diagnostic_id") or "",
            )
        )
        returned = diagnostics[:limit]
        failed_strategies = Counter(
            item.get("properties", {}).get("failed_strategy", "unknown")
            for item in diagnostics
        )
        return {
            "summary": {
                "total": len(diagnostics),
                "returned": len(returned),
                "limit": limit,
                "by_failed_strategy": dict(sorted(failed_strategies.items())),
                "by_inclusion_scope": dict(
                    Counter(item["inclusion_scope"] for item in diagnostics)
                ),
            },
            "scope": "targets, affected symbols and importing modules, expanded to their files",
            "items": returned,
        }

    @staticmethod
    def _unique_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: dict[tuple[Any, ...], dict[str, Any]] = {}
        for edge in edges:
            key = (
                edge.get("source"),
                edge.get("target"),
                edge.get("kind"),
                edge.get("semantic_role"),
                edge.get("confidence"),
            )
            unique.setdefault(key, edge)
        return sorted(
            unique.values(),
            key=lambda edge: (
                str(edge.get("source") or ""),
                str(edge.get("target") or ""),
                str(edge.get("kind") or ""),
                str(edge.get("semantic_role") or ""),
                str(edge.get("confidence") or ""),
            ),
        )

    @staticmethod
    def _edge_kind_filter(
        *,
        include_edge_kinds: list[str] | None,
        exclude_edge_kinds: list[str] | None,
    ) -> dict[str, list[str]]:
        include = sorted({kind for kind in include_edge_kinds or [] if kind})
        exclude = sorted({kind for kind in exclude_edge_kinds or [] if kind})
        return {"include": include, "exclude": exclude}

    @staticmethod
    def _architecture_summary_from_node_kinds(
        node_kinds: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "entrypoints": {
                kind: int(node_kinds.get(kind, 0) or 0)
                for kind in entrypoint_stat_kinds()
            },
            "resources": {
                kind: int(node_kinds.get(kind, 0) or 0) for kind in RESOURCE_NODE_KINDS
            },
        }

    @staticmethod
    def _summarize_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": node.get("id"),
                "kind": node.get("kind"),
                "label": node.get("name") or node.get("qualname") or node.get("id"),
                "name": node.get("name"),
                "qualname": node.get("qualname"),
                "path": node.get("path"),
                "start_line": node.get("start_line"),
                "end_line": node.get("end_line"),
            }
            for node in nodes[:50]
        ]

    @staticmethod
    def _summarize_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "source": edge.get("source"),
                "target": edge.get("target"),
                "kind": edge.get("kind"),
                "confidence": edge.get("confidence"),
                "semantic_role": edge.get("semantic_role"),
            }
            for edge in edges[:100]
        ]

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _filter_edges_by_kind(
        edges: list[dict[str, Any]], *, edge_filter: dict[str, list[str]]
    ) -> list[dict[str, Any]]:
        include = set(edge_filter.get("include", []))
        exclude = set(edge_filter.get("exclude", []))
        return [
            edge
            for edge in edges
            if (not include or edge.get("kind") in include)
            and edge.get("kind") not in exclude
        ]

    @staticmethod
    def _filter_edges_by_confidence(
        edges: list[dict[str, Any]],
        *,
        profile_config: ConfidenceProfileConfig,
    ) -> list[dict[str, Any]]:
        """Filter edges by the profile's allowed confidence levels."""
        allowed = profile_config.allowed_confidences
        return [
            edge for edge in edges if edge.get("confidence", "confirmed") in allowed
        ]

    def _resolve_target(
        self,
        conn: Any,
        query: str,
        *,
        policy: ResolutionPolicy = ResolutionPolicy.ANALYSIS_TARGET,
    ) -> TargetResolution:
        return self.target_resolver.resolve(conn, query, policy=policy)

    @staticmethod
    def _resolution_warning(
        resolution: TargetResolution, target_kind: str = "graph node"
    ) -> str:
        if resolution.status == "ambiguous":
            candidate_ids = [
                str(item.get("id")) for item in resolution.candidates if item.get("id")
            ]
            suffix = (
                f" Candidates: {', '.join(candidate_ids)}." if candidate_ids else ""
            )
            return (
                f"Target {resolution.query!r} is ambiguous; no {target_kind} was "
                f"selected.{suffix} Use a typed node id or exact qualname."
            )
        return f"No {target_kind} resolved for target {resolution.query!r}."

    def _module_ids_for_targets(self, conn: Any, target_ids: list[str]) -> list[str]:
        modules: set[str] = set()
        for node in self._nodes_by_ids(conn, target_ids):
            if node["kind"] == "module":
                modules.add(node["id"])
            elif node["path"]:
                module_row = conn.execute(
                    "SELECT id FROM nodes WHERE kind = 'module' AND path = ?",
                    (node["path"],),
                ).fetchone()
                if module_row:
                    modules.add(module_row["id"])
        return sorted(modules)

    def _reverse_call_impact(
        self,
        conn: Any,
        target_ids: list[str],
        max_depth: int,
        *,
        path_limits: PathQueryLimits = DEFAULT_PATH_QUERY_LIMITS,
        allowed_confidences: frozenset[str] | None = None,
    ) -> tuple[list[dict[str, Any]], bool, list[str]]:
        """BFS reverse traversal with guardrails.

        Returns ``(edges, truncated, reasons)`` where *truncated* is True
        **only** when there is evidence that matching edges were not returned.

        Evidence sources:
        - ``sql_capped``: the SQL helper hit per-target or global limits.
        - ``timeout``: wall-clock deadline exceeded.
        - ``batch_overflow``: the inner loop stopped before consuming all
          edges in the current SQL batch.
        - ``budget_full + next-depth exists``: global ``max_edges`` reached
          AND there are still unseen edges in the next BFS depth (verified
          by a lightweight ``SELECT 1 ... LIMIT 1`` existence query).
        """
        seen_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        frontier = set(target_ids)
        visited_nodes = set(target_ids)
        truncated = False
        truncation_reasons: set[str] = set()
        deadline = time.monotonic() + path_limits.timeout_seconds

        for depth_idx in range(max_depth):
            if not frontier:
                break
            if time.monotonic() > deadline:
                truncated = True
                truncation_reasons.add("timeout")
                break
            remaining = max(path_limits.max_edges - len(seen_edges), 0)
            if remaining == 0:
                # Budget was filled by a previous iteration.  Check whether
                # there are real unseen edges at this depth before warning.
                if depth_idx < max_depth and self._has_unseen_edges(
                    conn,
                    sorted(frontier),
                    CALL_QUERY_EDGE_KINDS,
                    seen_edges,
                    allowed_confidences=allowed_confidences,
                ):
                    truncated = True
                    truncation_reasons.add("max_edges")
                break
            edges, sql_capped = self._edges_for_targets_kinds_limited(
                conn,
                sorted(frontier),
                CALL_QUERY_EDGE_KINDS,
                per_target_limit=path_limits.max_fan_in,
                global_limit=remaining,
                allowed_confidences=allowed_confidences,
            )
            if sql_capped:
                truncated = True
                truncation_reasons.add("max_fan_in_or_edges")

            # Process edges: build next frontier BEFORE budget check so
            # we always know whether deeper exploration is possible.
            next_frontier: set[str] = set()
            budget_full = False
            for edge_idx, edge in enumerate(edges):
                source = edge["source"]
                key = (source, edge["target"], edge["kind"])
                if key not in seen_edges:
                    seen_edges[key] = edge
                if source not in visited_nodes:
                    next_frontier.add(source)
                    visited_nodes.add(source)
                if len(seen_edges) >= path_limits.max_edges:
                    budget_full = True
                    if edge_idx + 1 < len(edges):
                        truncated = True
                        truncation_reasons.add("max_edges")
                    break

            frontier = next_frontier

            if budget_full:
                # Budget is full.  If we haven't already marked truncated
                # (from sql_capped, batch overflow, etc.) check whether the
                # next depth has real unseen edges.
                if (
                    not truncated
                    and depth_idx + 1 < max_depth
                    and frontier
                    and self._has_unseen_edges(
                        conn,
                        sorted(frontier),
                        CALL_QUERY_EDGE_KINDS,
                        seen_edges,
                        allowed_confidences=allowed_confidences,
                    )
                ):
                    truncated = True
                    truncation_reasons.add("max_edges")
                break

        result = sorted(
            seen_edges.values(),
            key=lambda edge: (edge["source"], edge["target"], edge["kind"]),
        )
        return result, truncated, sorted(truncation_reasons)

    def _test_candidates(
        self,
        conn: Any,
        query: str,
        target_module_ids: list[str],
        target_ids: list[str],
    ) -> list[dict[str, Any]]:
        candidates: dict[str, dict[str, Any]] = {}

        for edge in self._edges_for_targets(conn, target_module_ids, "imports"):
            for evidence in edge["evidence"]:
                path = evidence.get("path")
                if self._is_test_path(path):
                    candidates[path] = {
                        "path": path,
                        "reason": f"imports {edge['target']}",
                        "evidence": "heuristic",
                    }

        for node in self._nodes_by_ids(conn, target_ids):
            path = node.get("path")
            if not path or self._is_test_path(path):
                continue
            stem = Path(path).stem
            token = stem.removeprefix("test_")
            rows = conn.execute("""
                SELECT path FROM files
                WHERE path LIKE '%/tests/%'
                   OR path LIKE 'backend/tests/%'
                   OR path LIKE 'tests/%'
                ORDER BY path
                """).fetchall()
            for row in rows:
                test_path = row["path"]
                test_name = Path(test_path).stem
                if token in test_name or test_name == f"test_{token}":
                    candidates.setdefault(
                        test_path,
                        {
                            "path": test_path,
                            "reason": f"name matches {stem}",
                            "evidence": "heuristic",
                        },
                    )

        if not candidates and self._is_test_path(query):
            candidates[query.replace("\\", "/")] = {
                "path": query.replace("\\", "/"),
                "reason": "target is already a test path",
                "evidence": "heuristic",
            }

        return sorted(candidates.values(), key=lambda item: item["path"])

    def _coverage_edges_for_targets(
        self, conn: Any, target_ids: list[str], target_module_ids: list[str]
    ) -> list[dict[str, Any]]:
        target_set = sorted({*target_ids, *target_module_ids})
        return self._edges_for_targets(conn, target_set, "covers")

    def _coverage_test_candidates(
        self, conn: Any, coverage_edges: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        candidates: dict[str, dict[str, Any]] = {}
        source_ids = sorted({edge["source"] for edge in coverage_edges})
        for node in self._nodes_by_ids(conn, source_ids):
            path = node.get("path")
            if not self._is_test_path(path):
                continue
            candidates[path] = {
                "path": path,
                "reason": "runtime coverage covers target",
                "evidence": "coverage",
            }
        return sorted(candidates.values(), key=lambda item: item["path"])

    @staticmethod
    def _merge_test_candidates(
        heuristic: list[dict[str, Any]], coverage: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        merged = {item["path"]: dict(item) for item in heuristic}
        for item in coverage:
            existing = merged.get(item["path"])
            if existing is None:
                merged[item["path"]] = dict(item)
                continue
            existing["reason"] = f"{existing['reason']}; {item['reason']}"
            existing["evidence"] = "coverage+heuristic"
        return sorted(merged.values(), key=lambda item: item["path"])

    def _test_gaps(
        self,
        *,
        target_ids: list[str],
        target_module_ids: list[str],
        coverage_edges: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not target_ids:
            return [
                {
                    "target": None,
                    "reason": "target did not resolve to an indexed node",
                    "severity": "unknown",
                }
            ]

        coverage_status = self.store.metadata.get("capabilities", {}).get(
            "coverage", "unavailable"
        )
        if coverage_status == "unavailable" or (
            coverage_status == "partial" and not coverage_edges
        ):
            return [
                {
                    "target": target_id,
                    "reason": "coverage data unavailable",
                    "severity": "unknown",
                }
                for target_id in target_ids
            ]

        covered_targets = {edge["target"] for edge in coverage_edges}
        # Every coverage edge names exactly what it covers. A module edge is
        # emitted when any line in the file executed (`"level": "file"` in
        # arcgraph/analyzers/coverage.py), so it covers the module target and
        # nothing else; a symbol edge is emitted only when the symbol's own
        # lines were hit. A gap is therefore reported for each requested
        # target that no edge names — the enclosing module of a symbol is not
        # a substitute for the symbol.
        del target_module_ids
        return [
            {
                "target": target_id,
                "reason": "no runtime coverage edge for target",
                "severity": "medium",
            }
            for target_id in target_ids
            if target_id not in covered_targets
        ]

    @staticmethod
    def _is_test_path(path: Any) -> bool:
        if not isinstance(path, str):
            return False
        normalized = path.replace("\\", "/")
        return (
            normalized.startswith("tests/")
            or "/tests/" in normalized
            or normalized.startswith("backend/tests/")
        )

    @staticmethod
    def _edge_score(edge: dict[str, Any]) -> float:
        score = edge.get("properties", {}).get("score", 0.0)
        return float(score) if isinstance(score, int | float) else 0.0

    @staticmethod
    def _compact_edge(edge: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": edge["source"],
            "target": edge["target"],
            "kind": edge["kind"],
            "confidence": edge["confidence"],
            "properties": edge.get("properties", {}),
        }

    def _edges_for_targets(
        self, conn: Any, target_ids: list[str], kind: str
    ) -> list[dict[str, Any]]:
        return self._edges_for_targets_kinds(conn, target_ids, [kind])

    def _edges_for_sources(
        self, conn: Any, source_ids: list[str], kind: str
    ) -> list[dict[str, Any]]:
        return self._edges_for_sources_kinds(conn, source_ids, [kind])

    def _similarity_family_ids(
        self,
        conn: Any,
        target_ids: list[str],
        *,
        seed_edges: list[dict[str, Any]],
    ) -> tuple[set[str], bool]:
        """Return the target's similarity component, bounded, and whether the
        walk stopped early.

        The component is unbounded in principle: a generated-code repository
        can link thousands of look-alike symbols into one family, and every
        visited node costs a hydrated edge row. The walk therefore stops at
        SIMILARITY_FAMILY_NODE_LIMIT nodes for every input, and the caller
        discloses the partial traversal rather than presenting a truncated
        component as the whole one.
        """

        # The seed counts against the limit too: one file target resolves to
        # every symbol the file declares, so the family can exceed the limit
        # before a single edge is walked. Bounding only the expansion left
        # member_count above the stated limit while claiming nothing was cut.
        seed_ids = [node_id for node_id in target_ids if node_id]
        truncated = len(seed_ids) > SIMILARITY_FAMILY_NODE_LIMIT
        family_ids = set(seed_ids[:SIMILARITY_FAMILY_NODE_LIMIT])
        seen_edges: set[tuple[str, str]] = set()

        def discover(edges: list[dict[str, Any]]) -> set[str]:
            nonlocal truncated
            discovered: set[str] = set()
            for edge in edges:
                edge_key = (edge["source"], edge["target"])
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                for node_id in edge_key:
                    if node_id not in family_ids:
                        if len(family_ids) >= SIMILARITY_FAMILY_NODE_LIMIT:
                            truncated = True
                            return discovered
                        family_ids.add(node_id)
                        discovered.add(node_id)
            return discovered

        frontier = set() if truncated else discover(seed_edges)
        while frontier and not truncated:
            frontier_ids = sorted(frontier)
            edges: list[dict[str, Any]] = []
            for start in range(0, len(frontier_ids), SQLITE_IN_BATCH_SIZE):
                chunk = frontier_ids[start : start + SQLITE_IN_BATCH_SIZE]
                edges.extend(self._edges_for_sources(conn, chunk, "similar_to"))
                edges.extend(self._edges_for_targets(conn, chunk, "similar_to"))

            frontier = discover(edges)
        return family_ids, truncated

    def _edges_for_targets_kinds(
        self, conn: Any, target_ids: list[str], kinds: list[str]
    ) -> list[dict[str, Any]]:
        return self._edges_for_endpoint_kinds(conn, "target", target_ids, kinds)

    def _edges_for_endpoint_kinds(
        self, conn: Any, column: str, ids: list[str], kinds: list[str]
    ) -> list[dict[str, Any]]:
        """Read edges by endpoint, in batches SQLite can bind.

        One file target resolves to every symbol the file declares, so an id
        list is bounded only by the size of the file — past SQLite's variable
        limit the query raises instead of returning. Node reads already batch
        on SQLITE_IN_BATCH_SIZE; edge reads did not, which is the same hazard
        one table over.
        """

        if not ids or not kinds:
            return []
        kind_placeholders = ",".join("?" for _ in kinds)
        rows: list[dict[str, Any]] = []
        for start in range(0, len(ids), SQLITE_IN_BATCH_SIZE):
            chunk = ids[start : start + SQLITE_IN_BATCH_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                self._edge_row(row)
                for row in conn.execute(
                    f"""
                    SELECT * FROM edges
                    WHERE {column} IN ({placeholders})
                      AND kind IN ({kind_placeholders})
                    ORDER BY source, target, kind
                    """,
                    (*chunk, *kinds),
                )
            )
        if len(ids) > SQLITE_IN_BATCH_SIZE:
            rows.sort(key=lambda row: (row["source"], row["target"], row["kind"]))
        return rows

    def _edges_for_targets_kinds_limited(
        self,
        conn: Any,
        target_ids: list[str],
        kinds: list[str],
        *,
        per_target_limit: int,
        global_limit: int,
        allowed_confidences: frozenset[str] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Like ``_edges_for_targets_kinds`` but with SQL-level limits.

        Uses ``ROW_NUMBER() OVER (PARTITION BY target)`` to cap fan-in
        per target and a global ``LIMIT`` to cap total materialised rows.

        Returns ``(edges, capped)`` where *capped* is True when either
        per-target or global limit was hit.
        """
        if not target_ids or not kinds:
            return [], False
        if len(target_ids) > SQLITE_IN_BATCH_SIZE:
            # The per-target window and the global cap both still hold when
            # the id list is split: each batch keeps its own per-target rank,
            # and the merged result is re-capped below.
            merged: list[dict[str, Any]] = []
            any_capped = False
            for start in range(0, len(target_ids), SQLITE_IN_BATCH_SIZE):
                chunk_rows, chunk_capped = self._edges_for_targets_kinds_limited(
                    conn,
                    target_ids[start : start + SQLITE_IN_BATCH_SIZE],
                    kinds,
                    per_target_limit=per_target_limit,
                    global_limit=global_limit,
                    allowed_confidences=allowed_confidences,
                )
                any_capped = any_capped or chunk_capped
                merged.extend(chunk_rows)
            merged.sort(key=lambda row: (row["source"], row["target"], row["kind"]))
            if len(merged) > global_limit:
                merged = merged[:global_limit]
                any_capped = True
            return merged, any_capped
        target_ph = ",".join("?" for _ in target_ids)
        kind_ph = ",".join("?" for _ in kinds)
        # Fetch per_target_limit + 1 rows per target so we can detect
        # per-target truncation via sentinel rows, then apply a global cap.
        sentinel = per_target_limit + 1
        fetch_limit = global_limit + 1
        # Build optional confidence filter clause
        if allowed_confidences:
            conf_list = sorted(allowed_confidences)
            conf_ph = ",".join("?" for _ in conf_list)
            conf_clause = f"AND confidence IN ({conf_ph})"
            conf_params: tuple[str, ...] = tuple(conf_list)
        else:
            conf_clause = ""
            conf_params = ()
        rows = list(
            conn.execute(
                f"""
                SELECT * FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY target ORDER BY source, kind
                           ) AS _rn
                    FROM edges
                    WHERE target IN ({target_ph})
                      AND kind IN ({kind_ph})
                      {conf_clause}
                )
                WHERE _rn <= ?
                ORDER BY source, target, kind
                LIMIT ?
                """,
                (*target_ids, *kinds, *conf_params, sentinel, fetch_limit),
            )
        )
        capped = len(rows) >= fetch_limit
        # Detect per-target truncation: any sentinel row means that target
        # had more callers than per_target_limit.  Iterate all fetched rows
        # so that sentinel rows don't consume global capacity.
        kept: list[dict[str, Any]] = []
        for idx, row in enumerate(rows):
            if row["_rn"] > per_target_limit:
                capped = True
                continue
            kept.append(self._edge_row(row))
            if len(kept) >= global_limit:
                # Only mark capped if there are unconsumed rows remaining
                # (either more valid rows or sentinel rows beyond this point).
                if idx + 1 < len(rows):
                    capped = True
                break
        return kept, capped

    def _has_unseen_edges(
        self,
        conn: Any,
        target_ids: list[str],
        kinds: list[str],
        seen_edges: dict[tuple[str, str, str], Any],
        *,
        allowed_confidences: frozenset[str] | None = None,
    ) -> bool:
        """Lightweight check: are there edges for *target_ids* not in *seen_edges*?

        Executes ``SELECT 1 ... LIMIT 1`` and filters out already-seen keys
        in Python.  This is cheap because we only need to find one unseen
        edge to return True.
        """
        if not target_ids or not kinds:
            return False
        if len(target_ids) > SQLITE_IN_BATCH_SIZE:
            return any(
                self._has_unseen_edges(
                    conn,
                    target_ids[start : start + SQLITE_IN_BATCH_SIZE],
                    kinds,
                    seen_edges,
                    allowed_confidences=allowed_confidences,
                )
                for start in range(0, len(target_ids), SQLITE_IN_BATCH_SIZE)
            )
        target_ph = ",".join("?" for _ in target_ids)
        kind_ph = ",".join("?" for _ in kinds)
        if allowed_confidences:
            conf_list = sorted(allowed_confidences)
            conf_ph = ",".join("?" for _ in conf_list)
            conf_clause = f"AND confidence IN ({conf_ph})"
            conf_params: tuple[str, ...] = tuple(conf_list)
        else:
            conf_clause = ""
            conf_params = ()
        for row in conn.execute(
            f"""
            SELECT source, target, kind FROM edges
            WHERE target IN ({target_ph})
              AND kind IN ({kind_ph})
              {conf_clause}
            LIMIT ?
            """,
            (*target_ids, *kinds, *conf_params, len(seen_edges) + 1),
        ):
            key = (row["source"], row["target"], row["kind"])
            if key not in seen_edges:
                return True
        return False

    def _edges_for_sources_kinds(
        self, conn: Any, source_ids: list[str], kinds: list[str]
    ) -> list[dict[str, Any]]:
        return self._edges_for_endpoint_kinds(conn, "source", source_ids, kinds)

    def _node_by_id(self, conn: Any, node_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return self._node_row(row) if row else None

    @staticmethod
    def node_from_row(row: Any) -> dict[str, Any]:
        return QueryEngine._node_row(row)

    @staticmethod
    def diagnostic_from_row(row: Any) -> dict[str, Any]:
        return QueryEngine._diagnostic_row(row)

    def nodes_by_ids(self, node_ids: list[str]) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            return self._nodes_by_ids(conn, node_ids)

    def _nodes_by_ids(self, conn: Any, node_ids: list[str]) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        ids = sorted({node_id for node_id in node_ids if node_id})
        rows: list[Any] = []
        for start in range(0, len(ids), SQLITE_IN_BATCH_SIZE):
            chunk = ids[start : start + SQLITE_IN_BATCH_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                conn.execute(
                    f"SELECT * FROM nodes WHERE id IN ({placeholders}) ORDER BY id",
                    chunk,
                )
            )
        return [self._node_row(row) for row in rows]

    @staticmethod
    def _is_routable_entrypoint(row: dict[str, Any]) -> bool:
        # A router-local path is not a URL, even when a caller supplies its
        # internal route id explicitly.
        return row.get("properties", {}).get("route_mounted") is not False

    @staticmethod
    def _entrypoint_aliases(entrypoint: str) -> list[str]:
        """Compatibility delegate to the unified entrypoint-set resolver."""

        return entrypoint_query_aliases(entrypoint.strip())

    def _forward_flow_edges(
        self,
        conn: Any,
        entrypoint_ids: list[str],
        *,
        max_depth: int,
        max_results: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        # Keep one extra edge to distinguish an exactly-sized complete result
        # from a result cut short by the edge budget. Unknown remaining counts
        # must not be reported as zero.
        seen: dict[tuple[str, str, str], dict[str, Any]] = {}
        visited = set(entrypoint_ids)
        frontier = set(entrypoint_ids)

        def finish(reason: str | None = None):
            edges = list(seen.values())[:max_results]
            return sorted(edges, key=lambda e: (e["source"], e["target"], e["kind"])), {
                "traversal_truncated": reason is not None,
                "traversal": {"truncated": reason is not None, "reason": reason},
                "reasons": [reason] if reason else [],
                "max_depth": max_depth,
                "max_results": max_results,
                "remaining_edge_count": None if reason else 0,
                "completeness_scope": "indexed flow edge kinds only",
            }

        for edge in self._edges_for_targets_kinds(
            conn, sorted(frontier), ["enqueues", "consumes"]
        ):
            seen.setdefault((edge["source"], edge["target"], edge["kind"]), edge)
            if len(seen) > max_results:
                return finish("max_results")
            visited.add(edge["source"])
            frontier.add(edge["source"])

        for _ in range(max_depth):
            if not frontier:
                break
            edges = self._edges_for_sources_kinds(
                conn, sorted(frontier), ENTRYPOINT_FLOW_EDGE_KINDS
            )
            frontier = set()
            for edge in edges:
                seen.setdefault((edge["source"], edge["target"], edge["kind"]), edge)
                if len(seen) > max_results:
                    return finish("max_results")
                if edge["target"] not in visited:
                    visited.add(edge["target"])
                    frontier.add(edge["target"])

        # An additional indexed edge, not merely a nonempty frontier, proves
        # that the depth boundary omitted work (leaf nodes are not truncation).
        if frontier and any(
            (edge["source"], edge["target"], edge["kind"]) not in seen
            for edge in self._edges_for_sources_kinds(
                conn, sorted(frontier), ENTRYPOINT_FLOW_EDGE_KINDS
            )
        ):
            return finish("max_depth")
        return finish()

    @staticmethod
    def _is_local_import_edge(edge: dict[str, Any]) -> bool:
        evidence = edge.get("evidence", [])
        return bool(evidence) and all(
            item.get("kind") == "ast_local_import"
            for item in evidence
            if isinstance(item, dict)
        )

    @staticmethod
    def _import_cycles(import_edges: list[dict[str, Any]]) -> list[list[str]]:
        graph: dict[str, list[str]] = defaultdict(list)
        for edge in import_edges:
            graph[edge["source"]].append(edge["target"])

        cycles: dict[tuple[str, ...], list[str]] = {}
        stack: list[str] = []
        visiting: set[str] = set()
        fully_explored: set[str] = set()

        def canonical(cycle: list[str]) -> tuple[str, ...]:
            body = cycle[:-1]
            rotations = [
                tuple(body[index:] + body[:index]) for index in range(len(body))
            ]
            return min(rotations)

        def visit(node: str) -> None:
            if node in fully_explored or len(cycles) >= 20:
                return
            if node in visiting:
                start = stack.index(node)
                cycle = [*stack[start:], node]
                cycles.setdefault(canonical(cycle), cycle)
                return
            visiting.add(node)
            stack.append(node)
            for target in graph.get(node, []):
                if target.startswith("mod:"):
                    visit(target)
            stack.pop()
            visiting.remove(node)
            fully_explored.add(node)

        for node in sorted(graph):
            visit(node)
            if len(cycles) >= 20:
                break
        return sorted(cycles.values())

    @staticmethod
    def _node_row(row: Any) -> dict[str, Any]:
        properties = json.loads(row["properties_json"])
        properties.pop("similarity", None)
        return {
            "id": row["id"],
            "kind": row["kind"],
            "name": row["name"],
            "qualname": row["qualname"],
            "path": row["path"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "canonical_identity": row["canonical_identity"],
            "properties": properties,
        }

    @staticmethod
    def _edge_row(row: Any) -> dict[str, Any]:
        return {
            "source": row["source"],
            "target": row["target"],
            "kind": row["kind"],
            "confidence": row["confidence"],
            "semantic_role": row["semantic_role"],
            "resolution": json.loads(row["resolution_json"]),
            "confidence_sources": json.loads(row["confidence_sources_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "properties": json.loads(row["properties_json"]),
        }

    @staticmethod
    def _diagnostic_row(row: Any) -> dict[str, Any]:
        return {
            "diagnostic_id": row["diagnostic_id"],
            "repo_id": row["repo_id"],
            "index_version": row["index_version"],
            "diagnostic_kind": row["diagnostic_kind"],
            "message": row["message"],
            "severity": row["severity"],
            "path": row["path"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "frontend_name": row["frontend_name"],
            "fact_id": row["fact_id"],
            "first_seen_index": row["first_seen_index"],
            "last_seen_index": row["last_seen_index"],
            "seen_count": row["seen_count"],
            "properties": json.loads(row["properties_json"]),
        }
