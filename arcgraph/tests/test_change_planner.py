from __future__ import annotations

from pathlib import Path

from arcgraph.change.contracts import (
    SOURCE_NORMALIZATION_PROJECTION_VERSION,
    BaselineReference,
    ChangeIntent,
    ChangeTarget,
    ProtectedSurface,
    ScopeItem,
    plan_content_projection,
)
from arcgraph.change.planner import ChangePlanner, _surface_matches_scope
from arcgraph.core.schemas import Node
from arcgraph.tests.change_safety_helpers import (
    baseline_reference,
    git,
    init_repo,
    write_build,
)


def _prepared_baseline(tmp_path: Path) -> tuple[Path, BaselineReference]:
    repo = init_repo(tmp_path)
    (repo / "src" / "api.py").write_text("# route source\n", encoding="utf-8")
    git(repo, "add", "src/api.py")
    git(repo, "commit", "-m", "add route source")
    output = tmp_path / "output"
    write_build(
        repo,
        output,
        index_version="index",
        commit_sha=git(repo, "rev-parse", "HEAD"),
    )
    return repo, baseline_reference(repo, output, index_version="index")


def _route_node() -> Node:
    return Node(
        id="route:GET:/items",
        kind="route",
        name="items",
        path="src/api.py",
        start_line=10,
        end_line=15,
        properties={"route": "GET /items"},
    )


def test_planner_requires_an_explicit_valid_target(tmp_path: Path) -> None:
    repo, baseline = _prepared_baseline(tmp_path)
    planner = ChangePlanner(repo, repo_id="repo")
    intent = ChangeIntent(
        repo_id="repo",
        intent_id="intent",
        task="Modify route GET /items, but do not infer it from this text.",
        targets=[],
    )

    plan = planner.plan(intent, baseline, [_route_node()], plan_id="plan")

    assert plan.planning_verdict == "PLAN_INVALID_INTENT"
    assert plan.allowed_edit_scope == []


def test_planner_uses_target_not_free_text_and_keeps_scopes_separate(
    tmp_path: Path,
) -> None:
    repo, baseline = _prepared_baseline(tmp_path)
    planner = ChangePlanner(repo, repo_id="repo")
    intent = ChangeIntent(
        repo_id="repo",
        intent_id="intent",
        task="Mentioning another route here cannot discover it.",
        targets=[
            ChangeTarget(
                repo_id="repo",
                kind="route",
                value="GET /items",
            )
        ],
        acceptance_criteria=["the response remains compatible"],
    )

    plan = planner.plan(intent, baseline, [_route_node()], plan_id="plan")

    assert plan.planning_verdict == "PLAN_READY_WITH_KNOWN_RISKS"
    assert {scope.policy for scope in plan.primary_edit_candidates} == {"candidate"}
    assert {scope.policy for scope in plan.allowed_edit_scope} == {"allowed"}
    assert {scope.policy for scope in plan.protected_scope} == {"protected"}
    assert plan.verification_plan.requirements
    assert not hasattr(plan, "required_tests")


def test_planner_repeated_runs_have_identical_stable_output(tmp_path: Path) -> None:
    repo, baseline = _prepared_baseline(tmp_path)
    planner = ChangePlanner(repo, repo_id="repo")
    intent = ChangeIntent(
        repo_id="repo",
        intent_id="intent",
        task="Modify only the declared route.",
        targets=[
            ChangeTarget(
                repo_id="repo",
                kind="route",
                value="GET /items",
            )
        ],
        acceptance_criteria=["the response remains compatible"],
    )
    nodes = [
        _route_node(),
        Node(
            id="fn:api.helper",
            kind="function",
            name="helper",
            path="src/api.py",
            start_line=20,
            end_line=22,
        ),
    ]

    first = planner.plan(intent, baseline, nodes, plan_id="plan")
    second = planner.plan(intent, baseline, reversed(nodes), plan_id="plan")

    assert plan_content_projection(first) == plan_content_projection(second)
    assert first.plan_content_digest == second.plan_content_digest


