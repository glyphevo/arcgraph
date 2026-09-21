from __future__ import annotations

import pytest

from arcgraph.change.contracts import (
    ChangedRegion,
    ProtectedSurface,
    ScopeItem,
    VerificationEvidence,
)
from arcgraph.change.errors import ChangePlanStateError, TrustedRunnerError
from arcgraph.change.verifier import (
    ChangeVerifier,
    _scope_matches,
    _surface_is_explicitly_authorized,
)
from arcgraph.tests.change_safety_helpers import (
    activated_approved_plan,
    current_code_identity,
    git,
    write_build,
)


def _evidence(plan, code) -> VerificationEvidence:
    requirement = plan.verification_plan.requirements[0]
    return VerificationEvidence(
        repo_id="repo",
        evidence_id="evidence",
        verification_requirement_id=requirement.requirement_id,
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        plan_content_digest=plan.plan_content_digest,
        result="pass",
        current_code_identity=code,
        attestation_level="self_reported",
        producer_identity="local-test",
        runner_metadata={"command": "pytest -q"},
        artifact_digest="artifact",
    )


def test_service_verify_persists_safe_report_and_terminal_state(tmp_path) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    service.record_evidence(_evidence(plan, current_code_identity(repo, output)))

    report = service.verify(plan.plan_id, plan.revision, plan.plan_content_digest)

    assert report.verdict == "SAFE_TO_PROCEED"
    assert service.get_view(plan.plan_id).plan_status == "verified"
    assert (
        service.get_verification_report(plan.plan_id, report.verification_id).verdict
        == "SAFE_TO_PROCEED"
    )


def test_unapproved_revision_cannot_be_verified(tmp_path) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    # A fresh service fixture has an approved decision; use a missing decision
    # directly to assert the verifier boundary without mutating lifecycle data.
    with pytest.raises(ChangePlanStateError):
        ChangeVerifier(repo, output, repo_id="repo").verify(
            plan,
            None,
            evidence=[],
        )


def test_untracked_source_is_not_silently_safe(tmp_path) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    (repo / "src" / "new.py").write_text("VALUE = 1\n", encoding="utf-8")
    write_build(
        repo,
        output,
        index_version="current",
        commit_sha=git(repo, "rev-parse", "HEAD"),
    )

    report = service.verify(plan.plan_id, plan.revision, plan.plan_content_digest)

    assert report.verdict != "SAFE_TO_PROCEED"
    assert service.get_view(plan.plan_id).plan_status == "failed"
    assert any(
        finding["code"] in {"CHANGED_REGION_UNMAPPED", "CHANGE_SCOPE_EXCEEDED"}
        for finding in report.findings
    )


def test_symbol_scope_does_not_authorize_another_protected_symbol_in_its_file() -> None:
    surface = ProtectedSurface(
        repo_id="repo",
        surface_id="surface-route",
        surface_kind="route",
        path="src/api.py",
        path_comparison_key="src/api.py",
        symbol_id="route:GET:/sensitive",
        detection_source="graph:route",
        capability="reliable_graph_fact",
        confidence="confirmed",
        detection_status="confirmed",
    )
    unrelated_symbol = ScopeItem(
        repo_id="repo",
        scope_item_id="scope-helper",
        path="src/api.py",
        path_comparison_key="src/api.py",
        symbol_id="fn:api.helper",
        scope_kind="symbol",
        policy="allowed",
        reason="planned helper edit",
        confidence="confirmed",
    )
    whole_path = unrelated_symbol.model_copy(
        update={
            "scope_item_id": "scope-path",
            "symbol_id": None,
            "scope_kind": "path",
            "reason": "explicit file target",
        }
    )

    assert not _surface_is_explicitly_authorized(surface, [unrelated_symbol])
    assert _surface_is_explicitly_authorized(surface, [whole_path])


