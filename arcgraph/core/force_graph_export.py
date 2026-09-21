"""Force-directed graph export payloads for interactive ArcGraph clients."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.visual_contract import (
    ENTRYPOINT_KINDS_SET,
    RESOURCE_NODE_KINDS_SET,
    visual_contract,
)

FORCE_GRAPH_SCHEMA = "ArcGraphForceGraph"
FORCE_GRAPH_VERSION = 1

DEFAULT_FORCE_EDGE_KINDS = (
    "imports",
    "calls",
    "constructs",
    "initializes",
    "extends",
    "implements",
    "overrides",
    "invokes",
    "injects",
    "provides",
    "enqueues",
    "consumes",
    "reads",
    "writes",
    "configures",
    "logs",
    "renders",
    "uses_hook",
    "maps_to",
    "exports",
    "registers",
)
STRUCTURAL_EDGE_KINDS = (
    "references",
    "uses",
    "contains",
    "defines",
    "declares",
)
CONTAINMENT_EDGE_KINDS = ("defines", "contains", "declares")
ALWAYS_EXCLUDED_EDGE_KINDS = ("similar_to", "covers", "dynamic_call")

BASE_EXCLUDED_NODE_KINDS = {
    "coverage_run",
    "diagnostic",
    "external_package",
    "source_root",
}
TEST_NODE_KINDS = {"test_case", "pytest_fixture"}
SYMBOL_EXCLUDED_NODE_KINDS = BASE_EXCLUDED_NODE_KINDS | {"module", "package"}

EDGE_KIND_WEIGHTS = {
    "invokes": 5.0,
    "calls": 5.0,
    "constructs": 4.5,
    "initializes": 4.0,
    "reads": 4.0,
    "writes": 4.0,
    "enqueues": 4.0,
    "consumes": 4.0,
    "injects": 3.5,
    "provides": 3.0,
    "renders": 3.0,
    "uses_hook": 3.0,
    "extends": 3.0,
    "implements": 3.0,
    "overrides": 3.0,
    "configures": 2.5,
    "logs": 2.0,
    "imports": 1.5,
    "exports": 1.5,
    "maps_to": 1.5,
    "registers": 1.5,
    "references": 0.3,
    "uses": 0.3,
    "contains": 0.1,
    "defines": 0.1,
    "declares": 0.1,
}
CONFIDENCE_WEIGHTS = {
    "confirmed": 1.0,
    "inferred": 0.75,
    "runtime-only": 0.65,
    "heuristic": 0.35,
    "unresolved": 0.1,
}
CONFIDENCE_RANK = {
    "confirmed": 0,
    "inferred": 1,
    "runtime-only": 2,
    "heuristic": 3,
    "unresolved": 4,
}
DATA_NODE_KINDS = {
    "pydantic_model",
    "interface",
    "type_alias",
    "model_field",
}


@dataclass(frozen=True)
class ForceGraphExportOptions:
    """Controls for the semantic force graph export."""

    max_symbol_nodes: int = 400
    min_symbol_degree: int = 5
    max_module_symbols: int = 50
    min_module_edge_weight: int = 1
    include_tests: bool = False
    include_generated: bool = False
    include_external: bool = False
    include_structural_edges: bool = False
    include_edge_kinds: tuple[str, ...] = ()
    exclude_edge_kinds: tuple[str, ...] = ()
    initial_focus: tuple[str, ...] = ()
    focus_depth: int = 1
    focus_direction: str = "both"


def build_force_graph_export(
    query_engine: QueryEngine,
    options: ForceGraphExportOptions | None = None,
) -> dict[str, Any]:
    """Build a production-oriented force graph payload from the current index.

    The exporter intentionally goes through ``QueryEngine`` and its current
    ``GraphStoreReader`` so callers inherit current-index resolution, schema
    loading, and freshness metadata instead of taking a raw SQLite path.
    """

    options = options or ForceGraphExportOptions()
    current = query_engine.current()
    edge_kinds = _effective_edge_kinds(options)

    with query_engine.store.connect() as conn:
        all_nodes = _load_nodes(conn)
        eligible_nodes, exclusion_counts = _eligible_nodes(all_nodes, options)
        edges = _load_edges(conn, edge_kinds)
        containment_edges = _load_edges(conn, CONTAINMENT_EDGE_KINDS)

    module_graph = _module_graph(
        eligible_nodes,
        edges,
        options=options,
    )
    symbol_graph = _symbol_graph(
        eligible_nodes,
        edges,
        options=options,
    )
    focus_index = _focus_index(
        eligible_nodes,
        edges,
        options=options,
    )
    focus_index["counts"]["overview_symbol_nodes"] = len(symbol_graph["nodes"])
    focus_state, focus_warnings = _focus_state(
        all_nodes=all_nodes,
        eligible_nodes=eligible_nodes,
        focus_index=focus_index,
        options=options,
    )
    module_symbols = _module_symbols(
        eligible_nodes,
        containment_edges,
        edges,
        options=options,
    )

    warnings = []
    freshness = current.get("freshness", {})
    if freshness.get("status") not in {None, "fresh"}:
        warnings.append(
            {
                "kind": "freshness",
                "message": "Current ArcGraph index is not fresh.",
                "freshness": freshness,
            }
        )

    warnings.extend(focus_warnings)

    return {
        "schema": FORCE_GRAPH_SCHEMA,
        "version": FORCE_GRAPH_VERSION,
        "status": "available",
        "scope": {
            "repo_id": str(query_engine.store.metadata.get("repo_id") or "default"),
            "index_version": str(
                query_engine.store.metadata.get("index_version") or "unknown"
            ),
            "source_roots": list(current.get("source_roots", [])),
        },
        "module_graph": module_graph,
        "symbol_graph": symbol_graph,
        "focus_index": focus_index,
        "focus": focus_state,
        "module_symbols": module_symbols,
        "meta": {
            "total_nodes": len(module_graph["nodes"]) + len(symbol_graph["nodes"]),
            "total_edges": len(module_graph["edges"]) + len(symbol_graph["edges"]),
            "raw_node_count": len(all_nodes),
            "eligible_node_count": len(eligible_nodes),
            "excluded_node_counts": dict(sorted(exclusion_counts.items())),
            "filters": _filters_payload(options, edge_kinds),
            "freshness": freshness,
            "capabilities": current.get("capabilities", {}),
            "visual_contract": visual_contract(),
        },
        "warnings": warnings,
    }


def _focus_index(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    options: ForceGraphExportOptions,
) -> dict[str, Any]:
    symbol_ids = _focus_symbol_ids(nodes, options)
    degree: Counter[str] = Counter()
    focus_edges: list[dict[str, Any]] = []
    adjacency: dict[str, dict[str, set[str]]] = {
        node_id: {"incoming": set(), "outgoing": set()} for node_id in symbol_ids
    }

    for edge_index, edge in enumerate(edges):
        source = edge["source"]
        target = edge["target"]
        if source not in symbol_ids or target not in symbol_ids or source == target:
            continue
        weight = _edge_layout_weight(edge)
        degree[source] += weight
        degree[target] += weight
        focus_edges.append(
            {
                **edge,
                "id": f"focus-edge:{edge_index}",
                "layout_weight": round(weight, 3),
            }
        )
        adjacency[source]["outgoing"].add(target)
        adjacency[target]["incoming"].add(source)

    focus_nodes = [
        _export_node(nodes[node_id], degree=round(float(degree[node_id]), 3))
        for node_id in symbol_ids
    ]
    return {
        "nodes": sorted(
            focus_nodes,
            key=lambda node: (
                str(node.get("qualname") or node.get("name") or ""),
                node["id"],
            ),
        ),
        "edges": sorted(
            focus_edges,
            key=lambda edge: (
                edge["source"],
                edge["target"],
                edge["kind"],
                edge.get("confidence") or "",
                edge["id"],
            ),
        ),
        "adjacency": {
            node_id: {
                "incoming": sorted(values["incoming"]),
                "outgoing": sorted(values["outgoing"]),
            }
            for node_id, values in sorted(adjacency.items())
        },
        "counts": {
            "nodes": len(symbol_ids),
            "edges": len(focus_edges),
            "overview_symbol_nodes": 0,
        },
    }


def _focus_state(
    *,
    all_nodes: dict[str, dict[str, Any]],
    eligible_nodes: dict[str, dict[str, Any]],
    focus_index: dict[str, Any],
    options: ForceGraphExportOptions,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    focus_nodes_by_id = {node["id"]: node for node in focus_index.get("nodes", [])}
    focus_ids = set(focus_nodes_by_id)
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    seed_ids: set[str] = set()
    warnings: list[dict[str, Any]] = []

    for target in options.initial_focus:
        clean_target = target.strip()
        if not clean_target:
            continue
        matches = _match_focus_target(focus_nodes_by_id, clean_target)
        if matches:
            resolved.append({"target": clean_target, "node_ids": matches})
            seed_ids.update(matches)
            continue

        all_matches = _match_raw_target(all_nodes, clean_target)
        if all_matches:
            excluded_reasons = sorted(
                {
                    _focus_unavailable_reason(
                        all_nodes[node_id],
                        eligible_nodes=eligible_nodes,
                        focus_ids=focus_ids,
                        options=options,
                    )
                    for node_id in all_matches
                }
            )
            reason = excluded_reasons[0] if excluded_reasons else "not_focusable"
            warning = {
                "kind": "focus_target_excluded",
                "target": clean_target,
                "reason": reason,
                "message": (
                    f"Focus target '{clean_target}' is not available in the "
                    "static focus index; regenerate with matching visual flags."
                ),
            }
        else:
            warning = {
                "kind": "focus_target_not_found",
                "target": clean_target,
                "reason": "not_found",
                "message": f"Focus target '{clean_target}' was not found.",
            }
        unresolved.append(
            {
                "target": clean_target,
                "reason": warning["reason"],
                "message": warning["message"],
            }
        )
        warnings.append(warning)

    focus_view = build_focus_view(
        focus_index,
        targets=options.initial_focus,
        depth=options.focus_depth,
        direction=options.focus_direction,
    )
    selection = focus_view["focus"]
    return (
        {
            "mode": "initial_focus" if seed_ids else "overview",
            "requested_targets": [
                target.strip() for target in options.initial_focus if target.strip()
            ],
            "depth": _normalized_focus_depth(options.focus_depth),
            "direction": _normalized_focus_direction(options.focus_direction),
            "resolved": resolved,
            "unresolved": unresolved,
            "resolved_seed_ids": sorted(seed_ids),
            "visible_node_ids": selection["visible_node_ids"],
            "visible_edge_ids": selection["visible_edge_ids"],
            "warnings": warnings,
        },
        warnings,
    )


def build_focus_view(
    focus_index: dict[str, Any],
    *,
    targets: tuple[str, ...] | list[str],
    depth: int = 1,
    direction: str = "both",
) -> dict[str, Any]:
    """Build a compact ego-graph view from an exported focus index."""

    focus_nodes_by_id = {node["id"]: node for node in focus_index.get("nodes", [])}
    focus_edges_by_id = {edge["id"]: edge for edge in focus_index.get("edges", [])}
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    seed_ids: set[str] = set()
    warnings: list[dict[str, Any]] = []

    for target in targets:
        clean_target = str(target or "").strip()
        if not clean_target:
            continue
        matches = _match_focus_target(focus_nodes_by_id, clean_target)
        if matches:
            resolved.append({"target": clean_target, "node_ids": matches})
            seed_ids.update(matches)
            continue
        warning = {
            "kind": "focus_target_not_found",
            "target": clean_target,
            "reason": "not_found",
            "message": f"Focus target '{clean_target}' was not found.",
        }
        unresolved.append(
            {
                "target": clean_target,
                "reason": warning["reason"],
                "message": warning["message"],
            }
        )
        warnings.append(warning)

    selection = _ego_selection(
        focus_index,
        seed_ids=seed_ids,
        depth=depth,
        direction=direction,
    )
    node_ids = selection["node_ids"]
    edge_ids = selection["edge_ids"]
    nodes = [
        focus_nodes_by_id[node_id]
        for node_id in node_ids
        if node_id in focus_nodes_by_id
    ]
    edges = [
        focus_edges_by_id[edge_id]
        for edge_id in edge_ids
        if edge_id in focus_edges_by_id
    ]
    focus_counts = focus_index.get("counts", {})
    total_nodes = int(focus_counts.get("nodes") or len(focus_nodes_by_id))
    total_edges = int(focus_counts.get("edges") or len(focus_edges_by_id))
    return {
        "schema": "ArcGraphFocusView",
        "version": 1,
        "status": "available" if seed_ids else ("partial" if targets else "available"),
        "focus": {
            "mode": "initial_focus" if seed_ids else "overview",
            "requested_targets": [
                str(target).strip() for target in targets if str(target).strip()
            ],
            "depth": _normalized_focus_depth(depth),
            "direction": _normalized_focus_direction(direction),
            "resolved": resolved,
            "unresolved": unresolved,
            "resolved_seed_ids": sorted(seed_ids),
            "visible_node_ids": node_ids,
            "visible_edge_ids": edge_ids,
            "warnings": warnings,
        },
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "hidden_nodes": max(0, total_nodes - len(nodes)),
            "hidden_edges": max(0, total_edges - len(edges)),
            "total_focus_nodes": total_nodes,
            "total_focus_edges": total_edges,
        },
        "warnings": warnings,
    }


def _focus_symbol_ids(
    nodes: dict[str, dict[str, Any]],
    options: ForceGraphExportOptions,
) -> set[str]:
    return {
        node_id
        for node_id, node in nodes.items()
        if node.get("kind") not in SYMBOL_EXCLUDED_NODE_KINDS
        and (node.get("kind") != "external_symbol" or options.include_external)
    }


def _match_focus_target(
    focus_nodes_by_id: dict[str, dict[str, Any]],
    target: str,
) -> list[str]:
    return sorted(
        node_id
        for node_id, node in focus_nodes_by_id.items()
        if _node_matches_target(node, target)
    )


def _match_raw_target(nodes: dict[str, dict[str, Any]], target: str) -> list[str]:
    return sorted(
        node_id for node_id, node in nodes.items() if _node_matches_target(node, target)
    )


def _node_matches_target(node: dict[str, Any], target: str) -> bool:
    normalized_target_path = _normalize_path_value(target)
    identity_values = {
        str(node.get("id") or ""),
        str(node.get("name") or ""),
        str(node.get("qualname") or ""),
        str(node.get("canonical_identity") or ""),
    }
    if target in identity_values:
        return True
    path = _normalize_path_value(str(node.get("path") or ""))
    if normalized_target_path and path == normalized_target_path:
        return True
    qualname = str(node.get("qualname") or "")
    return bool(qualname and qualname.endswith(f".{target}"))


def _focus_unavailable_reason(
    node: dict[str, Any],
    *,
    eligible_nodes: dict[str, dict[str, Any]],
    focus_ids: set[str],
    options: ForceGraphExportOptions,
) -> str:
    if node["id"] in focus_ids:
        return "available"
    if node["id"] not in eligible_nodes:
        return _node_exclusion_reason(node, options) or "excluded"
    if node.get("kind") in SYMBOL_EXCLUDED_NODE_KINDS:
        return "not_symbol"
    return "not_focusable"


def _ego_selection(
    focus_index: dict[str, Any],
    *,
    seed_ids: set[str],
    depth: int,
    direction: str,
) -> dict[str, list[str]]:
    if not seed_ids:
        return {"node_ids": [], "edge_ids": []}

    safe_depth = _normalized_focus_depth(depth)
    safe_direction = _normalized_focus_direction(direction)
    edges = focus_index.get("edges", [])
    frontier = set(seed_ids)
    visited = set(seed_ids)
    traversed_edges: set[str] = set()

    for _step in range(safe_depth):
        next_frontier: set[str] = set()
        for edge in edges:
            source = str(edge["source"])
            target = str(edge["target"])
            if safe_direction in {"outgoing", "both"} and source in frontier:
                next_frontier.add(target)
                traversed_edges.add(str(edge["id"]))
            if safe_direction in {"incoming", "both"} and target in frontier:
                next_frontier.add(source)
                traversed_edges.add(str(edge["id"]))
        next_frontier.difference_update(visited)
        if not next_frontier:
            break
        visited.update(next_frontier)
        frontier = next_frontier

    visible_edges = {
        str(edge["id"])
        for edge in edges
        if edge["source"] in visited
        and edge["target"] in visited
        and edge["id"] in traversed_edges
    }
    return {"node_ids": sorted(visited), "edge_ids": sorted(visible_edges)}


def _normalized_focus_depth(depth: int) -> int:
    return max(1, min(int(depth or 1), 2))


def _normalized_focus_direction(direction: str) -> str:
    return direction if direction in {"incoming", "outgoing", "both"} else "both"


def _load_nodes(conn: Any) -> dict[str, dict[str, Any]]:
    rows = conn.execute("""
        SELECT n.id, n.kind, n.name, n.qualname, n.path, n.start_line, n.end_line,
               n.canonical_identity, n.properties_json,
               f.source_root, f.module AS file_module, f.line_count AS file_line_count
        FROM nodes n
        LEFT JOIN files f ON n.path = f.path
        ORDER BY n.id
        """).fetchall()
    nodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        properties = _json_object(row["properties_json"])
        path = row["path"] or ""
        source_root = row["source_root"] or _source_root_from_path(path)
        nodes[row["id"]] = {
            "id": row["id"],
            "kind": row["kind"],
            "name": row["name"],
            "qualname": row["qualname"] or row["name"],
            "path": path,
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "canonical_identity": row["canonical_identity"],
            "properties": properties,
            "source_root": source_root,
            "file_module": row["file_module"],
            "line_count": int(
                properties.get("line_count") or row["file_line_count"] or 0
            ),
            "package": _package_for_path(path),
            "sub_package": _sub_package_for_path(path, source_root),
        }
    return nodes


def _load_edges(conn: Any, edge_kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    if not edge_kinds:
        return []
    placeholders = ",".join("?" for _ in edge_kinds)
    return [
        {
            "source": row["source"],
            "target": row["target"],
            "kind": row["kind"],
            "confidence": row["confidence"] or "confirmed",
            "semantic_role": row["semantic_role"],
        }
        for row in conn.execute(
            f"""
            SELECT source, target, kind, confidence, semantic_role
            FROM edges
            WHERE kind IN ({placeholders})
            ORDER BY source, target, kind, confidence
            """,
            edge_kinds,
        )
    ]


def _eligible_nodes(
    nodes: dict[str, dict[str, Any]],
    options: ForceGraphExportOptions,
) -> tuple[dict[str, dict[str, Any]], Counter[str]]:
    eligible: dict[str, dict[str, Any]] = {}
    excluded: Counter[str] = Counter()
    for node_id, node in nodes.items():
        reason = _node_exclusion_reason(node, options)
        if reason:
            excluded[reason] += 1
            continue
        eligible[node_id] = node
    return eligible, excluded


def _node_exclusion_reason(
    node: dict[str, Any],
    options: ForceGraphExportOptions,
) -> str | None:
    kind = str(node.get("kind") or "")
    path = str(node.get("path") or "")
    source_root = str(node.get("source_root") or "")
    if kind in BASE_EXCLUDED_NODE_KINDS:
        return "node_kind"
    if kind == "external_symbol" and not options.include_external:
        return "external"
    if not options.include_tests and (
        kind in TEST_NODE_KINDS or _is_test_path(path, source_root)
    ):
        return "tests"
    if not options.include_generated and _is_generated_path(path):
        return "generated"
    return None


def _module_graph(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    options: ForceGraphExportOptions,
) -> dict[str, Any]:
    module_ids = {
        node_id for node_id, node in nodes.items() if node.get("kind") == "module"
    }
    path_to_module = {
        str(node.get("path") or ""): node_id
        for node_id, node in nodes.items()
        if node_id in module_ids and node.get("path")
    }
    node_to_module = _node_to_module_map(nodes, module_ids, path_to_module)
    symbol_counts = Counter(
        module_id
        for node_id, module_id in node_to_module.items()
        if node_id not in module_ids and module_id in module_ids
    )
    module_tiers = _module_semantic_tiers(nodes, node_to_module, module_ids)

    aggregate: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in edges:
        source_module = node_to_module.get(edge["source"])
        target_module = node_to_module.get(edge["target"])
        if (
            not source_module
            or not target_module
            or source_module == target_module
            or source_module not in module_ids
            or target_module not in module_ids
        ):
            continue
        key = (source_module, target_module, edge["kind"])
        if key not in aggregate:
            aggregate[key] = {
                "source": source_module,
                "target": target_module,
                "kind": edge["kind"],
                "weight": 0,
                "layout_weight": 0.0,
                "confidence_counts": Counter(),
            }
        aggregate[key]["weight"] += 1
        aggregate[key]["layout_weight"] += _edge_layout_weight(edge)
        aggregate[key]["confidence_counts"][edge["confidence"]] += 1

    graph_edges = []
    for edge in aggregate.values():
        if edge["weight"] < options.min_module_edge_weight:
            continue
        confidence_counts = edge.pop("confidence_counts")
        edge["dominant_confidence"] = _dominant_confidence(confidence_counts)
        edge["confidence_counts"] = dict(sorted(confidence_counts.items()))
        edge["layout_weight"] = round(edge["layout_weight"], 3)
        graph_edges.append(edge)

    graph_nodes = [
        _export_node(
            node,
            symbol_count=symbol_counts.get(node_id, 0),
            semantic_tier=module_tiers.get(node_id, "logic"),
        )
        for node_id, node in nodes.items()
        if node_id in module_ids
    ]
    return {
        "nodes": sorted(graph_nodes, key=lambda node: node["id"]),
        "edges": sorted(
            graph_edges,
            key=lambda edge: (
                -float(edge.get("layout_weight") or 0),
                edge["source"],
                edge["target"],
                edge["kind"],
            ),
        ),
    }


def _symbol_graph(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    options: ForceGraphExportOptions,
) -> dict[str, Any]:
    symbol_ids = {
        node_id
        for node_id, node in nodes.items()
        if node.get("kind") not in SYMBOL_EXCLUDED_NODE_KINDS
        and (node.get("kind") != "external_symbol" or options.include_external)
    }
    degree: Counter[str] = Counter()
    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        if source in symbol_ids and target in symbol_ids and source != target:
            weight = _edge_layout_weight(edge)
            degree[source] += weight
            degree[target] += weight

    selected = {
        node_id
        for node_id, weighted_degree in degree.most_common(options.max_symbol_nodes)
        if weighted_degree >= options.min_symbol_degree
    }
    graph_edges = [
        {
            **edge,
            "layout_weight": round(_edge_layout_weight(edge), 3),
        }
        for edge in edges
        if edge["source"] in selected
        and edge["target"] in selected
        and edge["source"] != edge["target"]
    ]
    graph_nodes = [
        _export_node(nodes[node_id], degree=round(float(degree[node_id]), 3))
        for node_id in selected
    ]
    return {
        "nodes": sorted(
            graph_nodes,
            key=lambda node: (-float(node.get("degree") or 0), node["id"]),
        ),
        "edges": sorted(
            graph_edges,
            key=lambda edge: (
                edge["source"],
                edge["target"],
                edge["kind"],
                edge.get("confidence") or "",
            ),
        ),
    }


def _module_symbols(
    nodes: dict[str, dict[str, Any]],
    containment_edges: list[dict[str, Any]],
    semantic_edges: list[dict[str, Any]],
    *,
    options: ForceGraphExportOptions,
) -> dict[str, Any]:
    module_ids = {
        node_id for node_id, node in nodes.items() if node.get("kind") == "module"
    }
    children_by_module: dict[str, list[str]] = defaultdict(list)
    for edge in containment_edges:
        source = edge["source"]
        target = edge["target"]
        target_node = nodes.get(target)
        if (
            source in module_ids
            and target_node is not None
            and target_node.get("kind") not in SYMBOL_EXCLUDED_NODE_KINDS
        ):
            children_by_module[source].append(target)

    semantic_edges_by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
    child_to_module = {
        child: module_id
        for module_id, children in children_by_module.items()
        for child in children
    }
    for edge in semantic_edges:
        module_id = child_to_module.get(edge["source"])
        if module_id and child_to_module.get(edge["target"]) == module_id:
            semantic_edges_by_module[module_id].append(
                {
                    **edge,
                    "layout_weight": round(_edge_layout_weight(edge), 3),
                }
            )

    result: dict[str, Any] = {}
    for module_id, children in sorted(children_by_module.items()):
        unique_children = sorted(set(children))
        if not unique_children or len(unique_children) > options.max_module_symbols:
            continue
        result[module_id] = {
            "nodes": [_export_node(nodes[child]) for child in unique_children],
            "edges": sorted(
                semantic_edges_by_module.get(module_id, []),
                key=lambda edge: (
                    edge["source"],
                    edge["target"],
                    edge["kind"],
                    edge.get("confidence") or "",
                ),
            ),
        }
    return result


def _node_to_module_map(
    nodes: dict[str, dict[str, Any]],
    module_ids: set[str],
    path_to_module: dict[str, str],
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for node_id, node in nodes.items():
        if node_id in module_ids:
            mapping[node_id] = node_id
            continue
        path = str(node.get("path") or "")
        module_id = path_to_module.get(path)
        if module_id:
            mapping[node_id] = module_id
    return mapping


def _module_semantic_tiers(
    nodes: dict[str, dict[str, Any]],
    node_to_module: dict[str, str],
    module_ids: set[str],
) -> dict[str, str]:
    """Classify modules from contained node kinds instead of path/name regexes."""

    counts: dict[str, Counter[str]] = {module_id: Counter() for module_id in module_ids}
    for node_id, module_id in node_to_module.items():
        if node_id in module_ids or module_id not in counts:
            continue
        tier = _semantic_tier_for_kind(str(nodes[node_id].get("kind") or ""))
        if tier != "logic":
            counts[module_id][tier] += 1

    priority = {"entry": 0, "resource": 1, "data": 2, "logic": 3}
    result: dict[str, str] = {}
    for module_id, tier_counts in counts.items():
        if not tier_counts:
            result[module_id] = "logic"
            continue
        result[module_id] = sorted(
            tier_counts,
            key=lambda tier: (-tier_counts[tier], priority.get(tier, 99), tier),
        )[0]
    return result


def _semantic_tier_for_kind(kind: str) -> str:
    if kind in ENTRYPOINT_KINDS_SET:
        return "entry"
    if kind in RESOURCE_NODE_KINDS_SET:
        return "resource"
    if kind in DATA_NODE_KINDS:
        return "data"
    return "logic"


def _export_node(
    node: dict[str, Any],
    *,
    symbol_count: int | None = None,
    degree: float | None = None,
    semantic_tier: str | None = None,
) -> dict[str, Any]:
    properties = dict(node.get("properties") or {})
    kind = str(node["kind"])
    exported = {
        "id": node["id"],
        "kind": kind,
        "name": node["name"],
        "qualname": node["qualname"],
        "path": node.get("path"),
        "source_root": node.get("source_root"),
        "package": node.get("package") or "",
        "sub_package": node.get("sub_package") or "",
        "line_count": node.get("line_count") or 0,
        "semantic_tier": semantic_tier or _semantic_tier_for_kind(kind),
    }
    if node.get("start_line") is not None:
        exported["start_line"] = node["start_line"]
    if node.get("end_line") is not None:
        exported["end_line"] = node["end_line"]
    if symbol_count is not None:
        exported["symbol_count"] = symbol_count
    if degree is not None:
        exported["degree"] = degree
    if properties:
        exported["properties"] = _small_properties(properties)
    return exported


def _small_properties(properties: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "route_path",
        "method",
        "framework",
        "queue",
        "table",
        "resource_kind",
        "symbol_count",
        "line_count",
    }
    return {key: properties[key] for key in sorted(allowed & set(properties))}


def _effective_edge_kinds(options: ForceGraphExportOptions) -> tuple[str, ...]:
    if options.include_edge_kinds:
        edge_kinds = set(options.include_edge_kinds)
    else:
        edge_kinds = set(DEFAULT_FORCE_EDGE_KINDS)
        if options.include_structural_edges:
            edge_kinds.update(STRUCTURAL_EDGE_KINDS)
    edge_kinds.difference_update(ALWAYS_EXCLUDED_EDGE_KINDS)
    edge_kinds.difference_update(options.exclude_edge_kinds)
    return tuple(sorted(edge_kinds))


def _filters_payload(
    options: ForceGraphExportOptions,
    edge_kinds: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "include_tests": options.include_tests,
        "include_generated": options.include_generated,
        "include_external": options.include_external,
        "include_structural_edges": options.include_structural_edges,
        "edge_kinds": list(edge_kinds),
        "exclude_edge_kinds": list(options.exclude_edge_kinds),
        "max_symbol_nodes": options.max_symbol_nodes,
        "min_symbol_degree": options.min_symbol_degree,
        "max_module_symbols": options.max_module_symbols,
        "min_module_edge_weight": options.min_module_edge_weight,
        "initial_focus": list(options.initial_focus),
        "focus_depth": _normalized_focus_depth(options.focus_depth),
        "focus_direction": _normalized_focus_direction(options.focus_direction),
    }


def _dominant_confidence(counts: Counter[str]) -> str:
    if not counts:
        return "confirmed"
    return sorted(
        counts,
        key=lambda confidence: (
            -counts[confidence],
            CONFIDENCE_RANK.get(confidence, 99),
            confidence,
        ),
    )[0]


def _edge_layout_weight(edge: dict[str, Any]) -> float:
    kind_weight = EDGE_KIND_WEIGHTS.get(str(edge.get("kind") or ""), 1.0)
    confidence_weight = CONFIDENCE_WEIGHTS.get(
        str(edge.get("confidence") or "confirmed"),
        0.25,
    )
    return kind_weight * confidence_weight


def _package_for_path(path: str) -> str:
    parts = _path_parts(path)
    return parts[0] if parts else ""


def _sub_package_for_path(path: str, source_root: str | None) -> str:
    parts = _path_parts(path)
    if not parts:
        return source_root or ""
    if source_root:
        root_parts = _path_parts(source_root)
        if parts[: len(root_parts)] == root_parts:
            depth = min(len(parts), len(root_parts) + 1)
            return "/".join(parts[:depth])
    return "/".join(parts[: min(len(parts), 2)])


def _source_root_from_path(path: str) -> str:
    parts = _path_parts(path)
    return parts[0] if parts else ""


def _is_test_path(path: str, source_root: str) -> bool:
    parts = [part.lower() for part in _path_parts(path)]
    root_parts = [part.lower() for part in _path_parts(source_root)]
    directory_parts = parts[:-1]
    if any(part in {"test", "tests", "__tests__"} for part in root_parts):
        return True
    if any(part in {"test", "tests", "__tests__"} for part in directory_parts):
        return True
    test_roots = {"e2e", "unit", "integration"}
    if root_parts and root_parts[-1] in test_roots:
        return True
    if directory_parts and directory_parts[0] in test_roots:
        return True
    return len(directory_parts) > 1 and directory_parts[1] in test_roots


def _is_generated_path(path: str) -> bool:
    parts = [part.lower() for part in _path_parts(path)]
    if not parts:
        return False
    directory_parts = parts[:-1]
    generated_dirs = {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "htmlcov-integration",
        "htmlcov-unit",
        "node_modules",
        "output",
    }
    if any(part in generated_dirs for part in directory_parts):
        return True
    if any("backup" in part for part in directory_parts):
        return True
    return "coverage_html" in parts[-1]


def _path_parts(path: str | None) -> list[str]:
    if not path:
        return []
    return [part for part in str(path).replace("\\", "/").split("/") if part]


def _normalize_path_value(path: str | None) -> str:
    return "/".join(_path_parts(path))


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "FORCE_GRAPH_SCHEMA",
    "FORCE_GRAPH_VERSION",
    "ForceGraphExportOptions",
    "build_force_graph_export",
]