def test_planner_records_baseline_digests_for_mapped_symbol_scope(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    output = tmp_path / "output"
    write_build(
        repo,
        output,
        index_version="index",
        commit_sha=git(repo, "rev-parse", "HEAD"),
    )
    baseline = baseline_reference(repo, output, index_version="index")
    node = Node(
        id="fn:app.f",
        kind="function",
        name="f",
        path="src/app.py",
        start_line=1,
        end_line=2,
    )
    plan = ChangePlanner(repo, repo_id="repo").plan(
        ChangeIntent(
            repo_id="repo",
            intent_id="intent",
            task="explicit local function target",
            targets=[ChangeTarget(repo_id="repo", kind="symbol", value="fn:app.f")],
        ),
        baseline,
        [node],
        plan_id="plan",
    )

    scope = plan.allowed_edit_scope[0]
    assert scope.baseline_raw_source_digest
    assert scope.baseline_normalized_implementation_digest
    assert (
        scope.source_normalization_projection_version
        == SOURCE_NORMALIZATION_PROJECTION_VERSION
    )


def test_user_declared_protected_path_is_applied_to_matching_target(
    tmp_path: Path,
) -> None:
    repo, baseline = _prepared_baseline(tmp_path)
    planner = ChangePlanner(repo, repo_id="repo")
    intent = ChangeIntent(
        repo_id="repo",
        intent_id="intent",
        task="narrow path edit",
        targets=[ChangeTarget(repo_id="repo", kind="path", value="src/api.py")],
        user_declared_protected_surfaces=["src/api.py"],
    )

    plan = planner.plan(intent, baseline, [_route_node()], plan_id="plan")

    assert plan.protected_surfaces
    assert plan.protected_scope[0].path == "src/api.py"
    assert any(
        surface.detection_status == "declared" for surface in plan.protected_surfaces
    )


def test_user_declared_off_target_path_still_enters_protected_scope(
    tmp_path: Path,
) -> None:
    repo, baseline = _prepared_baseline(tmp_path)
    planner = ChangePlanner(repo, repo_id="repo")
    intent = ChangeIntent(
        repo_id="repo",
        intent_id="intent",
        task="narrow path edit",
        targets=[ChangeTarget(repo_id="repo", kind="path", value="src/api.py")],
        user_declared_protected_surfaces=["src/other.py"],
    )

    plan = planner.plan(intent, baseline, [_route_node()], plan_id="plan")

    assert {scope.path for scope in plan.allowed_edit_scope} == {"src/api.py"}
    assert "src/other.py" in {scope.path for scope in plan.protected_scope}


def test_pathless_user_declaration_protects_each_allowed_target(
    tmp_path: Path,
) -> None:
    repo, baseline = _prepared_baseline(tmp_path)
    plan = ChangePlanner(repo, repo_id="repo").plan(
        ChangeIntent(
            repo_id="repo",
            intent_id="intent",
            task="route declaration applies globally",
            targets=[ChangeTarget(repo_id="repo", kind="path", value="src/api.py")],
            user_declared_protected_surfaces=["route"],
        ),
        baseline,
        [_route_node()],
        plan_id="plan",
    )

    assert {scope.path for scope in plan.protected_scope} == {"src/api.py"}
    assert any(
        surface.detection_source == "user_declaration" and surface.path is None
        for surface in plan.protected_surfaces
    )


def test_planner_rejects_a_path_traversal_target(tmp_path: Path) -> None:
    repo, baseline = _prepared_baseline(tmp_path)
    plan = ChangePlanner(repo, repo_id="repo").plan(
        ChangeIntent(
            repo_id="repo",
            intent_id="intent",
            task="path escape must not resolve",
            targets=[ChangeTarget(repo_id="repo", kind="path", value="../outside.py")],
        ),
        baseline,
        [_route_node()],
        plan_id="plan",
    )

    assert plan.planning_verdict == "PLAN_BLOCKED_UNRESOLVED_TARGET"
    assert plan.allowed_edit_scope == []


def test_target_from_another_repo_never_resolves(tmp_path: Path) -> None:
    repo, _baseline = _prepared_baseline(tmp_path)
    planner = ChangePlanner(repo, repo_id="repo")
    target = ChangeTarget(repo_id="another-repo", kind="path", value="src/api.py")

    assert planner._resolve_target(target, [_route_node()], None) == []


def test_pathless_symbol_ids_do_not_match_only_because_both_are_none() -> None:
    surface = ProtectedSurface(
        repo_id="repo",
        surface_id="surface-other",
        surface_kind="security_boundary",
        path="src/other.py",
        path_comparison_key="src/other.py",
        detection_source="user_declaration",
        capability="user_declaration_required",
        confidence="confirmed",
        detection_status="declared",
    )
    scope = ScopeItem(
        repo_id="repo",
        scope_item_id="scope-target",
        path="src/api.py",
        path_comparison_key="src/api.py",
        scope_kind="path",
        policy="allowed",
        reason="explicit path target",
        confidence="confirmed",
    )

    assert not _surface_matches_scope(surface, scope)


def test_same_file_protected_symbol_does_not_capture_an_unrelated_symbol_target(
    tmp_path: Path,
) -> None:
    repo, baseline = _prepared_baseline(tmp_path)
    helper = Node(
        id="fn:api.helper",
        kind="function",
        name="helper",
        qualname="api.helper",
        path="src/api.py",
        start_line=1,
        end_line=1,
    )
    route = _route_node()
    plan = ChangePlanner(repo, repo_id="repo").plan(
        ChangeIntent(
            repo_id="repo",
            intent_id="intent-helper",
            task="edit only the unrelated helper",
            targets=[
                ChangeTarget(
                    repo_id="repo",
                    kind="symbol",
                    value=helper.id,
                )
            ],
        ),
        baseline,
        [helper, route],
        plan_id="plan-helper",
    )

    assert {scope.symbol_id for scope in plan.allowed_edit_scope} == {helper.id}
    assert plan.protected_scope == []
    assert not any(surface.symbol_id == route.id for surface in plan.protected_surfaces)


def test_planner_keeps_review_only_surfaces_out_of_protected_scope(
    tmp_path: Path,
) -> None:
    """A review-only capability must produce its own contract scope."""

    repo, baseline = _prepared_baseline(tmp_path)

    class ReviewOnlyDetector:
        def detect(self, *_args: object, **_kwargs: object) -> list[ProtectedSurface]:
            return [
                ProtectedSurface(
                    repo_id="repo",
                    surface_id="surface-review-route",
                    surface_kind="route",
                    path="src/api.py",
                    path_comparison_key="src/api.py",
                    symbol_id="route:GET:/items",
                    detection_source="test",
                    capability="partial_or_heuristic",
                    confidence="inferred",
                    detection_status="candidate",
                    policy="review_only",
                )
            ]

    planner = ChangePlanner(
        repo,
        repo_id="repo",
        protected_surface_detector=ReviewOnlyDetector(),  # type: ignore[arg-type]
    )
    plan = planner.plan(
        ChangeIntent(
            repo_id="repo",
            intent_id="intent",
            task="narrow route edit",
            targets=[ChangeTarget(repo_id="repo", kind="route", value="GET /items")],
        ),
        baseline,
        [_route_node()],
        plan_id="plan",
    )

    assert {scope.policy for scope in plan.review_only_scope} == {"review_only"}
    assert plan.protected_scope == []


def test_planner_blocks_an_unresolved_explicit_target(tmp_path: Path) -> None:
    repo, baseline = _prepared_baseline(tmp_path)
    planner = ChangePlanner(repo, repo_id="repo")
    intent = ChangeIntent(
        repo_id="repo",
        intent_id="intent",
        task="the planner must not expand this text into a target",
        targets=[ChangeTarget(repo_id="repo", kind="path", value="src/missing.py")],
    )

    plan = planner.plan(intent, baseline, [_route_node()], plan_id="plan")

    assert plan.planning_verdict == "PLAN_BLOCKED_UNRESOLVED_TARGET"
    assert plan.allowed_edit_scope == []


def test_planner_blocks_targeted_dynamic_registration(tmp_path: Path) -> None:
    repo, baseline = _prepared_baseline(tmp_path)
    planner = ChangePlanner(repo, repo_id="repo")
    dynamic = Node(
        id="registration:dynamic",
        kind="framework_registration",
        name="dynamic",
        path="src/api.py",
        start_line=1,
        end_line=1,
        properties={"dynamic": True},
    )
    intent = ChangeIntent(
        repo_id="repo",
        intent_id="intent",
        task="explicit dynamic registration target",
        targets=[
            ChangeTarget(
                repo_id="repo",
                kind="symbol",
                value="registration:dynamic",
            )
        ],
    )

    plan = planner.plan(intent, baseline, [dynamic], plan_id="plan")

    assert plan.planning_verdict == "PLAN_BLOCKED_CAPABILITY_GAP"
    assert any(item["code"] == "CAPABILITY_UNKNOWN" for item in plan.stop_conditions)


def test_planner_blocks_a_dirty_baseline_before_ready_activation(
    tmp_path: Path,
) -> None:
    repo, baseline = _prepared_baseline(tmp_path)
    planner = ChangePlanner(repo, repo_id="repo")
    dirty = baseline.model_copy(
        update={
            "working_tree_identity": baseline.working_tree_identity.model_copy(
                update={"clean": False}
            )
        }
    )
    intent = ChangeIntent(
        repo_id="repo",
        intent_id="intent",
        task="explicit route target",
        targets=[ChangeTarget(repo_id="repo", kind="route", value="GET /items")],
    )

    plan = planner.plan(intent, dirty, [_route_node()], plan_id="plan")

    assert plan.planning_verdict == "PLAN_BLOCKED_STALE_INDEX"
    assert any(item["code"] == "BASELINE_UNAVAILABLE" for item in plan.stop_conditions)
