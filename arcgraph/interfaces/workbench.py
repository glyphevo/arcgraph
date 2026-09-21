"""Static ArcGraph Explorer workbench generation."""

from __future__ import annotations

import json
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from arcgraph.core.force_graph_export import FORCE_GRAPH_SCHEMA, FORCE_GRAPH_VERSION
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.schemas import ContextRequest
from arcgraph.core.utils import strip_source_snippets
from arcgraph.interfaces.ci import run_ci_checks
from arcgraph.providers.context_provider import ContextProvider

WORKBENCH_STATUS_SCHEMA = "ArcGraphWorkbenchStatus"
WORKBENCH_STATUS_VERSION = 1
WORKBENCH_AUDIT_SCHEMA = "ArcGraphWorkbenchAuditIndex"
WORKBENCH_AUDIT_VERSION = 1
WORKBENCH_PAYLOAD_BUDGET_SCHEMA = "ArcGraphWorkbenchPayloadBudget"
WORKBENCH_PAYLOAD_BUDGET_VERSION = 1
WORKBENCH_ASSET_PACKAGE = "arcgraph.assets.workbench"
WORKBENCH_ASSET_EXCLUDES = {"__init__.py", "__pycache__"}
AUDIT_DEFAULT_MAX_NODES = 150
PAYLOAD_SIZE_SECTIONS = (
    "module_graph",
    "symbol_graph",
    "focus_index",
    "audit_index",
    "meta",
)
PAYLOAD_WARNING_THRESHOLDS = {
    "graph_data": 10 * 1024 * 1024,
    "focus_index": 8 * 1024 * 1024,
    "audit_index": 5 * 1024 * 1024,
}


@dataclass(frozen=True)
class WorkbenchAuditOptions:
    include: bool = True
    max_nodes: int = AUDIT_DEFAULT_MAX_NODES
    detail_level: str = "summary"


def build_workbench_status(query_engine: QueryEngine) -> dict[str, Any]:
    """Build the status payload consumed by the local visual workbench."""

    workspace = query_engine.workspace_status()
    ci = run_ci_checks(query_engine)
    semantic_quality = _check_by_name(ci, "semantic_quality_targets")
    evidence_health = _check_by_name(ci, "evidence_manifest")
    evidence_consistency = _check_by_name(ci, "evidence_commit_consistency")
    return {
        "schema": WORKBENCH_STATUS_SCHEMA,
        "version": WORKBENCH_STATUS_VERSION,
        "status": "available",
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace": workspace,
        "visual_contract": workspace.get("visual_contract", {}),
        "summary": {
            "freshness": workspace.get("freshness", {}),
            "capabilities": workspace.get("capabilities", {}),
            "evidence_manifest": workspace.get("evidence_manifest"),
            "ci": {
                "status": ci.get("status"),
                "summary": ci.get("summary", {}),
            },
            "semantic_quality": _compact_check(semantic_quality),
            "evidence_health": _compact_check(evidence_health),
            "evidence_commit_consistency": _compact_check(evidence_consistency),
        },
    }


def attach_workbench_audit_index(
    query_engine: QueryEngine,
    graph_payload: dict[str, Any],
    options: WorkbenchAuditOptions,
) -> dict[str, Any]:
    """Attach a compact static node audit index to a visual graph payload."""

    graph_payload["audit_index"] = build_workbench_audit_index(
        query_engine, graph_payload, options
    )
    return graph_payload


