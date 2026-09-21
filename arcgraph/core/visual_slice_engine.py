"""VisualSlice generation for interactive ArcGraph consumers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.visual_contract import (
    ENTRYPOINT_KINDS_SET,
    LANE_ORDER,
    RESOURCE_EDGE_KINDS,
    RESOURCE_NODE_KINDS,
    RESOURCE_NODE_KINDS_SET,
    SUPPORTED_VIEW_ORDER,
    entrypoint_stat_kinds,
)

VISUAL_SLICE_SCHEMA = "VisualSlice"
VISUAL_SLICE_VERSION = 1
__all__ = [
    "DEFAULT_VISUAL_SLICE_LIMITS",
    "VISUAL_SLICE_SCHEMA",
    "VISUAL_SLICE_VERSION",
    "VisualSliceEngine",
    "VisualSliceLimits",
    "make_visual_slice",
]

SUPPORTED_VIEWS = set(SUPPORTED_VIEW_ORDER)


@dataclass(frozen=True)
class VisualSliceLimits:
    max_nodes: int = 500
    max_edges: int = 500
    max_diagnostics: int = 50


DEFAULT_VISUAL_SLICE_LIMITS = VisualSliceLimits()


class VisualSliceEngine:
    """Build front-end friendly graph slices from QueryEngine payloads."""

    def __init__(self, query_engine: QueryEngine) -> None:
        self.query_engine = query_engine
        self._repo_id = str(query_engine.store.metadata.get("repo_id") or "default")
        self._index_version = str(
            query_engine.store.metadata.get("index_version") or "unknown"
        )

    def build_slice(
        self,
        view: str,
        *,
        target: str | None = None,
        profile: str = "review_default",
        include_unresolved: bool = True,
        include_runtime: bool = True,
        include_edge_kinds: list[str] | None = None,
        exclude_edge_kinds: list[str] | None = None,
        limits: VisualSliceLimits = DEFAULT_VISUAL_SLICE_LIMITS,
    ) -> dict[str, Any]:
        """Return a VisualSlice for a supported view."""

        if view not in SUPPORTED_VIEWS:
            return make_visual_slice(
                [],
                [],
                view=view,
                scope=self._scope(target=target),
                status="unavailable",
                diagnostics=[
                    {
                        "severity": "error",
                        "message": f"Unsupported VisualSlice view: {view}",
                    }
                ],
                limits=limits,
            )

        if view == "system_map":
            return self._system_map(limits=limits)

        if view == "entrypoint_circuit":
            if not target:
                return _missing_target_slice(
                    view, limits, scope=self._scope(target=target)
                )
            return self._entrypoint_circuit(target, limits=limits)

        if view == "impact_radius":
            if not target:
                return _missing_target_slice(
                    view, limits, scope=self._scope(target=target)
                )
            return self._impact_radius(
                target,
                profile=profile,
                include_unresolved=include_unresolved,
                include_runtime=include_runtime,
                include_edge_kinds=include_edge_kinds,
                exclude_edge_kinds=exclude_edge_kinds,
                limits=limits,
            )

        if view == "resource_flow":
            if not target:
                return _missing_target_slice(
                    view, limits, scope=self._scope(target=target)
                )
            return self._resource_flow(
                target,
                profile=profile,
                include_unresolved=include_unresolved,
                include_runtime=include_runtime,
                include_edge_kinds=include_edge_kinds,
                exclude_edge_kinds=exclude_edge_kinds,
                limits=limits,
            )

        if view == "unresolved_risk_map":
            return self._unresolved_risk_map(target=target, limits=limits)

        if view == "module_map":
            if not target:
                return _missing_target_slice(
                    view, limits, scope=self._scope(target=target)
                )
            return self._module_map(target, limits=limits)

        if view == "symbol_map":
            if not target:
                return _missing_target_slice(
                    view, limits, scope=self._scope(target=target)
                )
            return self._symbol_map(target, limits=limits)

        if view == "similarity_map":
            if not target:
                return _missing_target_slice(
                    view, limits, scope=self._scope(target=target)
                )
            return self._similarity_map(target, limits=limits)

        return _missing_target_slice(view, limits, scope=self._scope(target=target))

    def _entrypoint_circuit(
        self, target: str, *, limits: VisualSliceLimits
    ) -> dict[str, Any]:
        flow = self.query_engine.entrypoint_flow(target)
        nodes = [*flow.get("entrypoints", []), *flow.get("nodes", [])]
        edges = flow.get("edges", [])
        return make_visual_slice(
            nodes,
            edges,
            view="entrypoint_circuit",
            scope=self._scope(target=target),
            status=flow.get("status", "available"),
            diagnostics=_warnings_as_diagnostics(flow.get("warnings", [])),
            summary={
                "entrypoint_count": len(flow.get("entrypoints", [])),
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
            capabilities=flow.get("capabilities", {}),
            limits=limits,
        )

    def _impact_radius(
        self,
        target: str,
        *,
        profile: str,
        include_unresolved: bool,
        include_runtime: bool,
        include_edge_kinds: list[str] | None,
        exclude_edge_kinds: list[str] | None,
        limits: VisualSliceLimits,
    ) -> dict[str, Any]:
        impact = self.query_engine.impact(
            target,
            profile=profile,
            include_edge_kinds=include_edge_kinds,
            exclude_edge_kinds=exclude_edge_kinds,
        )
        edges = [
            *impact.get("call_impact", {}).get("edges", []),
            *impact.get("import_impact", {}).get("edges", []),
            *impact.get("entrypoint_impact", {}).get("edges", []),
            *impact.get("resource_impact", {}).get("edges", []),
        ]
        if not include_runtime:
            edges = [edge for edge in edges if edge.get("confidence") != "runtime-only"]

        nodes = [
            *self._nodes_by_ids(impact.get("resolved_targets", [])),
            *impact.get("call_impact", {}).get("affected_symbols", []),
            *impact.get("import_impact", {}).get("affected_modules", []),
            *impact.get("entrypoint_impact", {}).get("entrypoints", []),
            *impact.get("resource_impact", {}).get("resources", []),
        ]
        nodes = [*nodes, *self._endpoint_nodes(edges, nodes)]

        diagnostics = _warnings_as_diagnostics(impact.get("warnings", []))
        if include_unresolved:
            diagnostics.extend(impact.get("unresolved_risks", {}).get("items", []))

        return make_visual_slice(
            nodes,
            edges,
            view="impact_radius",
            scope=self._scope(target=target, profile=profile),
            status=impact.get("status", "available"),
            diagnostics=diagnostics,
            summary={
                "resolved_targets": impact.get("resolved_targets", []),
                "confidence_summary": impact.get("confidence_summary", {}),
                "test_gap_count": len(impact.get("test_gaps", [])),
            },
            capabilities=impact.get("capabilities", {}),
            limits=limits,
        )

    def _resource_flow(
        self,
        target: str,
        *,
        profile: str,
        include_unresolved: bool,
        include_runtime: bool,
        include_edge_kinds: list[str] | None,
        exclude_edge_kinds: list[str] | None,
        limits: VisualSliceLimits,
    ) -> dict[str, Any]:
        has_filter = include_edge_kinds is not None or exclude_edge_kinds is not None
        effective_kinds = list(RESOURCE_EDGE_KINDS)
        if include_edge_kinds is not None:
            allowed = set(include_edge_kinds)
            effective_kinds = [k for k in effective_kinds if k in allowed]
        if exclude_edge_kinds is not None:
            excluded = set(exclude_edge_kinds)
            effective_kinds = [k for k in effective_kinds if k not in excluded]

        if has_filter and not effective_kinds:
            return make_visual_slice(
                self._nodes_by_ids([target]),
                [],
                view="resource_flow",
                scope=self._scope(target=target, profile=profile),
                status="available",
                diagnostics=[],
                summary={"resource_count": 0, "edge_kinds": []},
                capabilities={},
                limits=limits,
            )

        impact = self.query_engine.impact(
            target,
            profile=profile,
            include_edge_kinds=effective_kinds,
        )
        edges = impact.get("resource_impact", {}).get("edges", [])
        if not include_runtime:
            edges = [edge for edge in edges if edge.get("confidence") != "runtime-only"]
        nodes = [
            *self._nodes_by_ids(impact.get("resolved_targets", [])),
            *impact.get("resource_impact", {}).get("resources", []),
        ]
        nodes = [*nodes, *self._endpoint_nodes(edges, nodes)]
        diagnostics = _warnings_as_diagnostics(impact.get("warnings", []))
        if include_unresolved:
            diagnostics.extend(impact.get("unresolved_risks", {}).get("items", []))

        return make_visual_slice(
            nodes,
            edges,
            view="resource_flow",
            scope=self._scope(target=target, profile=profile),
            status=impact.get("status", "available"),
            diagnostics=diagnostics,
            summary={
                "resource_count": len(
                    impact.get("resource_impact", {}).get("resources", [])
                ),
                "edge_kinds": effective_kinds,
            },
            capabilities=impact.get("capabilities", {}),
            limits=limits,
        )

    def _unresolved_risk_map(
        self, *, target: str | None, limits: VisualSliceLimits
    ) -> dict[str, Any]:
        unresolved = self.query_engine.unresolved(
            target,
            limit=max(limits.max_diagnostics, limits.max_nodes),
        )
        diagnostics = unresolved.get("unresolved", [])
        source_ids = [
            item.get("properties", {}).get("source_scope")
            for item in diagnostics
            if item.get("properties", {}).get("source_scope")
        ]
        source_nodes = self._nodes_by_ids(source_ids)
        source_lookup = {node["id"]: node for node in source_nodes}
        risk_nodes: list[dict[str, Any]] = []
        risk_edges: list[dict[str, Any]] = []
        for index, item in enumerate(diagnostics):
            risk_id = item.get("diagnostic_id") or f"diagnostic:{index}"
            source_id = item.get("properties", {}).get("source_scope")
            risk_nodes.append(
                {
                    "id": risk_id,
                    "kind": "diagnostic",
                    "name": item.get("properties", {}).get("raw_expression")
                    or item.get("diagnostic_kind", "diagnostic"),
                    "qualname": item.get("message"),
                    "path": item.get("path"),
                    "start_line": item.get("start_line"),
                    "end_line": item.get("end_line"),
                    "properties": item.get("properties", {}),
                }
            )
            if source_id and source_id in source_lookup:
                risk_edges.append(
                    {
                        "source": source_id,
                        "target": risk_id,
                        "kind": "dynamic_call",
                        "confidence": "unresolved",
                        "semantic_role": "unresolved_callsite",
                        "evidence": {"message": item.get("message")},
                    }
                )

        return make_visual_slice(
            [*source_nodes, *risk_nodes],
            risk_edges,
            view="unresolved_risk_map",
            scope=self._scope(target=target),
            status=unresolved.get("status", "available"),
            diagnostics=diagnostics,
            summary=unresolved.get("summary", {}),
            capabilities=unresolved.get("capabilities", {}),
            limits=limits,
        )

    def _system_map(self, *, limits: VisualSliceLimits) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        source_root_counts: dict[str, int] = {}
        root_kind_counts: list[tuple[str, str, int]] = []
        node_kinds: dict[str, int] = {}
        edge_kinds: dict[str, int] = {}

        with self.query_engine.store.connect() as conn:
            rows = conn.execute("""
                SELECT COALESCE(f.source_root, 'external') AS source_root,
                       n.kind AS kind,
                       COUNT(*) AS count
                FROM nodes n
                LEFT JOIN files f ON n.path = f.path
                GROUP BY COALESCE(f.source_root, 'external'), n.kind
                ORDER BY source_root, count DESC, kind
                """).fetchall()
            for row in rows:
                source_root = row["source_root"]
                kind = row["kind"]
                count = int(row["count"])
                source_root_counts[source_root] = (
                    source_root_counts.get(source_root, 0) + count
                )
                node_kinds[kind] = node_kinds.get(kind, 0) + count
                root_kind_counts.append((source_root, kind, count))
            edge_kinds = {
                row["kind"]: int(row["count"])
                for row in conn.execute(
                    "SELECT kind, COUNT(*) AS count FROM edges GROUP BY kind ORDER BY kind"
                )
            }

        for source_root, count in sorted(source_root_counts.items()):
            nodes.append(
                {
                    "id": f"source_root:{source_root}",
                    "kind": "source_root",
                    "name": source_root,
                    "qualname": source_root,
                    "properties": {"node_count": count},
                }
            )

        seen_kinds: set[str] = set()
        for kind, count in sorted(
            node_kinds.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            if len(seen_kinds) >= 24:
                break
            seen_kinds.add(kind)
            nodes.append(
                {
                    "id": f"node_kind:{kind}",
                    "kind": "node_kind",
                    "name": kind,
                    "qualname": kind,
                    "properties": {"node_count": count},
                }
            )

        for source_root, kind, count in root_kind_counts:
            if kind not in seen_kinds:
                continue
            edges.append(
                {
                    "source": f"source_root:{source_root}",
                    "target": f"node_kind:{kind}",
                    "kind": "contains",
                    "confidence": "confirmed",
                    "semantic_role": "system_map",
                    "properties": {"count": count},
                }
            )

        return make_visual_slice(
            nodes,
            edges,
            view="system_map",
            scope=self._scope(mode="source_root_kind_aggregate"),
            status="available",
            diagnostics=_warnings_as_diagnostics(
                self.query_engine.store.metadata.get("warnings", [])
            ),
            summary={
                "node_kinds": node_kinds,
                "edge_kinds": edge_kinds,
                "entrypoints": _entrypoint_counts(node_kinds),
                "resources": _resource_counts(node_kinds),
                "import_cycles": [],
                "degraded": "package/domain map is not available; grouped by source_root and node kind.",
            },
            capabilities={
                **self.query_engine.store.metadata.get("capabilities", {}),
                "architecture": "basic",
            },
            limits=limits,
        )

    def _module_map(self, target: str, *, limits: VisualSliceLimits) -> dict[str, Any]:
        """Module-level import graph centred on a target module."""
        imports = self.query_engine.imports(target)
        module_id = imports.get("module", target)
        outgoing = imports.get("outgoing", [])
        incoming = imports.get("incoming", [])
        all_edges = [*outgoing, *incoming]

        # Collect all referenced module IDs
        module_ids = sorted(
            {
                module_id,
                *{edge["source"] for edge in all_edges},
                *{edge["target"] for edge in all_edges},
            }
        )
        nodes = self._nodes_by_ids(module_ids)
        nodes = [*nodes, *self._endpoint_nodes(all_edges, nodes)]

        return make_visual_slice(
            nodes,
            all_edges,
            view="module_map",
            scope=self._scope(target=target),
            status=imports.get("status", "available"),
            diagnostics=_warnings_as_diagnostics(imports.get("warnings", [])),
            summary={
                "target_module": module_id,
                "outgoing_count": len(outgoing),
                "incoming_count": len(incoming),
            },
            capabilities=imports.get("capabilities", {}),
            limits=limits,
        )

    def _symbol_map(self, target: str, *, limits: VisualSliceLimits) -> dict[str, Any]:
        """Symbol-level relationship graph for a target symbol or file.

        Shows the target symbol, its callers, callees, and structural
        edges (defines, extends) to give a complete local view.
        """
        callers = self.query_engine.callers(target)
        callees = self.query_engine.callees(target)
        target_ids = callers.get("resolved_targets", [])
        caller_edges = callers.get("edges", [])
        callee_edges = callees.get("edges", [])
        all_edges = [*caller_edges, *callee_edges]

        nodes = [
            *self._nodes_by_ids(target_ids),
            *callers.get("callers", []),
            *callees.get("callees", []),
        ]
        nodes = [*nodes, *self._endpoint_nodes(all_edges, nodes)]

        status = callers.get("status", "available")
        warnings = [
            *callers.get("warnings", []),
            *callees.get("warnings", []),
        ]

        return make_visual_slice(
            nodes,
            all_edges,
            view="symbol_map",
            scope=self._scope(target=target),
            status=status,
            diagnostics=_warnings_as_diagnostics(warnings),
            summary={
                "resolved_targets": target_ids,
                "caller_count": len(callers.get("callers", [])),
                "callee_count": len(callees.get("callees", [])),
                "edge_count": len(all_edges),
            },
            capabilities=callers.get("capabilities", {}),
            limits=limits,
        )

    def _similarity_map(
        self, target: str, *, limits: VisualSliceLimits
    ) -> dict[str, Any]:
        """Similar implementation cluster for a target symbol."""
        similar = self.query_engine.similar(target, detail_level="full")
        target_ids = similar.get("resolved_targets", [])
        similar_items = similar.get("similar", [])

        nodes = self._nodes_by_ids(target_ids)
        edges: list[dict[str, Any]] = []
        for item in similar_items:
            node = item.get("node")
            if node:
                nodes.append(node)
            edge = item.get("edge")
            if edge:
                edges.append(edge)

        return make_visual_slice(
            nodes,
            edges,
            view="similarity_map",
            scope=self._scope(target=target),
            status=similar.get("status", "available"),
            diagnostics=_warnings_as_diagnostics(similar.get("warnings", [])),
            summary={
                "resolved_targets": target_ids,
                "similar_count": len(similar_items),
                "max_score": max(
                    (item.get("score", 0) for item in similar_items),
                    default=0,
                ),
            },
            capabilities=similar.get("capabilities", {}),
            limits=limits,
        )

    def _nodes_by_ids(self, node_ids: list[str]) -> list[dict[str, Any]]:
        ids = sorted({node_id for node_id in node_ids if node_id})
        if not ids:
            return []
        return self.query_engine.nodes_by_ids(ids)

    def _endpoint_nodes(
        self, edges: list[dict[str, Any]], known_nodes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        known_ids = {node.get("id") for node in known_nodes}
        endpoint_ids = sorted(
            {
                endpoint
                for edge in edges
                for endpoint in (edge.get("source"), edge.get("target"))
                if endpoint and endpoint not in known_ids
            }
        )
        return self._nodes_by_ids(endpoint_ids)

    def _scope(self, **values: Any) -> dict[str, Any]:
        return {
            "repo_id": self._repo_id,
            "index_version": self._index_version,
            **values,
        }


def make_visual_slice(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    view: str = "custom",
    scope: dict[str, Any] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
    capabilities: dict[str, str] | None = None,
    status: str = "available",
    limits: VisualSliceLimits = DEFAULT_VISUAL_SLICE_LIMITS,
) -> dict[str, Any]:
    """Normalize raw query payloads into the stable VisualSlice envelope."""

    deduped_nodes = _dedupe_nodes(nodes)
    deduped_edges = _dedupe_edges(edges)
    diagnostic_items = list(diagnostics or [])
    node_limit = max(1, limits.max_nodes)
    edge_limit = max(1, limits.max_edges)
    diagnostic_limit = max(0, limits.max_diagnostics)

    visual_nodes = [
        _visual_node(node, index=index, view=view)
        for index, node in enumerate(deduped_nodes)
    ]
    visual_edges = [_visual_edge(edge) for edge in deduped_edges]
    visible_nodes = visual_nodes[:node_limit]
    visible_edges = visual_edges[:edge_limit]
    visible_diagnostics = diagnostic_items[:diagnostic_limit]
    nodes_dropped = max(0, len(visual_nodes) - len(visible_nodes))
    edges_dropped = max(0, len(visual_edges) - len(visible_edges))
    diagnostics_dropped = max(0, len(diagnostic_items) - len(visible_diagnostics))
    lanes = _lanes(visible_nodes)
    groups = _groups(visible_nodes)
    payload_scope = _normalized_scope(scope)
    return {
        "schema": VISUAL_SLICE_SCHEMA,
        "version": VISUAL_SLICE_VERSION,
        "slice_id": _slice_id(view, payload_scope, visible_nodes, visible_edges),
        "view": view,
        "scope": payload_scope,
        "status": status,
        "nodes": visible_nodes,
        "edges": visible_edges,
        "lanes": lanes,
        "groups": groups,
        "overlays": _overlays(capabilities or {}, visible_diagnostics),
        "actions": _actions(status, payload_scope),
        "diagnostics": visible_diagnostics,
        "summary": summary or {},
        "capabilities": capabilities or {},
        "truncation": {
            "truncated": bool(nodes_dropped or edges_dropped or diagnostics_dropped),
            "nodes_dropped": nodes_dropped,
            "edges_dropped": edges_dropped,
            "diagnostics_dropped": diagnostics_dropped,
            "limits": {
                "max_nodes": node_limit,
                "max_edges": edge_limit,
                "max_diagnostics": diagnostic_limit,
            },
        },
    }


def _visual_node(node: dict[str, Any], *, index: int, view: str) -> dict[str, Any]:
    node_id = str(node.get("id") or f"node:{index}")
    kind = str(node.get("kind") or "unknown")
    properties = dict(node.get("properties") or {})
    lane_id = str(node.get("lane_id") or _lane_for_kind(kind, view))
    group_id = node.get("group_id") or _group_for_node(node, lane_id)
    importance = _importance(node, properties)
    return {
        "id": node_id,
        "label": node.get("label")
        or node.get("name")
        or node.get("qualname")
        or node_id,
        "kind": kind,
        "name": node.get("name"),
        "qualname": node.get("qualname"),
        "path": node.get("path"),
        "start_line": node.get("start_line"),
        "end_line": node.get("end_line"),
        "confidence": node.get("confidence")
        or properties.get("confidence")
        or "confirmed",
        "lane_id": lane_id,
        "group_id": group_id,
        "rank": int(node.get("rank") or _rank_for_node(node, index)),
        "importance": importance,
        "source_anchor": _source_anchor(node),
        "collapsed_count": int(
            node.get("collapsed_count") or properties.get("node_count") or 0
        ),
        "properties": properties,
    }


def _visual_edge(edge: dict[str, Any]) -> dict[str, Any]:
    evidence = edge.get("evidence") or {}
    resolution = edge.get("resolution") or {}
    return {
        "source": edge.get("source"),
        "target": edge.get("target"),
        "kind": edge.get("kind") or "related",
        "confidence": edge.get("confidence") or "confirmed",
        "semantic_role": edge.get("semantic_role"),
        "evidence_summary": _evidence_summary(evidence, resolution),
        "properties": edge.get("properties") or {},
    }


def _dedupe_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for node in nodes:
        node_id = node.get("id")
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        deduped.append(node)
    return deduped


def _dedupe_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str | None]] = set()
    deduped: list[dict[str, Any]] = []
    for edge in edges:
        source = edge.get("source")
        target = edge.get("target")
        kind = edge.get("kind")
        if not source or not target or not kind:
            continue
        key = (
            str(source),
            str(target),
            str(kind),
            edge.get("semantic_role"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped


def _lanes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for node in nodes:
        lane_id = node["lane_id"]
        counts[lane_id] = counts.get(lane_id, 0) + 1
    # Use the frozen LANE_ORDER for deterministic semantic ordering;
    # any lane_id not listed in LANE_ORDER sorts to the end alphabetically.
    lane_rank = {lane: idx for idx, lane in enumerate(LANE_ORDER)}
    fallback = len(LANE_ORDER)
    ordered = sorted(
        counts.items(),
        key=lambda pair: (lane_rank.get(pair[0], fallback), pair[0]),
    )
    return [
        {"id": lane_id, "label": _title(lane_id), "rank": index, "node_count": count}
        for index, (lane_id, count) in enumerate(ordered)
    ]


def _groups(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for node in nodes:
        group_id = node.get("group_id")
        if not group_id:
            continue
        counts[str(group_id)] = counts.get(str(group_id), 0) + 1
    return [
        {"id": group_id, "label": _title(group_id), "node_count": count}
        for group_id, count in sorted(counts.items())
    ]


def _overlays(
    capabilities: dict[str, str], diagnostics: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    for key in ("precision", "coverage", "runtime_trace"):
        value = capabilities.get(key)
        if value and value not in {"available", "precision_available"}:
            overlays.append({"kind": "capability", "key": key, "status": value})
    if diagnostics:
        overlays.append({"kind": "diagnostics", "count": len(diagnostics)})
    return overlays


def _actions(status: str, scope: dict[str, Any]) -> list[dict[str, Any]]:
    actions = [{"id": "fit_view", "kind": "viewport"}]
    target = scope.get("target")
    if target and status != "unavailable":
        actions.append({"id": "copy_target", "kind": "clipboard"})
    return actions


def _normalized_scope(scope: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "repo_id": "unknown",
        "index_version": "unknown",
        **(scope or {}),
    }


def _slice_id(
    view: str,
    scope: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> str:
    seed = "|".join(
        [
            view,
            repr(sorted(scope.items())),
            ",".join(node["id"] for node in nodes[:20]),
            ",".join(
                f"{edge.get('source')}->{edge.get('target')}:{edge.get('kind')}"
                for edge in edges[:20]
            ),
        ]
    )
    digest = hashlib.sha1(
        seed.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:12]
    return f"slice:{view}:{digest}"


def _lane_for_kind(kind: str, view: str) -> str:
    if view == "system_map":
        return "source_roots" if kind == "source_root" else "node_kinds"
    if kind in ENTRYPOINT_KINDS_SET and kind not in RESOURCE_NODE_KINDS_SET:
        return "entrypoints"
    if kind in RESOURCE_NODE_KINDS_SET:
        return "resources"
    if kind in {"module", "external_package"}:
        return "modules"
    if kind == "diagnostic":
        return "diagnostics"
    return "symbols"


def _group_for_node(node: dict[str, Any], lane_id: str) -> str | None:
    path = node.get("path")
    if path:
        return str(path).split("/", 1)[0]
    if lane_id in {"source_roots", "node_kinds", "diagnostics"}:
        return lane_id
    return None


def _rank_for_node(node: dict[str, Any], index: int) -> int:
    kind = node.get("kind")
    if kind in ENTRYPOINT_KINDS_SET and kind not in RESOURCE_NODE_KINDS_SET:
        return 0
    if kind == "source_root":
        return 0
    if kind in {"function", "method", "class"}:
        return 1
    if kind in RESOURCE_NODE_KINDS_SET or kind == "diagnostic":
        return 2
    return index


def _importance(node: dict[str, Any], properties: dict[str, Any]) -> float:
    for key in ("importance", "node_count", "degree"):
        value = node.get(key, properties.get(key))
        if isinstance(value, int | float):
            return float(value)
    return 1.0


def _source_anchor(node: dict[str, Any]) -> dict[str, Any] | None:
    path = node.get("path")
    if not path:
        return None
    return {
        "path": path,
        "start_line": node.get("start_line"),
        "end_line": node.get("end_line"),
    }


def _evidence_summary(evidence: Any, resolution: dict[str, Any]) -> str | None:
    if isinstance(evidence, list):
        evidence = next((item for item in evidence if isinstance(item, dict)), {})
    if not isinstance(evidence, dict):
        evidence = {}

    if evidence.get("message"):
        return str(evidence["message"])
    if evidence.get("line") or evidence.get("start_line") or evidence.get("path"):
        path = evidence.get("path", "")
        line = evidence.get("line") or evidence.get("start_line")
        return f"{path}:{line}" if line else str(path)
    if evidence.get("detail"):
        return str(evidence["detail"])
    if resolution.get("status"):
        return str(resolution["status"])
    return None


def _warnings_as_diagnostics(warnings: list[str]) -> list[dict[str, Any]]:
    return [{"severity": "warning", "message": warning} for warning in warnings]


def _entrypoint_counts(node_kinds: dict[str, int]) -> dict[str, int]:
    return {kind: int(node_kinds.get(kind, 0) or 0) for kind in entrypoint_stat_kinds()}


def _resource_counts(node_kinds: dict[str, int]) -> dict[str, int]:
    return {kind: int(node_kinds.get(kind, 0) or 0) for kind in RESOURCE_NODE_KINDS}


def _missing_target_slice(
    view: str, limits: VisualSliceLimits, *, scope: dict[str, Any] | None = None
) -> dict[str, Any]:
    return make_visual_slice(
        [],
        [],
        view=view,
        scope=scope,
        status="unavailable",
        diagnostics=[
            {
                "severity": "error",
                "message": f"VisualSlice view {view} requires a target.",
            }
        ],
        limits=limits,
    )


def _title(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()
