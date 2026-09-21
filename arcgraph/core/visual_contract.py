"""Shared VisualSlice capability contract for ArcGraph consumers.

This module is the **single source of truth** for domain constants used by
the backend (QueryEngine, VisualSliceEngine).  The ``visual_contract()``
function exposes these constants as an API payload so that frontend consumers
can derive all domain-specific configuration dynamically instead of
maintaining hardcoded copies.

.. note::

   The frontend ``ArcGraphConstants.ts`` still contains its own hardcoded
   copies.  Full migration to consuming ``visual_contract()`` is planned as
   part of the frontend visualisation redesign (Phase 2.5).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# View catalogue
# ---------------------------------------------------------------------------

SUPPORTED_VIEW_ORDER: list[str] = [
    "system_map",
    "module_map",
    "symbol_map",
    "impact_radius",
    "entrypoint_circuit",
    "resource_flow",
    "similarity_map",
    "unresolved_risk_map",
]

DEFAULT_VIEW: str = "system_map"

TARGET_REQUIRED_VIEWS: list[str] = [
    "module_map",
    "symbol_map",
    "impact_radius",
    "entrypoint_circuit",
    "resource_flow",
    "similarity_map",
]

# ---------------------------------------------------------------------------
# Node-kind classification
# ---------------------------------------------------------------------------

# Navigable entry-point kinds (search dropdown, entrypoint-flow traversal).
# ``queue`` is included because it serves as a valid starting point for flow
# exploration, even though it is also classified as a resource for
# architecture statistics.
ENTRYPOINT_KINDS: list[str] = [
    "route",
    "mcp_tool",
    "worker_task",
    "cli_command",
    "test_case",
    "component",
    "queue",
]

# Resource node kinds (data stores, queues, config, logging sinks).
RESOURCE_NODE_KINDS: list[str] = [
    "table",
    "queue",
    "config",
    "log_sink",
    "pydantic_model",
]

# General-purpose search kinds shown in the search bar.
SEARCHABLE_NODE_KINDS: list[str] = [
    "function",
    "method",
    "component",
    "class",
    "interface",
    "type_alias",
    "enum",
    "module",
    "route",
    "mcp_tool",
    "worker_task",
    "table",
    "queue",
    "test_case",
]

# ---------------------------------------------------------------------------
# Edge-kind classification
# ---------------------------------------------------------------------------

# Edge kinds representing resource I/O (used by resource_flow view).
RESOURCE_EDGE_KINDS: list[str] = [
    "reads",
    "writes",
    "enqueues",
    "consumes",
    "publishes",
    "configures",
]

# Edge kinds representing structural relationships (class hierarchy + containment).
STRUCTURAL_EDGE_KINDS: list[str] = [
    "defines",
    "exports",
    "extends",
    "implements",
    "overrides",
    "contains",
]

# Containment-only edges: used for architecture/structural views but MUST NOT
# participate in impact analysis or call traversal. This is an architecture
# contract.
CONTAINMENT_EDGE_KINDS: list[str] = [
    "contains",
]

# Structural container node kinds: represent organizational groupings
# (source roots, packages) rather than code symbols.
CONTAINER_NODE_KINDS: list[str] = [
    "source_root",
    "package",
]

# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

CONFIDENCE_FILTERS: list[str] = [
    "all",
    "confirmed",
    "inferred",
    "heuristic",
    "runtime-only",
    "unresolved",
]

# ---------------------------------------------------------------------------
# Lane ordering (visual layout)
# ---------------------------------------------------------------------------

LANE_ORDER: list[str] = [
    "entrypoints",
    "symbols",
    "modules",
    "resources",
    "diagnostics",
    "source_roots",
    "node_kinds",
]

# ---------------------------------------------------------------------------
# Kind → ID-prefix mapping (for constructing backend search queries)
# ---------------------------------------------------------------------------

KIND_TO_ID_PREFIX: dict[str, str] = {
    "route": "route",
    "mcp_tool": "mcp_tool",
    "worker_task": "worker",
    "component": "component",
    "queue": "queue",
    "cli_command": "cli",
    "test_case": "test",
}

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PROFILE: str = "review_default"

DEFAULT_LIMITS: dict[str, int] = {
    "max_nodes": 500,
    "max_edges": 500,
    "max_diagnostics": 50,
}


# ---------------------------------------------------------------------------
# Pre-computed sets for O(1) membership checks (internal use)
# ---------------------------------------------------------------------------

ENTRYPOINT_KINDS_SET: frozenset[str] = frozenset(ENTRYPOINT_KINDS)
RESOURCE_NODE_KINDS_SET: frozenset[str] = frozenset(RESOURCE_NODE_KINDS)


def entrypoint_stat_kinds() -> list[str]:
    """Return entrypoint kinds for architecture-level counting.

    Excludes kinds that are also classified as resources (e.g. ``queue``)
    so that each kind appears in exactly one architecture section. Test cases
    remain valid entrypoint-flow starting points, but are excluded from
    dashboard architecture counts because they describe verification surface
    rather than external product/system boundaries.
    """
    return [
        k
        for k in ENTRYPOINT_KINDS
        if k not in RESOURCE_NODE_KINDS_SET and k != "test_case"
    ]


# ---------------------------------------------------------------------------
# Contract API
# ---------------------------------------------------------------------------


def visual_contract() -> dict[str, Any]:
    """Return the read-only visual capability contract exposed by the API.

    This is the **single payload** that the frontend should use to derive all
    domain-specific configuration instead of maintaining hardcoded constants.
    """

    return {
        # View catalogue
        "views": list(SUPPORTED_VIEW_ORDER),
        "default_view": DEFAULT_VIEW,
        "target_required_views": list(TARGET_REQUIRED_VIEWS),
        # Node-kind classification
        "entrypoint_kinds": list(ENTRYPOINT_KINDS),
        "resource_node_kinds": list(RESOURCE_NODE_KINDS),
        "searchable_kinds": list(SEARCHABLE_NODE_KINDS),
        # Edge-kind classification
        "resource_edge_kinds": list(RESOURCE_EDGE_KINDS),
        "structural_edge_kinds": list(STRUCTURAL_EDGE_KINDS),
        # Confidence
        "confidence_filters": list(CONFIDENCE_FILTERS),
        # Lane ordering
        "lane_order": list(LANE_ORDER),
        # ID prefix mapping
        "kind_to_id_prefix": dict(KIND_TO_ID_PREFIX),
        # Defaults
        "default_profile": DEFAULT_PROFILE,
        "default_limits": dict(DEFAULT_LIMITS),
    }
