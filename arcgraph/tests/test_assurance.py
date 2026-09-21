from __future__ import annotations

from arcgraph.core.assurance import build_assurance
from arcgraph.providers.risk_provider import _unknowns


def _current() -> dict[str, object]:
    return {
        "index_version": "test-index",
        "commit_sha": "a" * 40,
        "source_roots": ["src"],
        "file_count": 1,
        "freshness": {"status": "fresh", "stale": False},
        "capabilities": {
            "precision": "precision_available",
            "coverage": "available",
            "runtime_trace": "available",
        },
    }


def test_assurance_support_requires_target_level_evidence_in_every_channel() -> None:
    """The strongest posture needs evidence about the target, not an
    inventory of what the index ingested. Index-level capabilities alone
    cannot retire a per-target claim; with target-level evidence in every
    channel, bounded_support is still reachable."""

    index_level_only = build_assurance(
        current=_current(),
        target_resolutions=[{"status": "resolved", "resolved_ids": ["fn:pkg.run"]}],
        confidence_summaries=[{"by_confidence": {"confirmed": 3}}],
        coverage_statuses=["available"],
        analysis_limits=[{"traversal": {"truncated": False}}],
    )

    assert index_level_only["posture"] == "limited"
    unknowns = _unknowns(index_level_only)
    assert "exact_references" in {item["kind"] for item in unknowns}
    exact = next(item for item in unknowns if item["kind"] == "exact_references")
    # A gap the caller closes with one flag, not a standing limit: the block
    # below retires this same unknown, so any class asserting permanence
    # would contradict the assertion thirteen lines down.
    assert exact["class"] == "evidence_gap"
    assert exact["resolvable_by"] == ["verify_references"]

    with_target_evidence = build_assurance(
        current=_current(),
        target_resolutions=[{"status": "resolved", "resolved_ids": ["fn:pkg.run"]}],
        confidence_summaries=[{"by_confidence": {"confirmed": 3, "runtime-only": 1}}],
        coverage_statuses=["available"],
        precise_reference_statuses=["available"],
        analysis_limits=[{"traversal": {"truncated": False}}],
    )

    assert with_target_evidence["posture"] == "bounded_support"
    assert with_target_evidence["verification_required"] == []
    assert "exact_references" not in {
        item["kind"] for item in _unknowns(with_target_evidence)
    }


def test_assurance_distinguishes_analysis_and_response_truncation() -> None:
    resolved = [{"status": "resolved", "resolved_ids": ["fn:pkg.run"]}]

    analysis_limited = build_assurance(
        current=_current(),
        target_resolutions=resolved,
        analysis_limits=[{"traversal": {"truncated": True, "reasons": ["max_edges"]}}],
    )
    response_limited = build_assurance(
        current=_current(),
        target_resolutions=resolved,
        response_truncation={"truncated": True, "scope": "response_presentation"},
    )

    assert analysis_limited["posture"] == "do_not_rely"
    assert analysis_limited["limits"]["traversal_truncated"] is True
    analysis_unknown = next(
        item
        for item in _unknowns(analysis_limited)
        if item["kind"] == "analysis_truncated_scope"
    )
    assert analysis_unknown["class"] == "analysis_limit"
    assert analysis_unknown["resolvable_by"] == [
        "narrower_target",
        "direct_follow_up_queries",
    ]
    assert response_limited["posture"] == "limited"
    assert response_limited["limits"]["response_truncated"] is True
    truncated = next(
        item
        for item in _unknowns(response_limited)
        if item["kind"] == "truncated_scope"
    )
    assert truncated["class"] == "response_limit"
    assert truncated["resolvable_by"] == ["narrower_target", "higher_max_results"]

    combined = build_assurance(
        current=_current(),
        target_resolutions=resolved,
        analysis_limits=[{"unresolved_risks": {"truncated": True}}],
        response_truncation={"truncated": True},
    )
    combined_unknowns = {item["kind"]: item for item in _unknowns(combined)}
    assert combined_unknowns["analysis_truncated_scope"]["class"] == ("analysis_limit")
    assert combined_unknowns["truncated_scope"]["class"] == "response_limit"


def test_assurance_counts_unresolved_dynamic_and_truncated_boundaries() -> None:
    current = _current()
    current["warnings"] = [
        {"kind": "typescript_dynamic_import_unresolved"},
        {"kind": "typescript_config_dynamic"},
        {"kind": "typescript_config_dynamic"},
    ]

    assurance = build_assurance(
        current=current,
        target_resolutions=[{"status": "resolved", "resolved_ids": ["fn:pkg.run"]}],
        confidence_summaries=[{"by_confidence": {"confirmed": 2, "unresolved": 3}}],
        analysis_limits=[
            {
                "traversal": {"truncated": True},
                "unresolved_risks": {"truncated": True},
            }
        ],
        response_truncation={
            "truncated": True,
            "truncated_counts": {"affected_symbols": 4, "tests": 2},
        },
        unresolved_risk_count=5,
    )

    assert assurance["limits"] == {
        "traversal_truncated": True,
        "analysis_data_truncated": True,
        "response_truncated": True,
        "unresolved_risk_count": 5,
        "unresolved_edge_count": 3,
        "dynamic_gap_count": 3,
        "analysis_truncation_count": 2,
        "response_truncation_count": 6,
    }


