from __future__ import annotations

from arcgraph.change.contracts import ProtectedSurface, ScopeItem
from arcgraph.change.verification_plan import build_verification_plan


def test_verification_plan_is_the_single_requirement_authority() -> None:
    scope = ScopeItem(
        repo_id="repo",
        scope_item_id="scope",
        path="src/app.py",
        path_comparison_key="src/app.py",
        scope_kind="path",
        policy="allowed",
        reason="target",
    )
    surface = ProtectedSurface(
        repo_id="repo",
        surface_id="surface",
        surface_kind="route",
        path="src/app.py",
        path_comparison_key="src/app.py",
        detection_source="graph:route",
        capability="partial_or_heuristic",
        confidence="inferred",
        detection_status="candidate",
    )

    plan = build_verification_plan(
        repo_id="repo",
        plan_id="plan",
        plan_revision=1,
        input_digest="input",
        allowed_edit_scope=[scope],
        protected_surfaces=[surface],
        acceptance_criteria=["first", "second"],
    )

    assert len(plan.requirements) == 4
    assert {item.category for item in plan.requirements} == {
        "scope",
        "acceptance",
        "protected_surface",
    }
