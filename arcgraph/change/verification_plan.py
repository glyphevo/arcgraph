"""Deterministic construction of the single authoritative verification plan."""

from __future__ import annotations

from typing import Iterable

from arcgraph.change.contracts import (
    ProtectedSurface,
    ScopeItem,
    VerificationPlan,
    VerificationRequirement,
    canonical_digest,
)


def build_verification_plan(
    *,
    repo_id: str,
    plan_id: str,
    plan_revision: int,
    input_digest: str,
    allowed_edit_scope: Iterable[ScopeItem],
    protected_surfaces: Iterable[ProtectedSurface],
    acceptance_criteria: list[str],
) -> VerificationPlan:
    """Create requirements from concrete inputs, never from task free text."""

    requirements: list[VerificationRequirement] = []
    for scope in sorted(allowed_edit_scope, key=lambda item: item.scope_item_id):
        requirements.append(
            _requirement(
                repo_id=repo_id,
                category="scope",
                level="required",
                subject=scope.scope_item_id,
                reason="direct allowed edit scope must remain exact",
                acceptance_condition="all changed regions map to this approved scope",
                source_scope_item_ids=[scope.scope_item_id],
                minimum_attestation_level="self_reported",
            )
        )
    for position, criterion in enumerate(acceptance_criteria):
        requirements.append(
            _requirement(
                repo_id=repo_id,
                category="acceptance",
                level="required",
                subject=f"criterion:{position}",
                reason=criterion,
                acceptance_condition=criterion,
                minimum_attestation_level="artifact_backed",
            )
        )
    for surface in sorted(protected_surfaces, key=lambda item: item.surface_id):
        requirements.append(
            _requirement(
                repo_id=repo_id,
                category="protected_surface",
                level="required",
                subject=surface.surface_id,
                reason=(
                    f"{surface.surface_kind} requires explicit review: "
                    f"{surface.detection_status}"
                ),
                acceptance_condition="protected-surface impact is reviewed and evidenced",
                minimum_attestation_level="artifact_backed",
            )
        )
    unique = {requirement.requirement_id: requirement for requirement in requirements}
    return VerificationPlan(
        repo_id=repo_id,
        plan_id=plan_id,
        plan_revision=plan_revision,
        requirements=[unique[key] for key in sorted(unique)],
        input_digest=input_digest,
    )


def _requirement(
    *,
    repo_id: str,
    category: str,
    level: str,
    subject: str,
    reason: str,
    acceptance_condition: str,
    minimum_attestation_level: str,
    source_scope_item_ids: list[str] | None = None,
) -> VerificationRequirement:
    requirement_id = (
        "requirement-"
        + canonical_digest(
            {
                "repo_id": repo_id,
                "category": category,
                "subject": subject,
                "reason": reason,
                "acceptance_condition": acceptance_condition,
                "source_scope_item_ids": source_scope_item_ids or [],
            }
        )[:20]
    )
    return VerificationRequirement(
        repo_id=repo_id,
        requirement_id=requirement_id,
        category=category,
        level=level,  # type: ignore[arg-type]
        subject=subject,
        reason=reason,
        acceptance_condition=acceptance_condition,
        minimum_attestation_level=minimum_attestation_level,  # type: ignore[arg-type]
        source_scope_item_ids=source_scope_item_ids or [],
    )
