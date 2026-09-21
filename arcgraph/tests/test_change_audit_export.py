from __future__ import annotations

import json

from arcgraph.change.contracts import VerificationEvidence
from arcgraph.tests.change_safety_helpers import (
    activated_approved_plan,
    current_code_identity,
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
        stdout_digest="stdout",
        stderr_digest="stderr",
        artifact_digest="artifact",
    )


def test_audit_export_is_contained_default_redacted_and_non_authoritative(
    tmp_path,
) -> None:
    repo, output, service, plan = activated_approved_plan(tmp_path)
    service.record_evidence(
        _evidence(plan, current_code_identity(repo, output)),
        material={"secret": "never-export", "summary": "pass"},
    )
    service._audit(
        "simulated_sensitive_reason",
        plan_id=plan.plan_id,
        revision=plan.revision,
        reason=(
            "Bearer audit-secret-token at " "C:\\Users\\alice\\private\\workspace\\repo"
        ),
    )
    report = service.verify(plan.plan_id, plan.revision, plan.plan_content_digest)
    before = service.get_view(plan.plan_id).model_dump(mode="json")

    export_path = service.audit_export("audit-export")
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    after = service.get_view(plan.plan_id).model_dump(mode="json")

    assert export_path == output / "change-safety" / "exports" / "audit-export.json"
    assert payload["sensitive_material_included"] is False
    assert payload["redaction_assurance"] == "best_effort_pattern_based"
    serialized = json.dumps(payload, sort_keys=True)
    for forbidden in (
        "runner_metadata",
        "stdout_digest",
        "stderr_digest",
        "artifact_digest",
        "never-export",
        "audit-secret-token",
        "alice",
    ):
        assert forbidden not in serialized
    assert any(
        item.get("verification_id") == report.verification_id
        for item in payload["records"]
    )
    assert before == after
