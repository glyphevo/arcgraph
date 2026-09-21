"""TypeRef summary metrics for Python semantic analysis."""

from __future__ import annotations

from collections import Counter
from typing import Any

from arcgraph.core.schemas import Node


def collect_type_metrics(nodes: list[Node]) -> dict[str, Any]:
    type_ref_total = 0
    type_scope_ids: set[str] = set()
    resolved_total = 0
    unresolved_total = 0
    diagnostic_total = 0
    strategy_counts: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()
    origin_counts: Counter[str] = Counter()
    diagnostic_counts: Counter[str] = Counter()

    for node in sorted(nodes, key=lambda item: item.id):
        type_refs = node.properties.get("type_refs", [])
        if isinstance(type_refs, list):
            for type_ref in type_refs:
                if not isinstance(type_ref, dict):
                    continue
                name = type_ref.get("name")
                strategy = type_ref.get("strategy")
                if not isinstance(name, str) or not isinstance(strategy, str):
                    continue
                type_ref_total += 1
                type_scope_ids.add(str(type_ref.get("scope_id") or node.id))
                strategy_counts[strategy] += 1
                confidence = type_ref.get("confidence")
                if isinstance(confidence, str) and confidence:
                    confidence_counts[confidence] += 1
                origin = type_ref.get("origin")
                if isinstance(origin, str) and origin:
                    origin_counts[origin] += 1
                if isinstance(type_ref.get("type_id"), str):
                    resolved_total += 1
                else:
                    unresolved_total += 1

        diagnostics = node.properties.get("type_diagnostics", [])
        if isinstance(diagnostics, list):
            for diagnostic in diagnostics:
                if not isinstance(diagnostic, dict):
                    continue
                kind = diagnostic.get("kind")
                if not isinstance(kind, str) or not kind:
                    continue
                diagnostic_total += 1
                diagnostic_counts[kind] += 1

    return {
        "type_ref_total": type_ref_total,
        "type_scope_total": len(type_scope_ids),
        "resolved_type_ref_total": resolved_total,
        "unresolved_type_ref_total": unresolved_total,
        "type_diagnostic_total": diagnostic_total,
        "by_strategy": dict(sorted(strategy_counts.items())),
        "by_confidence": dict(sorted(confidence_counts.items())),
        "by_origin": dict(sorted(origin_counts.items())),
        "by_diagnostic_kind": dict(sorted(diagnostic_counts.items())),
    }
