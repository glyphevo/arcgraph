"""VisualSlice compatibility helpers for report-facing graph payloads."""

from __future__ import annotations

from typing import Any

# VisualSlice is now produced by the core query layer because the interactive API
# and HTML reports share the same normalized envelope. Keep this module as the
# report-facing compatibility import path for older callers.
from arcgraph.core.visual_slice_engine import (
    DEFAULT_VISUAL_SLICE_LIMITS,
    VISUAL_SLICE_SCHEMA,
    VISUAL_SLICE_VERSION,
    VisualSliceLimits,
    make_visual_slice,
)

__all__ = [
    "DEFAULT_VISUAL_SLICE_LIMITS",
    "VISUAL_SLICE_SCHEMA",
    "VISUAL_SLICE_VERSION",
    "VisualSliceLimits",
    "build_report_visual_slices",
    "make_visual_slice",
]


def build_report_visual_slices(
    *,
    impact: dict[str, Any],
    similar: dict[str, Any],
    architecture: dict[str, Any],
    entrypoint_flow: dict[str, Any] | None,
    limits: VisualSliceLimits = DEFAULT_VISUAL_SLICE_LIMITS,
) -> dict[str, Any]:
    """Build all report graph views through the stable VisualSlice envelope."""

    entrypoint_nodes = []
    entrypoint_edges = []
    if entrypoint_flow and entrypoint_flow.get("status") == "available":
        entrypoint_nodes = [
            *entrypoint_flow.get("entrypoints", []),
            *entrypoint_flow.get("nodes", []),
        ]
        entrypoint_edges = entrypoint_flow.get("edges", [])
    else:
        entrypoint_nodes = impact.get("entrypoint_impact", {}).get("entrypoints", [])
        entrypoint_edges = impact.get("entrypoint_impact", {}).get("edges", [])

    impact_nodes = [
        *impact.get("call_impact", {}).get("affected_symbols", []),
        *impact.get("import_impact", {}).get("affected_modules", []),
        *impact.get("entrypoint_impact", {}).get("entrypoints", []),
        *impact.get("resource_impact", {}).get("resources", []),
    ]
    impact_edges = [
        *impact.get("call_impact", {}).get("edges", []),
        *impact.get("import_impact", {}).get("edges", []),
        *impact.get("entrypoint_impact", {}).get("edges", []),
        *impact.get("resource_impact", {}).get("edges", []),
    ]
    similarity_nodes = [
        item.get("node", {}) for item in similar.get("similar", []) if item.get("node")
    ]
    similarity_edges = [
        item.get("edge", {}) for item in similar.get("similar", []) if item.get("edge")
    ]
    capabilities = impact.get("capabilities", {})
    return {
        "views": {
            "entrypoint": make_visual_slice(
                entrypoint_nodes,
                entrypoint_edges,
                view="entrypoint_circuit",
                capabilities=capabilities,
                limits=limits,
            ),
            "impact": make_visual_slice(
                impact_nodes,
                impact_edges,
                view="impact_radius",
                capabilities=capabilities,
                limits=limits,
            ),
            "similarity": make_visual_slice(
                similarity_nodes,
                similarity_edges,
                view="similarity_map",
                capabilities=similar.get("capabilities", {}),
                limits=limits,
            ),
            "architecture": make_visual_slice(
                _architecture_nodes(architecture),
                _architecture_edges(architecture),
                view="system_map",
                capabilities=architecture.get("capabilities", {}),
                limits=limits,
            ),
            "semantic": make_visual_slice(
                _semantic_slice_nodes(impact),
                _semantic_slice_edges(impact),
                view="unresolved_risk_map",
                diagnostics=impact.get("unresolved_risks", {}).get("items", []),
                summary=impact.get("confidence_summary", {}),
                capabilities=capabilities,
                status=impact.get("status", "available"),
                limits=limits,
            ),
        }
    }


def _architecture_nodes(architecture: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for cycle in architecture.get("import_cycles", []):
        for node_id in cycle:
            nodes.setdefault(
                node_id,
                {
                    "id": node_id,
                    "kind": "module",
                    "path": None,
                },
            )
    for item in architecture.get("top_fan_in", []):
        node_id = item.get("target")
        if node_id:
            nodes.setdefault(node_id, {"id": node_id, "kind": "fan_in", "path": None})
    for item in architecture.get("top_fan_out", []):
        node_id = item.get("source")
        if node_id:
            nodes.setdefault(node_id, {"id": node_id, "kind": "fan_out", "path": None})
    return list(nodes.values())


def _architecture_edges(architecture: dict[str, Any]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for cycle in architecture.get("import_cycles", []):
        for source, target in zip(cycle, cycle[1:], strict=False):
            edges.append({"source": source, "target": target, "kind": "cycle"})
    return edges


def _semantic_slice_nodes(impact: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for edge in _semantic_slice_edges(impact):
        for key in ("source", "target"):
            node_id = edge.get(key)
            if isinstance(node_id, str) and node_id:
                nodes.setdefault(
                    node_id, {"id": node_id, "kind": "semantic", "path": None}
                )
    return list(nodes.values())


def _semantic_slice_edges(impact: dict[str, Any]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for key in (
        "confirmed_impact",
        "inferred_impact",
        "runtime_impact",
        "heuristic_impact",
        "unresolved_impact",
    ):
        for edge in impact.get(key, []):
            if isinstance(edge, dict):
                edges.append(edge)
    return edges
