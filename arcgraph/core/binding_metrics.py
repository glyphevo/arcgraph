"""Binding summary metrics for Python semantic analysis."""

from __future__ import annotations

from collections import Counter
from typing import Any

from arcgraph.core.schemas import Node


def collect_binding_metrics(nodes: list[Node]) -> dict[str, Any]:
    binding_total = 0
    binding_scope_ids: set[str] = set()
    kind_counts: Counter[str] = Counter()
    scope_counts: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()
    diagnostic_counts: Counter[str] = Counter()
    diagnostic_total = 0
    static_only_total = 0
    shadowed_total = 0
    annotated_total = 0
    instance_attribute_total = 0

    for node in sorted(nodes, key=lambda item: item.id):
        bindings = node.properties.get("bindings", [])
        if isinstance(bindings, list):
            for binding in bindings:
                if not isinstance(binding, dict):
                    continue
                name = binding.get("name")
                kind = binding.get("kind")
                if not isinstance(name, str) or not isinstance(kind, str):
                    continue
                binding_total += 1
                binding_scope_ids.add(str(binding.get("scope_id") or node.id))
                kind_counts[kind] += 1
                scope_kind = binding.get("scope_kind")
                if isinstance(scope_kind, str) and scope_kind:
                    scope_counts[scope_kind] += 1
                confidence = binding.get("confidence")
                if isinstance(confidence, str) and confidence:
                    confidence_counts[confidence] += 1
                if binding.get("static_only") is True:
                    static_only_total += 1
                if isinstance(binding.get("shadows_binding_id"), str):
                    shadowed_total += 1
                if isinstance(binding.get("annotation"), str):
                    annotated_total += 1
                if kind == "instance_attribute":
                    instance_attribute_total += 1

        diagnostics = node.properties.get("binding_diagnostics", [])
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
        "binding_total": binding_total,
        "binding_scope_total": len(binding_scope_ids),
        "binding_diagnostic_total": diagnostic_total,
        "static_only_binding_total": static_only_total,
        "shadowed_binding_total": shadowed_total,
        "annotated_binding_total": annotated_total,
        "instance_attribute_binding_total": instance_attribute_total,
        "by_binding_kind": dict(sorted(kind_counts.items())),
        "by_scope_kind": dict(sorted(scope_counts.items())),
        "by_confidence": dict(sorted(confidence_counts.items())),
        "by_diagnostic_kind": dict(sorted(diagnostic_counts.items())),
    }