def test_narrow_symbol_scope_does_not_authorize_a_whole_file_surface() -> None:
    surface = ProtectedSurface(
        repo_id="repo",
        surface_id="surface-file",
        surface_kind="security_boundary",
        path="src/api.py",
        path_comparison_key="src/api.py",
        detection_source="user_declaration",
        capability="user_declaration_required",
        confidence="confirmed",
        detection_status="declared",
    )
    narrow_scope = ScopeItem(
        repo_id="repo",
        scope_item_id="scope-helper",
        path="src/api.py",
        path_comparison_key="src/api.py",
        symbol_id="fn:api.helper",
        scope_kind="symbol",
        policy="allowed",
        reason="planned helper edit",
        confidence="confirmed",
    )

    assert not _surface_is_explicitly_authorized(surface, [narrow_scope])


def test_symbol_scope_tracks_a_mapped_rename_across_paths() -> None:
    scope = ScopeItem(
        repo_id="repo",
        scope_item_id="scope-symbol",
        path="src/old_name.py",
        path_comparison_key="src/old_name.py",
        symbol_id="fn:package.symbol",
        baseline_symbol_id="fn:package.symbol",
        scope_kind="symbol",
        policy="allowed",
        reason="planned symbol rename",
        confidence="confirmed",
    )
    renamed_region = ChangedRegion(
        repo_id="repo",
        path="src/new_name.py",
        path_comparison_key="src/new_name.py",
        baseline_symbol_id="fn:package.symbol",
        current_symbol_id="fn:package.symbol",
        classification="renamed",
        mapping_status="mapped",
    )

    assert _scope_matches(renamed_region, scope)


def test_path_scope_tracks_both_sides_of_a_rename() -> None:
    old_scope = ScopeItem(
        repo_id="repo",
        scope_item_id="scope-old-path",
        path="src/old_name.py",
        path_comparison_key="src/old_name.py",
        scope_kind="path",
        policy="protected",
        reason="protected original path",
        confidence="confirmed",
    )
    renamed_region = ChangedRegion(
        repo_id="repo",
        path="src/new_name.py",
        path_comparison_key="src/new_name.py",
        old_path="src/old_name.py",
        old_path_comparison_key="src/old_name.py",
        new_path="src/new_name.py",
        new_path_comparison_key="src/new_name.py",
        classification="renamed",
        mapping_status="mapped",
    )

    assert _scope_matches(renamed_region, old_scope)


def test_protected_scope_wins_when_the_same_scope_is_also_allowed(tmp_path) -> None:
    repo, output, _service, plan = activated_approved_plan(tmp_path)
    verifier = ChangeVerifier(repo, output, repo_id="repo")
    allowed = plan.allowed_edit_scope[0]
    protected = allowed.model_copy(
        update={"policy": "protected", "reason": "confirmed protected surface"}
    )
    revision = plan.model_copy(update={"protected_scope": [protected]})
    region = ChangedRegion(
        repo_id="repo",
        path=allowed.path,
        path_comparison_key=allowed.path_comparison_key,
        baseline_symbol_id=allowed.symbol_id,
        current_symbol_id=allowed.symbol_id,
        classification="modified",
        mapping_status="mapped",
    )

    codes = {
        finding.code for finding in verifier._scope_findings(revision, [], [region])
    }

    assert "PROTECTED_SCOPE_CHANGED" in codes


def test_non_implementation_regions_still_require_all_scope_authorizations(
    tmp_path,
) -> None:
    """A comment-only Git hunk remains a real edit for scope policy purposes."""

    repo, output, _service, plan = activated_approved_plan(tmp_path)
    verifier = ChangeVerifier(repo, output, repo_id="repo")
    review_scope = ScopeItem(
        repo_id="repo",
        scope_item_id="review-only-comment",
        path="src/app.py",
        path_comparison_key="src/app.py",
        scope_kind="path",
        policy="review_only",
        reason="read-only dependency",
        confidence="confirmed",
    )
    protected_scope = ScopeItem(
        repo_id="repo",
        scope_item_id="protected-comment",
        path="src/app.py",
        path_comparison_key="src/app.py",
        scope_kind="path",
        policy="protected",
        reason="protected dependency",
        confidence="confirmed",
    )
    revision = plan.model_copy(
        update={
            "allowed_edit_scope": [],
            "review_only_scope": [review_scope],
            "protected_scope": [protected_scope],
        }
    )
    region = ChangedRegion(
        repo_id="repo",
        path="src/app.py",
        path_comparison_key="src/app.py",
        baseline_symbol_id="fn:app.f",
        current_symbol_id="fn:app.f",
        classification="non_implementation",
        mapping_status="not_applicable",
    )

    codes = {
        finding.code for finding in verifier._scope_findings(revision, [], [region])
    }

    assert {
        "CHANGE_SCOPE_EXCEEDED",
        "REVIEW_ONLY_SCOPE_CHANGED",
        "PROTECTED_SCOPE_CHANGED",
    } <= codes


