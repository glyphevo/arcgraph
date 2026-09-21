"""Unresolved callsite classification for ArcGraph diagnostics.

Provides machine-readable classification of unresolved call diagnostics into
agent-actionable categories.  This module lives in ``core/`` because it is a
pure data-classification function with no dependency on report rendering or
CLI/MCP interfaces.

Originally extracted from ``ArcGraph.interfaces.reports``.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# ---------------------------------------------------------------------------
# Category definitions
# ---------------------------------------------------------------------------

_UNRESOLVED_CATEGORY_MEANINGS: tuple[tuple[str, str], ...] = (
    (
        "static_candidate",
        "Likely static-resolution work remains; candidates exist or the expression is a normal attribute/name call.",
    ),
    (
        "true_dynamic_call",
        "Dynamic dispatch, import indirection, plugin lookup, or unknown receiver should stay visible.",
    ),
    (
        "mock_or_assertion",
        "Explicit mock/assertion helper calls are unresolved but lower release risk.",
    ),
    (
        "external_service_boundary",
        "External client/session/SDK/service-boundary calls should not be confirmed without evidence.",
    ),
    (
        "missing_type_context",
        "Receiver or type context is explicitly missing; SCIP/Pyright or analyzer improvements may resolve it.",
    ),
    (
        "framework_magic",
        "Framework registration, decorator, or dependency-injection magic should stay visible unless adapter evidence confirms it.",
    ),
    (
        "generated_or_reflection",
        "Generated-code or reflection-driven calls are intentionally hard to resolve statically.",
    ),
)

UNRESOLVED_CATEGORIES: tuple[str, ...] = tuple(
    category for category, _meaning in _UNRESOLVED_CATEGORY_MEANINGS
)

_CATEGORY_METADATA: dict[str, dict[str, Any]] = {
    "static_candidate": {
        "risk_level": "high",
        "release_blocking": True,
        "suggested_next_step": "Inspect the source and improve static resolution or add precise evidence.",
    },
    "missing_type_context": {
        "risk_level": "high",
        "release_blocking": True,
        "suggested_next_step": "Generate SCIP/Pyright evidence or add annotations so receiver/type context is available.",
    },
    "external_service_boundary": {
        "risk_level": "medium",
        "release_blocking": False,
        "suggested_next_step": "Confirm behavior with coverage, runtime trace, or source review before editing this boundary.",
    },
    "framework_magic": {
        "risk_level": "medium",
        "release_blocking": False,
        "suggested_next_step": "Check framework adapter coverage or inspect registration/decorator source manually.",
    },
    "generated_or_reflection": {
        "risk_level": "medium",
        "release_blocking": False,
        "suggested_next_step": "Review generated/reflection source or provide runtime evidence for the dynamic path.",
    },
    "true_dynamic_call": {
        "risk_level": "low",
        "release_blocking": False,
        "suggested_next_step": "Keep visible; use runtime evidence only if this dynamic call is relevant to the change.",
    },
    "mock_or_assertion": {
        "risk_level": "low",
        "release_blocking": False,
        "suggested_next_step": "Usually safe to leave unresolved unless the test helper behavior is the edit target.",
    },
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_unresolved_records(unresolved: dict[str, Any]) -> dict[str, Any]:
    """Return a machine-readable classification for unresolved diagnostics.

    *unresolved* is the full payload returned by ``QueryEngine.unresolved()``.
    The returned dict contains ``category_counts``, ``categories``,
    ``failed_strategy_counts``, and ``records`` (per-diagnostic classifications).
    """
    records = unresolved.get("unresolved") or unresolved.get("items") or []
    classifications = [
        {**_unresolved_record_location(record), **classify_unresolved_record(record)}
        for record in records
        if isinstance(record, dict)
    ]
    category_counts = Counter(item["category"] for item in classifications)
    risk_counts = Counter(item["risk_level"] for item in classifications)
    release_blocking_count = sum(
        1 for item in classifications if item["release_blocking"]
    )
    failed_strategy_counts = unresolved.get("summary", {}).get("by_failed_strategy", {})
    if not isinstance(failed_strategy_counts, dict):
        failed_strategy_counts = {}
    categories = {
        category: {
            "count": int(category_counts.get(category, 0)),
            "meaning": meaning,
            "risk_level": _CATEGORY_METADATA[category]["risk_level"],
            "release_blocking": _CATEGORY_METADATA[category]["release_blocking"],
            "suggested_next_step": _CATEGORY_METADATA[category]["suggested_next_step"],
        }
        for category, meaning in _UNRESOLVED_CATEGORY_MEANINGS
    }
    return {
        "status": unresolved.get("status", "unknown"),
        "query": unresolved.get("query"),
        "index_version": unresolved.get("index_version"),
        "freshness": unresolved.get("freshness", {}),
        "total_unresolved": unresolved.get("summary", {}).get("total", 0),
        "classified_records": len(classifications),
        "limit": unresolved.get("summary", {}).get("limit", len(classifications)),
        "category_counts": {
            category: int(category_counts.get(category, 0))
            for category, _meaning in _UNRESOLVED_CATEGORY_MEANINGS
        },
        "risk_counts": dict(sorted(risk_counts.items())),
        "release_blocking_count": release_blocking_count,
        "recommended_actions": _recommended_actions(category_counts),
        "categories": categories,
        "failed_strategy_counts": dict(sorted(failed_strategy_counts.items())),
        "records": classifications,
    }


def annotate_unresolved_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return *record* with compact triage fields attached."""

    return {**record, **classify_unresolved_record(record)}


