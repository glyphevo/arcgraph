from __future__ import annotations

from arcgraph.change.verdicts import (
    VerdictFinding,
    decide_planning_verdict,
    decide_verification_verdict,
)


def test_unknown_blocking_planning_finding_fails_closed() -> None:
    verdict = decide_planning_verdict(
        [
            VerdictFinding(
                code="FUTURE_BLOCKING_POLICY",
                message="new policy has not been classified yet",
            )
        ]
    )

    assert verdict == "PLAN_INVALID_INTENT"


def test_verification_precedence_selects_the_highest_blocking_tier() -> None:
    verdict = decide_verification_verdict(
        [
            VerdictFinding(code="EVIDENCE_MISSING", message="missing"),
            VerdictFinding(code="CURRENT_BUILD_STALE", message="stale"),
            VerdictFinding(code="PLAN_NOT_APPROVED", message="not approved"),
        ]
    )

    assert verdict == "PLAN_INVALID_OR_SUPERSEDED"


def test_source_normalization_unknown_has_a_specific_high_risk_verdict() -> None:
    verdict = decide_verification_verdict(
        [
            VerdictFinding(
                code="SOURCE_NORMALIZATION_UNKNOWN",
                message="non-Python implementation projection",
            )
        ]
    )

    assert verdict == "UNRESOLVED_HIGH_RISK_DEPENDENCY"
