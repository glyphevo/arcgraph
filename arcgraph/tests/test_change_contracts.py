from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from arcgraph.change.contracts import (
    BaselineReference,
    BuildIdentity,
    ChangeIntent,
    ChangePlanRevision,
    ChangeTarget,
    CodeIdentity,
    ProtectedSurface,
    VerificationPlan,
    VerificationRequirement,
    WorkingTreeIdentity,
)
from arcgraph.change.errors import ChangeContractVersionUnsupported


def _revision(
    *, criteria: list[str] | None = None, target_order: list[str] | None = None
) -> ChangePlanRevision:
    repo_id = "test-repo"
    build = BuildIdentity(
        repo_id=repo_id,
        index_version="build-1",
        build_relative_path="builds/build-1",
        commit_sha="a" * 40,
        git_tree_sha="b" * 40,
        file_manifest_digest="files",
        summary_digest="summary",
        index_sqlite_sha256="sqlite",
        index_sqlite_size=3,
    )
    baseline = BaselineReference(
        repo_id=repo_id,
        baseline_id="baseline-1",
        build_identity=build,
        working_tree_identity=WorkingTreeIdentity(
            repo_id=repo_id,
            head_sha="a" * 40,
            clean=True,
            status_digest="clean",
        ),
        pin_id="pin-1",
        baseline_source_identity={"repo_id": repo_id, "commit_sha": "a" * 40},
    )
    targets = target_order or ["src/a.py", "src/b.py"]
    intent = ChangeIntent(
        repo_id=repo_id,
        intent_id="intent-1",
        task="Change only the declared target.",
        targets=[
            ChangeTarget(repo_id=repo_id, kind="path", value=target)
            for target in targets
        ],
        acceptance_criteria=criteria or ["first", "second"],
    )
    verification = VerificationPlan(
        repo_id=repo_id,
        plan_id="plan-1",
        plan_revision=1,
        input_digest="verification-input",
    )
    return ChangePlanRevision(
        repo_id=repo_id,
        plan_id="plan-1",
        revision=1,
        intent=intent,
        baseline=baseline,
        planning_verdict="PLAN_READY",
        verification_plan=verification,
        input_digest="plan-input",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code_identity_requirement", "best_effort"),
        ("freshness_requirement", "stale_build_allowed"),
    ],
)
def test_verification_requirement_strategy_fields_reject_unsupported_values(
    field: str,
    value: str,
) -> None:
    payload = {
        "repo_id": "repo",
        "requirement_id": "requirement",
        "category": "tests",
        "level": "required",
        "subject": "target",
        "reason": "safety",
        "acceptance_condition": "pass",
        field: value,
    }

    with pytest.raises(ValidationError):
        VerificationRequirement.model_validate(payload)


def test_change_contract_requires_exact_version_and_forbids_unknown_fields() -> None:
    with pytest.raises(ChangeContractVersionUnsupported):
        ChangeTarget(
            repo_id="repo",
            kind="path",
            value="src/a.py",
            change_contract_version="1.0.0",
        )

    with pytest.raises(ValidationError):
        ChangeTarget(
            repo_id="repo",
            kind="path",
            value="src/a.py",
            unknown_future_field=True,
        )


def test_plan_digest_is_independent_of_timestamps_and_target_order() -> None:
    first = _revision()
    second = _revision(target_order=["src/b.py", "src/a.py"])

    assert first.plan_content_digest == second.plan_content_digest
    assert first.revision_created_at != datetime.min.replace(tzinfo=timezone.utc)


def test_plan_digest_preserves_ordered_acceptance_criteria() -> None:
    first = _revision(criteria=["first", "second"])
    second = _revision(criteria=["second", "first"])

    assert first.plan_content_digest != second.plan_content_digest


@pytest.mark.parametrize(
    "field_name",
    [
        "unknowns",
        "stop_conditions",
        "residual_risk",
        "affected_entrypoints",
        "affected_resources",
        "affected_contracts",
    ],
)
def test_plan_digest_includes_reserved_names_inside_free_form_records(
    field_name: str,
) -> None:
    base = _revision().model_dump(mode="python")
    first_payload = {
        **base,
        field_name: [{"generated_at": "first", "cache": {"index": "one"}}],
        "plan_content_digest": "",
    }
    second_payload = {
        **base,
        field_name: [{"generated_at": "second", "cache": {"index": "two"}}],
        "plan_content_digest": "",
    }

    first = ChangePlanRevision.model_validate(first_payload)
    second = ChangePlanRevision.model_validate(second_payload)

    assert first.plan_content_digest != second.plan_content_digest


def test_plan_digest_includes_free_form_protected_surface_evidence() -> None:
    base = _revision().model_dump(mode="python")
    surface = ProtectedSurface(
        repo_id="test-repo",
        surface_id="surface",
        surface_kind="route",
        detection_source="graph:route",
        capability="partial_or_heuristic",
        confidence="inferred",
        detection_status="candidate",
        evidence=[{"generated_at": "first", "cache": {"index": "one"}}],
    )
    first = ChangePlanRevision.model_validate(
        {
            **base,
            "protected_surfaces": [surface.model_dump(mode="python")],
            "plan_content_digest": "",
        }
    )
    changed_surface = surface.model_copy(
        update={"evidence": [{"generated_at": "second", "cache": {"index": "two"}}]}
    )
    second = ChangePlanRevision.model_validate(
        {
            **base,
            "protected_surfaces": [changed_surface.model_dump(mode="python")],
            "plan_content_digest": "",
        }
    )

    assert first.plan_content_digest != second.plan_content_digest


def test_code_identity_digest_excludes_capture_timestamp_but_not_status() -> None:
    common = {
        "repo_id": "repo",
        "head_sha": "a" * 40,
        "git_tree_sha": "b" * 40,
        "working_tree_clean": False,
        "working_tree_status_digest": "status-a",
        "file_manifest_digest": "manifest",
        "index_version": "build",
        "build_identity_digest": "identity",
    }
    first = CodeIdentity(
        **common,
        captured_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    second = CodeIdentity(
        **common,
        captured_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    changed = CodeIdentity(
        **{**common, "working_tree_status_digest": "status-b"},
        captured_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )

    assert first.code_identity_digest == second.code_identity_digest
    assert first.code_identity_digest != changed.code_identity_digest


def test_partial_protected_surface_cannot_claim_confirmed_detection() -> None:
    with pytest.raises(ValidationError):
        ProtectedSurface(
            repo_id="repo",
            surface_id="surface",
            surface_kind="route",
            detection_source="graph:route",
            capability="partial_or_heuristic",
            confidence="inferred",
            detection_status="confirmed",
        )