def classify_unresolved_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return the compact triage classification for one unresolved diagnostic."""

    category, reason = _classify_unresolved_record(record)
    metadata = _CATEGORY_METADATA[category]
    properties = record.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    return {
        "category": category,
        "risk_level": metadata["risk_level"],
        "release_blocking": metadata["release_blocking"],
        "classification_reason": reason,
        "suggested_next_step": metadata["suggested_next_step"],
        "raw_expression": str(properties.get("raw_expression") or "<unknown>"),
        "failed_strategy": str(properties.get("failed_strategy") or "unknown"),
        "candidate_count": _optional_int(properties.get("candidate_count")),
    }


def compact_unresolved_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return one bounded diagnostic with the shared triage classification.

    Context and Change Preflight expose the same unresolved facts.  Keeping the
    projection here prevents the two agent surfaces from silently disagreeing
    about the diagnostic message or release-triage fields.
    """

    properties = record.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    classification: dict[str, Any] = {
        key: record.get(key)
        for key in (
            "category",
            "risk_level",
            "release_blocking",
            "classification_reason",
            "suggested_next_step",
        )
        if key in record
    } or classify_unresolved_record(record)
    message, message_truncated = _bounded_text(record.get("message"), 320)
    expression, expression_truncated = _bounded_text(
        properties.get("raw_expression"), 512
    )
    compact = {
        "diagnostic_id": record.get("diagnostic_id"),
        "kind": record.get("kind") or record.get("diagnostic_kind"),
        "severity": record.get("severity"),
        "path": record.get("path") or properties.get("path"),
        "start_line": record.get("start_line") or properties.get("line"),
        "message": message,
        "message_truncated": message_truncated or None,
        "raw_expression": expression,
        "raw_expression_truncated": (
            expression_truncated or properties.get("raw_expression_truncated") or None
        ),
        "failed_strategy": properties.get("failed_strategy"),
        "source_scope": properties.get("source_scope"),
        "inclusion_scope": record.get("inclusion_scope"),
        "category": classification.get("category"),
        "risk_level": classification.get("risk_level"),
        "release_blocking": classification.get("release_blocking"),
        "classification_reason": classification.get("classification_reason"),
        "suggested_next_step": classification.get("suggested_next_step"),
    }
    return {key: value for key, value in compact.items() if value is not None}


def _bounded_text(value: Any, limit: int) -> tuple[Any, bool]:
    if not isinstance(value, str) or len(value) <= limit:
        return value, False
    return value[:limit], True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _classify_unresolved_record(record: dict[str, Any]) -> tuple[str, str]:
    properties = record.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    raw_expression = str(properties.get("raw_expression") or "<unknown>")
    failed_strategy = str(properties.get("failed_strategy") or "unknown")
    path = str(record.get("path") or properties.get("path") or "")
    raw_lower = raw_expression.lower()
    if (
        failed_strategy == "union_receiver"
        and properties.get("semantic_reason") == "ambiguous_union_receiver"
    ):
        return "true_dynamic_call", "analyzer-recorded union receiver alternatives"
    if _is_mock_or_assertion(raw_lower):
        return "mock_or_assertion", "explicit mock/assertion helper expression"
    if _is_framework_magic(raw_lower):
        return "framework_magic", "framework registration or decorator pattern"
    elif _is_external_service_boundary(raw_lower):
        return "external_service_boundary", "external client/session boundary"
    if _is_missing_type_context(properties, failed_strategy):
        return "missing_type_context", "explicitly missing receiver/type context"
    if _is_generated_or_reflection(path, raw_lower):
        return "generated_or_reflection", "generated-code or reflection pattern"
    elif _is_true_dynamic_call(properties, failed_strategy):
        return "true_dynamic_call", "analyzer-recorded dynamic dispatch"
    return "static_candidate", "ordinary call expression remains unresolved"


