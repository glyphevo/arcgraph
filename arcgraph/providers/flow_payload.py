"""Compact flow responses; analysis records stay in the index, not the protocol."""

from __future__ import annotations

import json
from typing import Any

FLOW_PAYLOAD_BYTES = 64 * 1024
_NODE_FIELDS = ("id", "kind", "name", "qualname", "path", "start_line", "end_line")
_EDGE_FIELDS = ("source", "target", "kind", "confidence", "semantic_role")
_EVIDENCE_FIELDS = ("kind", "path", "start_line", "end_line", "line", "column")
_RECOVERY = (
    "For omitted traversal, increase max_depth (up to 6) or max_results within "
    "the server limit. For omitted presentation, query a narrower entrypoint. "
    "Use arcgraph_explain with a returned node id, detail_level=detailed and "
    "an explicit max_results for direct relations and evidence. "
    "No result establishes runtime completeness."
)


def _fields(value: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {key: value[key] for key in names if key in value}


def payload_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def compact_flow(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    for key in ("nodes", "entrypoints"):
        result[key] = [_fields(node, _NODE_FIELDS) for node in payload.get(key, [])]
    result["edges"] = []
    omitted_evidence = 0
    for edge in payload.get("edges", []):
        evidence = edge.get("evidence", [])
        shown = [_fields(item, _EVIDENCE_FIELDS) for item in evidence[:2]]
        omitted_evidence += max(0, len(evidence) - len(shown))
        result["edges"].append(
            {
                **_fields(edge, _EDGE_FIELDS),
                "resolution": _fields(
                    edge.get("resolution", {}), ("status", "strategy")
                ),
                "evidence": shown,
                "evidence_total": len(evidence),
            }
        )
    result["presentation"] = {
        "format": "compact_flow",
        "internal_properties": "omitted_by_design",
        "evidence_limit_per_edge": 2,
    }
    result["truncation"] = {
        **payload.get("truncation", {}),
        "scope": "graph_traversal_and_response_presentation",
        "response_truncated": omitted_evidence > 0,
        "truncated": bool(payload.get("truncation", {}).get("truncated"))
        or omitted_evidence > 0,
        "omitted_evidence_items": omitted_evidence,
        "omitted_nodes": 0,
        "omitted_edges": 0,
        "omitted_entrypoints": 0,
        "omitted_fields": [],
        "byte_budget": FLOW_PAYLOAD_BYTES,
        "recovery": _RECOVERY,
    }
    return bound_flow_payload(result)


def bound_flow_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Enforce the serialized budget, including the final MCP envelope.

    Remove whole records, never shorten identifiers or silently cut JSON. A
    retained edge always retains its endpoint nodes. Envelope overflow is
    explicit too (e.g. unusually long paths or diagnostic messages).
    """
    result = dict(payload)
    for key in ("nodes", "edges", "entrypoints"):
        result[key] = list(payload.get(key, []))
    truncation = dict(result.get("truncation", {}))
    truncation["omitted_fields"] = list(truncation.get("omitted_fields", []))
    result["truncation"] = truncation
    while payload_size(result) > FLOW_PAYLOAD_BYTES:
        truncation.update(truncated=True, response_truncated=True, budget_exceeded=True)
        if result["edges"]:
            result["edges"].pop()
            truncation["omitted_edges"] += 1
            needed = {n["id"] for n in result["entrypoints"]} | {
                e[key] for e in result["edges"] for key in ("source", "target")
            }
            kept = [n for n in result["nodes"] if n["id"] in needed]
            truncation["omitted_nodes"] += len(result["nodes"]) - len(kept)
            result["nodes"] = kept
            continue
        if result["nodes"]:
            result["nodes"].pop()
            truncation["omitted_nodes"] += 1
            continue
        if result["entrypoints"]:
            result["entrypoints"].pop()
            truncation["omitted_entrypoints"] += 1
            continue
        # Metadata is normally tiny. Do not allow a long query/path/warning
        # to bypass the response budget, and do not pass a false complete result.
        candidates = [
            k
            for k in result
            if k not in {"truncation", "nodes", "edges", "entrypoints", "status"}
        ]
        if not candidates:
            raise ValueError("Flow response budget is smaller than its limit metadata")
        key = max(candidates, key=lambda k: payload_size(result[k]))
        result.pop(key)
        truncation["omitted_fields"].append(key)
        result["status"] = "partial"
    return result