def attach_workbench_payload_budget(
    graph_payload: dict[str, Any],
    *,
    thresholds: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Attach static workbench payload-size metadata and non-fatal warnings."""

    graph_payload["payload_budget"] = build_workbench_payload_budget(
        graph_payload,
        thresholds=thresholds,
    )
    return graph_payload


def build_workbench_payload_budget(
    graph_payload: dict[str, Any],
    *,
    thresholds: dict[str, int] | None = None,
) -> dict[str, Any]:
    active_thresholds = dict(PAYLOAD_WARNING_THRESHOLDS)
    if thresholds:
        active_thresholds.update(thresholds)
    section_bytes = {
        section: _estimated_json_bytes(graph_payload.get(section, {}))
        for section in PAYLOAD_SIZE_SECTIONS
    }
    largest_name, largest_bytes = max(
        section_bytes.items(), key=lambda item: item[1], default=("none", 0)
    )
    base_payload = {
        key: value for key, value in graph_payload.items() if key != "payload_budget"
    }
    budget = {
        "schema": WORKBENCH_PAYLOAD_BUDGET_SCHEMA,
        "version": WORKBENCH_PAYLOAD_BUDGET_VERSION,
        "thresholds": active_thresholds,
        "sections": section_bytes,
        "graph_data_bytes": _estimated_json_bytes(base_payload),
        "largest_section": {
            "name": largest_name,
            "estimated_bytes": largest_bytes,
        },
        "warnings": [],
    }
    budget["warnings"] = _payload_budget_warnings(budget)
    return budget


def build_workbench_audit_index(
    query_engine: QueryEngine,
    graph_payload: dict[str, Any],
    options: WorkbenchAuditOptions,
) -> dict[str, Any]:
    if not options.include:
        return _empty_audit_index(enabled=False, options=options)

    focus_nodes = [
        node
        for node in graph_payload.get("focus_index", {}).get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    ]
    seed_ids = [
        seed
        for seed in graph_payload.get("focus", {}).get("resolved_seed_ids", [])
        if isinstance(seed, str)
    ]
    selected_ids = _select_audit_node_ids(
        [node["id"] for node in focus_nodes],
        seed_ids,
        max_nodes=options.max_nodes,
    )
    node_by_id = {node["id"]: node for node in focus_nodes}
    provider = ContextProvider(query_engine)
    nodes: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, str]] = []

    for node_id in selected_ids:
        try:
            nodes[node_id] = _build_node_audit_entry(
                provider,
                node_id=node_id,
                node=node_by_id.get(node_id, {"id": node_id}),
                detail_level=options.detail_level,
            )
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            warnings.append(
                {
                    "kind": "audit_node_unavailable",
                    "node_id": node_id,
                    "message": f"Could not build audit summary for {node_id}: {exc}",
                }
            )

    focus_node_count = len(focus_nodes)
    truncated = max(0, focus_node_count - len(nodes))
    payload = {
        "schema": WORKBENCH_AUDIT_SCHEMA,
        "version": WORKBENCH_AUDIT_VERSION,
        "enabled": True,
        "detail_level": options.detail_level,
        "max_nodes": max(0, options.max_nodes),
        "counts": {
            "focus_nodes": focus_node_count,
            "nodes": len(nodes),
            "truncated": truncated,
            "initial_focus_nodes": len(seed_ids),
        },
        "nodes": nodes,
        "warnings": warnings,
    }
    payload["estimated_bytes"] = _estimated_json_bytes(payload)
    payload["counts"]["estimated_bytes"] = payload["estimated_bytes"]
    return payload


def write_workbench(
    *,
    output_dir: Path,
    graph_payload: dict[str, Any],
    status_payload: dict[str, Any],
) -> dict[str, Any]:
    """Write packaged workbench assets plus graph and status JSON payloads."""

    output_dir.mkdir(parents=True, exist_ok=True)
    _copy_assets(output_dir)
    graph_path = output_dir / "graph_data.json"
    status_path = output_dir / "status_data.json"
    graph_path.write_text(
        json.dumps(graph_payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    status_path.write_text(
        json.dumps(status_payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "schema": "ArcGraphWorkbench",
        "version": 1,
        "status": "available",
        "path": str((output_dir / "index.html").resolve()),
        "output_dir": str(output_dir.resolve()),
        "artifacts": {
            "index_html": str((output_dir / "index.html").resolve()),
            "graph_data": str(graph_path.resolve()),
            "status_data": str(status_path.resolve()),
        },
        "graph": {
            "schema": graph_payload.get("schema", FORCE_GRAPH_SCHEMA),
            "version": graph_payload.get("version", FORCE_GRAPH_VERSION),
            "module_nodes": len(graph_payload.get("module_graph", {}).get("nodes", [])),
            "module_edges": len(graph_payload.get("module_graph", {}).get("edges", [])),
            "symbol_nodes": len(graph_payload.get("symbol_graph", {}).get("nodes", [])),
            "symbol_edges": len(graph_payload.get("symbol_graph", {}).get("edges", [])),
            "focus_nodes": graph_payload.get("focus_index", {})
            .get("counts", {})
            .get("nodes", 0),
            "focus_edges": graph_payload.get("focus_index", {})
            .get("counts", {})
            .get("edges", 0),
            "initial_focus_resolved": len(
                graph_payload.get("focus", {}).get("resolved_seed_ids", [])
            ),
            "audit_nodes": graph_payload.get("audit_index", {})
            .get("counts", {})
            .get("nodes", 0),
            "audit_truncated": graph_payload.get("audit_index", {})
            .get("counts", {})
            .get("truncated", 0),
            "audit_estimated_bytes": graph_payload.get("audit_index", {}).get(
                "estimated_bytes", 0
            ),
            "payload_estimated_bytes": graph_payload.get("payload_budget", {}).get(
                "graph_data_bytes", 0
            ),
            "largest_payload_section": graph_payload.get("payload_budget", {}).get(
                "largest_section", {}
            ),
            "payload_warnings": graph_payload.get("payload_budget", {}).get(
                "warnings", []
            ),
            "warnings": graph_payload.get("warnings", []),
        },
        "status_summary": _cli_status_summary(status_payload),
    }


def attach_browser_open_result(payload: dict[str, Any], target: str) -> dict[str, Any]:
    """Best-effort browser open helper used by local visual entrypoints."""

    try:
        opened = webbrowser.open(target)
    except Exception as exc:  # pragma: no cover - exercised via callers
        opened = False
        message = str(exc)
    else:
        message = "webbrowser.open returned False"
    if opened:
        payload["open"] = {
            "requested": True,
            "status": "opened",
            "target": target,
        }
        return payload

    warning = {
        "kind": "browser_open_failed",
        "target": target,
        "message": message,
    }
    payload["open"] = {
        "requested": True,
        "status": "warn",
        "target": target,
        "message": message,
    }
    payload.setdefault("warnings", []).append(warning)
    return payload


def _copy_assets(output_dir: Path) -> None:
    assets = resources.files(WORKBENCH_ASSET_PACKAGE)
    _copy_tree(assets, output_dir)


def _copy_tree(source: Any, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if child.name in WORKBENCH_ASSET_EXCLUDES:
            continue
        destination = target / child.name
        if child.is_dir():
            _copy_tree(child, destination)
        else:
            destination.write_bytes(child.read_bytes())


def _check_by_name(ci: dict[str, Any], name: str) -> dict[str, Any] | None:
    for check in ci.get("checks", []):
        if check.get("name") == name:
            return check
    return None


def _compact_check(check: dict[str, Any] | None) -> dict[str, Any] | None:
    if not check:
        return None
    return {
        "name": check.get("name"),
        "status": check.get("status"),
        "message": check.get("message"),
        "details": check.get("details", {}),
    }


def _cli_status_summary(status_payload: dict[str, Any]) -> dict[str, Any]:
    summary = status_payload.get("summary", {})
    freshness = summary.get("freshness") or {}
    ci = summary.get("ci") or {}
    evidence_health = summary.get("evidence_health") or {}
    semantic_quality = summary.get("semantic_quality") or {}
    return {
        "freshness": freshness.get("status"),
        "ci": ci.get("status"),
        "ci_counts": ci.get("summary", {}),
        "evidence_health": evidence_health.get("status"),
        "semantic_quality": semantic_quality.get("status"),
    }


def _empty_audit_index(
    *, enabled: bool, options: WorkbenchAuditOptions
) -> dict[str, Any]:
    return {
        "schema": WORKBENCH_AUDIT_SCHEMA,
        "version": WORKBENCH_AUDIT_VERSION,
        "enabled": enabled,
        "detail_level": options.detail_level,
        "max_nodes": max(0, options.max_nodes),
        "counts": {
            "focus_nodes": 0,
            "nodes": 0,
            "truncated": 0,
            "initial_focus_nodes": 0,
            "estimated_bytes": 0,
        },
        "estimated_bytes": 0,
        "nodes": {},
        "warnings": [],
    }


def _select_audit_node_ids(
    focus_node_ids: list[str], seed_ids: list[str], *, max_nodes: int
) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    focus_set = set(focus_node_ids)

    for node_id in focus_node_ids[: max(0, max_nodes)]:
        if node_id not in seen:
            selected.append(node_id)
            seen.add(node_id)

    for seed_id in seed_ids:
        if seed_id in focus_set and seed_id not in seen:
            selected.append(seed_id)
            seen.add(seed_id)
    return selected


def _build_node_audit_entry(
    provider: ContextProvider,
    *,
    node_id: str,
    node: dict[str, Any],
    detail_level: str,
) -> dict[str, Any]:
    request = ContextRequest(
        task="visual node audit",
        targets=[node_id],
        max_results=8 if detail_level == "standard" else 6,
        detail_level=detail_level,
        profile="review_default",
        include_source=False,
    )
    explain = provider.explain(request)
    explanation = (explain.get("explanations") or [{}])[0]
    impact = provider.compact_impact(node_id, request)
    audit = {
        "node": _node_summary(node, explanation),
        "status": explanation.get("status", explain.get("status")),
        "provenance": {
            "incoming_edges": explanation.get("incoming_edges", []),
            "outgoing_edges": explanation.get("outgoing_edges", []),
        },
        "confidence_summary": explanation.get("confidence_summary", {}),
        "resolution_summary": explanation.get("resolution_summary", {}),
        "impact": _audit_impact_summary(impact),
        "tests": {
            "candidates": impact.get("test_candidates", []),
            "gaps": impact.get("test_gaps", []),
            "coverage": impact.get("coverage", {}),
        },
        "unresolved": {
            "summary": explanation.get("unresolved", {}).get("summary", {}),
            "items": explanation.get("unresolved", {}).get("items", []),
            "impact_risks": impact.get("unresolved_risks", {}),
        },
        "recommended_next_reads": _merge_recommended_reads(
            explain.get("recommended_next_reads", []),
            impact.get("recommended_next_reads", []),
            max_results=8 if detail_level == "standard" else 6,
        ),
        "truncation": {
            "explain": explain.get("truncation", {}),
            "impact": impact.get("truncation", {}),
        },
        "warnings": _unique(
            [
                *explain.get("warnings", []),
                *impact.get("warnings", []),
                *explanation.get("warnings", []),
            ]
        ),
    }
    return strip_source_snippets(_strip_raw_properties(audit))


def _node_summary(node: dict[str, Any], explanation: dict[str, Any]) -> dict[str, Any]:
    symbol = (explanation.get("symbols") or [{}])[0]
    source = symbol if isinstance(symbol, dict) and symbol.get("id") else node
    keys = (
        "id",
        "kind",
        "label",
        "name",
        "qualname",
        "path",
        "package",
        "sub_package",
        "semantic_tier",
        "start_line",
        "end_line",
    )
    return {key: source[key] for key in keys if source.get(key) is not None}


def _audit_impact_summary(impact: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": impact.get("status"),
        "call_impact": _impact_section_counts(impact.get("call_impact", {})),
        "import_impact": _impact_section_counts(impact.get("import_impact", {})),
        "entrypoint_impact": _impact_section_counts(
            impact.get("entrypoint_impact", {})
        ),
        "resource_impact": _impact_section_counts(impact.get("resource_impact", {})),
        "confidence_summary": impact.get("confidence_summary", {}),
        "provenance_summary": impact.get("provenance_summary", {}),
    }


def _impact_section_counts(section: dict[str, Any]) -> dict[str, Any]:
    return {
        key: section.get(key)
        for key in (
            "node_count",
            "edge_count",
            "affected_symbols",
            "affected_modules",
            "entrypoints",
            "resources",
        )
        if section.get(key) is not None
    }


def _merge_recommended_reads(
    first: list[dict[str, str]],
    second: list[dict[str, str]],
    *,
    max_results: int,
) -> list[dict[str, str]]:
    merged: dict[str, str] = {}
    for item in [*first, *second]:
        path = item.get("path")
        reason = item.get("reason")
        if path and reason and path not in merged:
            merged[path] = reason
    return [
        {"path": path, "reason": reason}
        for path, reason in list(merged.items())[:max_results]
    ]


def _strip_raw_properties(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_raw_properties(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_raw_properties(item)
            for key, item in value.items()
            if key != "properties"
        }
    return value


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))


def _estimated_json_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _payload_budget_warnings(budget: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    thresholds = budget.get("thresholds", {})
    sections = budget.get("sections", {})
    candidates = {
        "graph_data": budget.get("graph_data_bytes", 0),
        "focus_index": sections.get("focus_index", 0),
        "audit_index": sections.get("audit_index", 0),
    }
    for section, estimated_bytes in candidates.items():
        threshold = thresholds.get(section)
        if threshold is None or estimated_bytes <= threshold:
            continue
        warnings.append(
            {
                "kind": "payload_budget_exceeded",
                "section": section,
                "estimated_bytes": estimated_bytes,
                "threshold_bytes": threshold,
                "message": (
                    f"{section} is {estimated_bytes} bytes, above the static "
                    f"workbench budget of {threshold} bytes."
                ),
            }
        )
    return warnings