def test_requested_reference_verification_outranks_global_precision() -> None:
    """A verification that was actually requested is authoritative: it may
    upgrade an AST-only index and must equally downgrade a precise one."""

    from arcgraph.core.assurance import build_assurance

    current = {
        "index_version": "assurance-reference-authority",
        "freshness": {"stale": False, "stale_files": []},
        "capabilities": {
            "precision": "precision_available",
            "coverage": "available",
            "runtime_trace": "available",
        },
        "warnings": [],
    }
    resolutions = [{"status": "resolved", "query": "pkg.api.hello"}]

    partial = build_assurance(
        current=current,
        target_resolutions=resolutions,
        coverage_statuses=["available"],
        precise_reference_statuses=["partial"],
    )
    assert partial["evidence"]["target_reference_verification"] == "partial"
    assert any("reference" in item.lower() for item in partial["verification_required"])
    assert partial["posture"] != "bounded_support"

    # Not requesting verification cannot establish the claim either: an
    # ingested precision artifact is an index-level input, and the non-claim
    # it would retire is about this target.
    unrequested = build_assurance(
        current=current,
        target_resolutions=resolutions,
        coverage_statuses=["available"],
    )
    assert unrequested["evidence"]["target_reference_verification"] == "not_requested"
    assert any(
        "reference" in item.lower() for item in unrequested["verification_required"]
    )

    # The upgrade direction still works: a target-level success lifts an
    # index whose global capability is only an AST fallback.
    ast_only = dict(current)
    ast_only["capabilities"] = {
        **current["capabilities"],
        "precision": "ast_fallback_only",
    }
    upgraded = build_assurance(
        current=ast_only,
        target_resolutions=resolutions,
        coverage_statuses=["available"],
        precise_reference_statuses=["available"],
    )
    assert not any(
        "reference" in item.lower() for item in upgraded["verification_required"]
    )


def test_target_resolutions_are_bounded_but_posture_uses_all_of_them() -> None:
    from arcgraph.core.assurance import build_assurance

    current = {
        "index_version": "assurance-bounded-resolutions",
        "freshness": {"stale": False, "stale_files": []},
        "capabilities": {"coverage": "available", "runtime_trace": "available"},
        "warnings": [],
    }
    resolutions = [{"status": "resolved", "query": f"t{index}"} for index in range(20)]
    # One unresolved target outside the returned slice still means the answer
    # is incomplete.
    resolutions[-1] = {"status": "unresolved", "query": "t19"}

    payload = build_assurance(
        current=current,
        target_resolutions=resolutions,
        coverage_statuses=["available"],
        max_target_resolutions=3,
    )

    assert len(payload["target_resolutions"]) == 3
    assert payload["target_resolution_summary"] == {
        "total": 20,
        "returned": 3,
        "omitted": 17,
    }
    assert payload["posture"] == "do_not_rely"


def test_unverified_targets_still_weaken_reference_evidence() -> None:
    """A skipped verification must contribute a status. An empty list reads
    as "not requested" and lets assurance fall back to the index capability,
    which would claim more than the request checked."""

    from arcgraph.core.assurance import build_assurance

    current = {
        "index_version": "budget-honesty",
        "freshness": {"stale": False, "stale_files": []},
        "capabilities": {
            "precision": "precision_available",
            "coverage": "available",
            "runtime_trace": "available",
        },
        "warnings": [],
    }
    resolutions = [{"status": "resolved", "query": "t"}]

    verified_only = build_assurance(
        current=current,
        target_resolutions=resolutions,
        coverage_statuses=["available"],
        precise_reference_statuses=["available"],
    )
    with_unverified = build_assurance(
        current=current,
        target_resolutions=resolutions,
        coverage_statuses=["available"],
        precise_reference_statuses=["available", "partial"],
    )

    assert verified_only["evidence"]["target_reference_verification"] == "available"
    assert with_unverified["evidence"]["target_reference_verification"] == "partial"
    assert any(
        "reference" in item.lower() for item in with_unverified["verification_required"]
    )


def test_coverage_evidence_is_target_level_not_index_level() -> None:
    """An ingested coverage artifact says a run happened, not that it
    exercised this target. A response must not claim coverage while its own
    test_gaps say the target has none."""

    from arcgraph.core.assurance import target_coverage_status

    # No gap for the target: the artifact speaks for it.
    assert target_coverage_status("available", False) == "available"
    # A gap for the target: the artifact cannot establish its coverage.
    assert target_coverage_status("available", True) == "partial"
    # No artifact at all stays unavailable either way.
    assert target_coverage_status("unavailable", True) == "unavailable"
    assert target_coverage_status("unavailable", False) == "unavailable"