def _recommended_actions(category_counts: Counter[str]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for category, _meaning in _UNRESOLVED_CATEGORY_MEANINGS:
        count = int(category_counts.get(category, 0))
        if count <= 0:
            continue
        metadata = _CATEGORY_METADATA[category]
        actions.append(
            {
                "category": category,
                "count": count,
                "risk_level": metadata["risk_level"],
                "release_blocking": metadata["release_blocking"],
                "suggested_next_step": metadata["suggested_next_step"],
            }
        )
    return actions


def _unresolved_record_location(record: dict[str, Any]) -> dict[str, str]:
    properties = record.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    path = str(record.get("path") or properties.get("path") or "unknown")
    line = record.get("start_line") or properties.get("line")
    return {"location": f"{path}:{line}" if line else path}


def _is_mock_or_assertion(raw_lower: str) -> bool:
    return any(
        token in raw_lower
        for token in (
            "assert_called",
            "assert_any_call",
            "assert_has_calls",
            "mock.",
            "mocker.",
            "monkeypatch.",
        )
    )


def _is_external_service_boundary(raw_lower: str) -> bool:
    if raw_lower in {"async_session", "session_maker", "from_pretrained"}:
        return True
    boundary_terms = (
        ".execute",
        ".fetch",
        ".fetchall",
        ".fetchone",
        ".post",
        ".get",
        ".put",
        ".delete",
        ".publish",
        ".send",
        ".query",
        ".commit",
        ".rollback",
        ".connect",
        ".basic_publish",
    )
    receiver_terms = (
        "broker",
        "channel",
        "client",
        "session",
        "redis",
        "db",
        "database",
        "connection",
        "cursor",
        "http",
        "api",
        "sdk",
        "queue",
        "producer",
        "consumer",
        "session_maker",
    )
    return any(term in raw_lower for term in boundary_terms) and any(
        term in raw_lower for term in receiver_terms
    )


def _is_missing_type_context(properties: dict[str, Any], failed_strategy: str) -> bool:
    if failed_strategy in {
        "missing_type_context",
        "receiver_type_unknown",
        "receiver_type_unresolved",
        "type_context_missing",
        "type_resolution_unresolved",
    }:
        return True
    for key in (
        "missing_type_context",
        "receiver_type_missing",
        "type_context_missing",
    ):
        if properties.get(key) is True:
            return True
    receiver_status = str(properties.get("receiver_type_status") or "").lower()
    return receiver_status in {"missing", "unknown", "unresolved"}


def _is_framework_magic(raw_lower: str) -> bool:
    framework_names = {
        "apirouter",
        "depends",
        "fastapi",
        "celery",
        "shared_task",
        "django",
        "typer",
        "click",
        "blueprint",
    }
    if raw_lower in framework_names:
        return True
    decorator_suffixes = (
        ".route",
        ".get",
        ".post",
        ".put",
        ".patch",
        ".delete",
        ".websocket",
        ".command",
        ".task",
        ".add_api_route",
        ".include_router",
        ".autodiscover_tasks",
        ".add_command",
    )
    framework_receivers = (
        "router",
        "app",
        "api",
        "blueprint",
        "celery",
        "cli",
        "typer",
        "click",
    )
    if any(raw_lower.endswith(suffix) for suffix in decorator_suffixes):
        return any(receiver in raw_lower for receiver in framework_receivers)
    return any(
        token in raw_lower
        for token in (
            "dependency",
            "route_handler",
            "viewset",
            "urlpattern",
            "fixture",
        )
    )


def _is_generated_or_reflection(path: str, raw_lower: str) -> bool:
    normalized_path = path.replace("\\", "/").lower()
    if any(
        token in normalized_path
        for token in (
            "/generated/",
            "/gen/",
            "generated_",
            ".generated.",
            "/migrations/",
        )
    ):
        return True
    return any(
        token in raw_lower
        for token in (
            "getattr",
            "setattr",
            "hasattr",
            "__import__",
            "import_module",
            "globals()",
            "locals()",
            "eval(",
            "exec(",
        )
    )


def _is_true_dynamic_call(properties: dict[str, Any], failed_strategy: str) -> bool:
    if properties.get("callee_binding_kind") in {
        "parameter",
        "assignment",
        "annotated_assignment",
        "for_target",
        "with_as",
        "except_as",
        "nonlocal",
    }:
        return True
    return failed_strategy in {
        "dynamic_call",
        "dynamic_dispatch",
        "runtime_dynamic_call",
    }


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None
