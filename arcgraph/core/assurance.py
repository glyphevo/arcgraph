"""Structured, machine-readable limits on what a read result can support."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable


def build_assurance(
    *,
    current: dict[str, Any],
    target_resolutions: Iterable[dict[str, Any]] = (),
    confidence_summaries: Iterable[dict[str, Any]] = (),
    coverage_statuses: Iterable[str] = (),
    precise_reference_statuses: Iterable[str] = (),
    analysis_limits: Iterable[dict[str, Any]] = (),
    response_truncation: dict[str, Any] | None = None,
    unresolved_risk_count: int = 0,
    max_target_resolutions: int | None = None,
) -> dict[str, Any]:
    """Build assurance from structural signals, never rendered warning text.

    ``max_target_resolutions`` bounds only what is returned. Posture and every
    derived claim are computed from the full set, because one unresolved
    target outside the returned slice still means the answer is incomplete.
    """

    resolutions = [dict(item) for item in target_resolutions if item]
    confidence = _aggregate_confidence(confidence_summaries)
    limits = [dict(item) for item in analysis_limits if item]
    freshness = current.get("freshness", {})
    capabilities = current.get("capabilities", {})
    coverage_values = [value for value in coverage_statuses if value]
    coverage = _weakest_status(
        coverage_values or [str(capabilities.get("coverage", "unavailable"))]
    )
    precision = str(
        capabilities.get("precision")
        or capabilities.get("precise_references")
        or "unavailable"
    )
    reference_values = [value for value in precise_reference_statuses if value]
    target_reference_verification = _weakest_status(reference_values)
    precision_available = reference_evidence_available(
        precision, target_reference_verification
    )
    runtime_trace = str(capabilities.get("runtime_trace", "unavailable"))
    # Same rule as references: an ingested trace says the index has runtime
    # evidence, not that the trace exercised this target. The target-level
    # signal is already here — its impact carrying runtime-derived edges.
    # A target that was exercised but whose every edge is also statically
    # confirmed reads as unestablished; that error is in the safe direction.
    runtime_established = (
        evidence_available(runtime_trace) and confidence.get("runtime-only", 0) > 0
    )
    traversal_truncated = any(
        item.get("traversal", {}).get("truncated") is True for item in limits
    )
    analysis_data_truncated = any(
        any(
            isinstance(section, dict) and section.get("truncated") is True
            for name, section in item.items()
            if name != "traversal"
        )
        for item in limits
    )
    response_truncated = bool((response_truncation or {}).get("truncated"))
    analysis_truncation_count = sum(
        1
        for item in limits
        for section in item.values()
        if isinstance(section, dict) and section.get("truncated") is True
    )
    response_truncation_count = sum(
        int(value)
        for value in (response_truncation or {}).get("truncated_counts", {}).values()
        if isinstance(value, int) and value > 0
    )
    if response_truncated and response_truncation_count == 0:
        response_truncation_count = 1
    unresolved_edge_count = int(confidence.get("unresolved", 0) or 0)
    dynamic_gap_count = _dynamic_gap_count(current.get("warnings", []))
    returned_resolutions = (
        resolutions
        if max_target_resolutions is None
        else resolutions[: max(0, max_target_resolutions)]
    )
    unresolved_resolutions = not resolutions or any(
        item.get("status") in {"unresolved", "ambiguous"} for item in resolutions
    )
    stale = freshness.get("stale") is True or freshness.get("status") == "stale"
    weak_evidence = (
        any(
            confidence.get(key, 0) > 0
            for key in ("inferred", "heuristic", "unresolved")
        )
        or unresolved_risk_count > 0
    )
    missing_evidence = (
        not precision_available
        or not evidence_available(coverage)
        or not runtime_established
    )

    if (
        stale
        or unresolved_resolutions
        or traversal_truncated
        or analysis_data_truncated
    ):
        posture = "do_not_rely"
    elif response_truncated or weak_evidence or missing_evidence:
        posture = "limited"
    else:
        posture = "bounded_support"

    non_claims: list[str] = [
        "No claim covers code outside indexed roots.",
        "Zero unresolved edges does not prove completeness.",
    ]
    verification_required: list[str] = []
    if unresolved_resolutions:
        non_claims.append(
            "No target-specific conclusion is supported for unresolved or ambiguous targets."
        )
        verification_required.append(
            "Select an exact qualname or typed node id and rerun the read."
        )
    if stale:
        non_claims.append(
            "The result does not describe the current working tree completely."
        )
        verification_required.append("Refresh the index before relying on the result.")
    if traversal_truncated or analysis_data_truncated:
        non_claims.append(
            "The analysis does not establish the full reachable blast radius."
        )
        verification_required.append(
            "Rerun with adequate analysis guardrails and inspect the untruncated result."
        )
    if response_truncated:
        non_claims.append(RESPONSE_TRUNCATION_NON_CLAIM)
        verification_required.append(RESPONSE_TRUNCATION_VERIFICATION)
    if not precision_available:
        non_claims.append("Exact reference completeness is not established.")
        verification_required.append(
            "Verify references manually or with precise evidence."
        )
    if not evidence_available(coverage):
        non_claims.append("Test coverage and test sufficiency are not established.")
        verification_required.append(
            "Run or import coverage before claiming test sufficiency."
        )
    if not runtime_established:
        non_claims.append(
            "Dynamic and runtime registration completeness are not established."
        )
        verification_required.append("Validate dynamic behavior with runtime evidence.")

    return {
        "posture": posture,
        "indexed_scope": {
            "index_version": current.get("index_version"),
            "commit_sha": current.get("commit_sha"),
            "source_roots": list(current.get("source_roots", [])),
            "file_count": int(current.get("file_count", 0) or 0),
            "freshness": freshness,
        },
        "target_resolutions": returned_resolutions,
        "target_resolution_summary": {
            "total": len(resolutions),
            "returned": len(returned_resolutions),
            "omitted": len(resolutions) - len(returned_resolutions),
        },
        "evidence": {
            "impact_edges_by_confidence": confidence,
            "precision": precision,
            "target_reference_verification": target_reference_verification,
            "coverage": coverage,
            "runtime_trace": runtime_trace,
        },
        "limits": {
            "traversal_truncated": traversal_truncated,
            "analysis_data_truncated": analysis_data_truncated,
            "response_truncated": response_truncated,
            "unresolved_risk_count": unresolved_risk_count,
            "unresolved_edge_count": unresolved_edge_count,
            "dynamic_gap_count": dynamic_gap_count,
            "analysis_truncation_count": analysis_truncation_count,
            "response_truncation_count": response_truncation_count,
        },
        "completeness": {
            "status": (
                "not_established" if posture == "do_not_rely" else "bounded_static_only"
            ),
            "scope": "Only indexed sources and reported evidence are covered.",
        },
        "non_claims": _unique(non_claims),
        "verification_required": _unique(verification_required),
    }


def _aggregate_confidence(
    summaries: Iterable[dict[str, Any]],
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for summary in summaries:
        by_confidence = summary.get("by_confidence", {})
        if isinstance(by_confidence, dict):
            for key, value in by_confidence.items():
                if isinstance(value, int):
                    counts[str(key)] += value
    return {
        key: counts.get(key, 0)
        for key in ("confirmed", "inferred", "runtime-only", "heuristic", "unresolved")
    }


RESPONSE_TRUNCATION_NON_CLAIM = (
    "The response is a presentation subset of the analyzed result."
)
RESPONSE_TRUNCATION_VERIFICATION = (
    "Request a larger result limit or inspect the raw result."
)


def apply_response_truncation(
    assurance: dict[str, Any], truncation: dict[str, Any]
) -> dict[str, Any]:
    """Re-apply the response-truncation rule to an assurance already built.

    Assurance reads response truncation, so anything that truncates after it
    is built leaves it stating the opposite of the payload it travels in.
    The rule lives here rather than being restated by the late truncator,
    which would be a second copy that drifts. It is monotone: it can only
    weaken a posture, never strengthen one.
    """

    if not bool((truncation or {}).get("truncated")):
        return assurance
    limits = dict(assurance.get("limits", {}))
    counted = sum(
        int(value)
        for value in (truncation or {}).get("truncated_counts", {}).values()
        if isinstance(value, int) and value > 0
    )
    # An assurance already marked truncated still has to absorb the counts
    # added after it was built: returning early there left the payload's own
    # total and the number beside it disagreeing.
    already_truncated = limits.get("response_truncated") is True
    recorded = int(limits.get("response_truncation_count", 0) or 0)
    if already_truncated and counted <= recorded:
        return assurance

    updated = dict(assurance)
    limits["response_truncated"] = True
    limits["response_truncation_count"] = max(recorded, counted or 1)
    updated["limits"] = limits
    if updated.get("posture") == "bounded_support":
        updated["posture"] = "limited"
    for key, value in (
        ("non_claims", RESPONSE_TRUNCATION_NON_CLAIM),
        ("verification_required", RESPONSE_TRUNCATION_VERIFICATION),
    ):
        entries = list(updated.get(key, []))
        if value not in entries:
            entries.append(value)
        updated[key] = entries
    return updated


def target_coverage_status(index_status: Any, has_coverage_gap: bool) -> str:
    """Return coverage evidence for one target, not for the index.

    An ingested coverage artifact says a coverage run happened; it says
    nothing about whether it exercised THIS target. When the same analysis
    already found the target has no coverage edge, the artifact cannot
    establish coverage for it, and the response must not claim otherwise
    while its own test_gaps say the opposite.
    """

    status = str(index_status or "unavailable")
    if status == "unavailable" or not has_coverage_gap:
        return status
    return "partial"


def reference_evidence_available(
    precision: Any, target_reference_verification: Any
) -> bool:
    """Return whether exact-reference completeness is established.

    The non-claim this gates — "exact reference completeness is not
    established" — is about the requested target, so only a target-level
    verification can retire it. An ingested precision artifact says the
    index has one; it does not say the artifact covers this symbol, and an
    empty or partial artifact yields the same capability string. Index-level
    precision therefore never establishes the claim on its own: it can only
    be the reason a verification succeeds when one is run.
    """

    del precision
    return evidence_available(target_reference_verification)


def evidence_available(value: Any) -> bool:
    """Return whether an assurance evidence status establishes availability."""

    return value in {
        "available",
        "precision_available",
        "full",
        "precise",
        "complete",
    }


def _weakest_status(values: list[str]) -> str:
    if not values:
        return "not_requested"
    order = {"unavailable": 0, "partial": 1, "available": 2}
    return min(values, key=lambda value: order.get(value, 0))


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _dynamic_gap_count(warnings: Any) -> int:
    """Count observed dynamic-analysis gap records, not distinct kind labels."""

    if not isinstance(warnings, list):
        return 0
    total = 0
    for item in warnings:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if not isinstance(kind, str) or "dynamic" not in kind.lower():
            continue
        count = item.get("count")
        total += count if isinstance(count, int) and count > 0 else 1
    return total
