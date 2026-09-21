"""Bound supplemental explain relations without changing callable semantics."""

from __future__ import annotations

import json
from typing import Any

RELATIONSHIP_BYTES = 32768


def compact_relations(raw: dict[str, Any], limit: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scope": raw["scope"],
        "incoming": [],
        "outgoing": [],
        "totals": {
            direction: len(raw[direction]) for direction in ("incoming", "outgoing")
        },
        "limits": {
            "per_direction": limit,
            "evidence_per_edge": 2,
            "byte_budget": RELATIONSHIP_BYTES,
        },
    }
    for direction in ("incoming", "outgoing"):
        for edge in raw[direction][:limit]:
            evidence = edge.get("evidence", [])
            payload[direction].append(
                {
                    key: edge[key]
                    for key in ("source", "target", "kind", "confidence")
                    if key in edge
                }
                | {
                    "evidence": [
                        {
                            key: item[key]
                            for key in ("kind", "path", "start_line", "end_line")
                            if key in item
                        }
                        for item in evidence[:2]
                    ],
                    "evidence_omitted": max(0, len(evidence) - 2),
                }
            )
    return bound_relations(payload)


def bound_relations(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove whole edge records; run again after MCP sanitization."""
    reasons = set(payload.get("truncation", {}).get("reasons", []))
    if any(
        payload["totals"][d] > payload["limits"]["per_direction"]
        for d in ("incoming", "outgoing")
    ):
        reasons.add("context_limit")
    if any(
        edge["evidence_omitted"]
        for d in ("incoming", "outgoing")
        for edge in payload[d]
    ):
        reasons.add("evidence_limit")
    while True:
        omitted = {
            d: payload["totals"][d] - len(payload[d]) for d in ("incoming", "outgoing")
        }
        payload["truncation"] = {
            "truncated": bool(reasons),
            "reasons": sorted(reasons),
            "omitted_edges": omitted,
            "omitted_evidence_on_retained_edges": sum(
                edge["evidence_omitted"]
                for d in ("incoming", "outgoing")
                for edge in payload[d]
            ),
        }
        if (
            len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
            <= RELATIONSHIP_BYTES
        ):
            return payload
        reasons.add("byte_budget")
        direction = max(("incoming", "outgoing"), key=lambda d: len(payload[d]))
        if not payload[direction]:
            raise ValueError("Relationship metadata exceeds fixed byte budget")
        payload[direction].pop()


def bound_explain_relations(payload: dict[str, Any]) -> dict[str, Any]:
    for explanation in payload.get("explanations", []):
        relations = explanation.get("relations")
        if relations is None:
            continue
        bound_relations(relations)
        if relations["truncation"]["truncated"]:
            for owner in (explanation, payload):
                truncation = owner.setdefault("truncation", {})
                truncation["truncated"] = True
                truncation["relations_truncated"] = True
                if not truncation.get("reason"):
                    truncation["reason"] = "relations_limits"
    return payload