def test_missing_evidence_material_availability_is_not_treated_as_available(
    tmp_path,
) -> None:
    """A direct verifier caller cannot satisfy a required record by omission."""

    repo, output, _service, plan = activated_approved_plan(tmp_path)
    verifier = ChangeVerifier(repo, output, repo_id="repo")
    findings = verifier._evidence_findings(
        plan,
        current_code_identity(repo, output),
        [_evidence(plan, current_code_identity(repo, output))],
        {},
    )

    assert any(finding.code == "EVIDENCE_STALE" for finding in findings)


def test_evidence_from_another_code_state_is_rejected_as_stale(tmp_path) -> None:
    """Evidence must describe the code being verified, not an earlier one.

    A passing record whose current-code identity no longer matches is the
    shape a re-run after an edit produces, and accepting it would approve a
    change on the strength of a test that never saw it.
    """

    repo, output, _service, plan = activated_approved_plan(tmp_path)
    verifier = ChangeVerifier(repo, output, repo_id="repo")
    code = current_code_identity(repo, output)
    stale = _evidence(plan, code).model_copy(
        update={
            "current_code_identity": code.model_copy(
                update={"code_identity_digest": "a-different-code-identity-digest"}
            )
        }
    )

    findings = verifier._evidence_findings(
        plan, code, [stale], {stale.evidence_id: "available"}
    )

    assert any(finding.code == "EVIDENCE_STALE" for finding in findings)


def test_failing_evidence_is_rejected_rather_than_counted(tmp_path) -> None:
    repo, output, _service, plan = activated_approved_plan(tmp_path)
    verifier = ChangeVerifier(repo, output, repo_id="repo")
    code = current_code_identity(repo, output)
    failed = _evidence(plan, code).model_copy(update={"result": "fail"})

    findings = verifier._evidence_findings(
        plan, code, [failed], {failed.evidence_id: "available"}
    )

    assert any(finding.code == "EVIDENCE_FAILED" for finding in findings)


def test_inconclusive_evidence_does_not_satisfy_a_requirement(tmp_path) -> None:
    """A result that is neither pass nor fail is absence, not permission."""

    repo, output, _service, plan = activated_approved_plan(tmp_path)
    verifier = ChangeVerifier(repo, output, repo_id="repo")
    code = current_code_identity(repo, output)
    inconclusive = _evidence(plan, code).model_copy(update={"result": "skipped"})

    findings = verifier._evidence_findings(
        plan, code, [inconclusive], {inconclusive.evidence_id: "available"}
    )

    assert any(finding.code == "EVIDENCE_MISSING" for finding in findings)


def test_evidence_from_an_unusable_runner_is_rejected(tmp_path) -> None:
    """A registry that cannot vouch for the runner blocks the evidence.

    The registry is what separates a record produced by an approved runner
    from one a caller wrote by hand, so a failure to validate has to refuse
    rather than fall through to the result field.
    """

    repo, output, _service, plan = activated_approved_plan(tmp_path)
    verifier = ChangeVerifier(repo, output, repo_id="repo")
    code = current_code_identity(repo, output)
    item = _evidence(plan, code)

    class _RefusingRegistry:
        @staticmethod
        def validate(_item, _requirement) -> None:
            raise TrustedRunnerError("runner is not registered")

    verifier.trusted_runner_registry = _RefusingRegistry()

    findings = verifier._evidence_findings(
        plan, code, [item], {item.evidence_id: "available"}
    )

    assert any(finding.code == "TRUSTED_RUNNER_UNAVAILABLE" for finding in findings)
