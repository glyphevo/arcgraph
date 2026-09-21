"""Central deterministic verdict precedence for Surgical Change Safety."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arcgraph.change.contracts import PlanningVerdict, VerificationVerdict


@dataclass(frozen=True, slots=True)
class VerdictFinding:
    code: str
    message: str
    severity: str = "blocking"
    category: str = "safety"
    details: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "category": self.category,
        }
        if self.details:
            payload["details"] = self.details
        return payload


_VERIFICATION_PRECEDENCE: tuple[tuple[set[str], VerificationVerdict], ...] = (
    (
        {
            "PLAN_NOT_APPROVED",
            "PLAN_REJECTED",
            "PLAN_SUPERSEDED",
            "PLAN_ARCHIVED",
            "PLAN_CONTENT_DIGEST_MISMATCH",
        },
        "PLAN_INVALID_OR_SUPERSEDED",
    ),
    (
        {
            "BASELINE_INDEX_INTEGRITY_MISMATCH",
            "BASELINE_SOURCE_UNAVAILABLE",
            "BASELINE_GIT_OBJECT_MISSING",
            "BASELINE_SOURCE_IDENTITY_MISMATCH",
            "CHANGE_STORE_CORRUPT",
        },
        "BASELINE_UNAVAILABLE",
    ),
    (
        {
            "CURRENT_CODE_IDENTITY_MISMATCH",
            "CURRENT_BUILD_CODE_IDENTITY_MISMATCH",
            "CURRENT_BUILD_STALE",
        },
        "INDEX_STALE",
    ),
    (
        {"CHANGE_SCOPE_EXCEEDED", "REVIEW_ONLY_SCOPE_CHANGED"},
        "CHANGE_SCOPE_EXCEEDED",
    ),
    (
        {"PROTECTED_SURFACE_CHANGED", "PROTECTED_SCOPE_CHANGED"},
        "PROTECTED_SURFACE_CHANGED",
    ),
    (
        {
            "UNRESOLVED_HIGH_RISK_DEPENDENCY",
            "GRAPH_DELTA_INCOMPLETE",
            "CHANGED_REGION_UNMAPPED",
            "CAPABILITY_UNKNOWN_HIGH_RISK",
            "NODE_IDENTITY_MOVE_REQUIRES_REVIEW",
            "SOURCE_NORMALIZATION_UNKNOWN",
        },
        "UNRESOLVED_HIGH_RISK_DEPENDENCY",
    ),
    (
        {"VERIFICATION_FAILED", "EVIDENCE_FAILED"},
        "VERIFICATION_FAILED",
    ),
    (
        {
            "EVIDENCE_MISSING",
            "EVIDENCE_STALE",
            "EVIDENCE_ATTESTATION_INSUFFICIENT",
            "TRUSTED_RUNNER_UNAVAILABLE",
        },
        "INSUFFICIENT_EVIDENCE",
    ),
)


def decide_planning_verdict(findings: list[VerdictFinding]) -> PlanningVerdict:
    codes = {finding.code for finding in findings if finding.severity == "blocking"}
    if "PLAN_INVALID_INTENT" in codes:
        return "PLAN_INVALID_INTENT"
    if "TARGET_UNRESOLVED" in codes:
        return "PLAN_BLOCKED_UNRESOLVED_TARGET"
    if "INDEX_STALE" in codes or "BASELINE_UNAVAILABLE" in codes:
        return "PLAN_BLOCKED_STALE_INDEX"
    if "GRAPH_DELTA_TRUNCATED" in codes:
        return "PLAN_BLOCKED_TRUNCATED_IMPACT"
    if "CAPABILITY_UNKNOWN" in codes:
        return "PLAN_BLOCKED_CAPABILITY_GAP"
    if "EVIDENCE_INSUFFICIENT" in codes:
        return "PLAN_BLOCKED_INSUFFICIENT_EVIDENCE"
    if codes:
        # Planning findings are an extensible boundary.  A newly introduced
        # blocking code must never fall through to a ready verdict merely
        # because this precedence table has not been updated yet.
        return "PLAN_INVALID_INTENT"
    if any(finding.severity in {"warning", "manual_review"} for finding in findings):
        return "PLAN_READY_WITH_KNOWN_RISKS"
    return "PLAN_READY"


def decide_verification_verdict(findings: list[VerdictFinding]) -> VerificationVerdict:
    """Return one stable verdict without duplicating policy in callers."""

    blocking_codes = {
        finding.code for finding in findings if finding.severity == "blocking"
    }
    for codes, verdict in _VERIFICATION_PRECEDENCE:
        if blocking_codes & codes:
            return verdict
    if blocking_codes:
        return "VERIFICATION_FAILED"
    if any(finding.severity in {"warning", "manual_review"} for finding in findings):
        return "SAFE_WITH_KNOWN_RISKS"
    return "SAFE_TO_PROCEED"


def verdict_is_blocking(verdict: VerificationVerdict | PlanningVerdict) -> bool:
    return verdict not in {
        "SAFE_TO_PROCEED",
        "SAFE_WITH_KNOWN_RISKS",
        "PLAN_READY",
        "PLAN_READY_WITH_KNOWN_RISKS",
    }
