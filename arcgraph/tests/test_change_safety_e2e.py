"""Real Git/build end-to-end tests for Surgical Change Safety.

These tests deliberately use a copied, committed fixture project and the real
ArcGraph indexer.  They complement focused unit tests by proving that a plan,
Git working-tree state, immutable graph builds, evidence ingress, verification,
purge, and lifecycle records compose safely across the public service boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from arcgraph.change.contracts import (
    ChangeIntent,
    ChangeTarget,
    EvidencePurgeEvent,
)
from arcgraph.change.errors import (
    BaselineIntegrityMismatch,
    ChangePlanStateError,
    ChangeStoreCorrupt,
    CurrentBuildCodeIdentityMismatch,
)
from arcgraph.change.service import ChangeSafetyService
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.tests.change_safety_helpers import git

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "change_safety_project"
REPO_ID = "change-safety-e2e"


def _prepare_project(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / f"workspace-{len(list(tmp_path.glob('workspace-*')))}"
    repo = workspace / "project"
    shutil.copytree(FIXTURE_ROOT, repo)
    git(repo.parent, "init", repo.name)
    git(repo, "config", "user.email", "change-safety@example.invalid")
    git(repo, "config", "user.name", "Change Safety E2E")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture baseline")
    output = workspace / "output"
    _build(repo, output)
    return repo, output


def _build(repo: Path, output: Path) -> None:
    ArcGraphIndexer(
        repo_root=repo,
        output_dir=output,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()


def _approved_service_and_plan(
    repo: Path,
    output: Path,
    *,
    plan_id: str = "plan-e2e",
) -> tuple[ChangeSafetyService, object]:
    target = next(
        node
        for node in GraphStoreReader.from_current(output).iter_nodes()
        if node.path == "src/change_safety_app/service.py"
        and node.name == "change_status"
    )
    service = ChangeSafetyService(repo, output, repo_id=REPO_ID)
    view = service.plan(
        ChangeIntent(
            repo_id=REPO_ID,
            intent_id=f"intent-{plan_id}",
            task="change only the fixture service status behavior",
            targets=[
                ChangeTarget(
                    repo_id=REPO_ID,
                    kind="symbol",
                    value=target.id,
                )
            ],
        ),
        plan_id=plan_id,
        pin_id=f"pin-{plan_id}",
    )
    plan = view.plan_revision
    service.approve(
        plan.plan_id,
        plan.revision,
        plan.plan_content_digest,
        actor="e2e-reviewer",
        reason="exact fixture scope reviewed",
    )
    return service, plan


def _record_passing_evidence(service: ChangeSafetyService, plan: object) -> list[str]:
    evidence_ids: list[str] = []
    for position, requirement in enumerate(plan.verification_plan.requirements):
        if requirement.minimum_attestation_level == "trusted_runner":
            raise AssertionError("fixture must not need an external trusted runner")
        evidence_id = f"evidence-{position}"
        service.record_evidence_submission(
            evidence_id=evidence_id,
            verification_requirement_id=requirement.requirement_id,
            plan_id=plan.plan_id,
            plan_revision=plan.revision,
            plan_content_digest=plan.plan_content_digest,
            result="pass",
            attestation_level=requirement.minimum_attestation_level,
            producer_identity="fixture-local-runner",
            artifact_digest=f"fixture-artifact-{position}",
        )
        evidence_ids.append(evidence_id)
    return evidence_ids


def _replace(repo: Path, relative_path: str, before: str, after: str) -> None:
    path = repo / relative_path
    content = path.read_text(encoding="utf-8")
    assert before in content
    path.write_text(content.replace(before, after, 1), encoding="utf-8")


def test_e2e_approved_exact_scope_real_git_build_and_archive(tmp_path: Path) -> None:
    repo, output = _prepare_project(tmp_path)
    service, plan = _approved_service_and_plan(repo, output)

    _replace(
        repo,
        "src/change_safety_app/service.py",
        '"status": "planned"',
        '"status": "updated"',
    )
    _build(repo, output)
    evidence_ids = _record_passing_evidence(service, plan)

    report = service.verify(plan.plan_id, plan.revision, plan.plan_content_digest)

    assert report.verdict in {"SAFE_TO_PROCEED", "SAFE_WITH_KNOWN_RISKS"}
    assert report.evidence_ids == evidence_ids
    assert report.graph_delta is not None
    assert report.graph_delta.implementation_changes
    assert any(
        region.path == "src/change_safety_app/service.py"
        for region in report.changed_regions
    )
    assert service.get_view(plan.plan_id).plan_decision.status == "approved"
    assert service.get_view(plan.plan_id).plan_status == "verified"

    archived = service.archive(
        plan.plan_id,
        plan.revision,
        plan.plan_content_digest,
        actor="e2e-reviewer",
        reason="validated fixture change",
    )
    assert archived.plan_status == "archived"
    assert archived.pin_projection["active"] is False


def test_e2e_unapproved_and_out_of_scope_changes_never_pass(tmp_path: Path) -> None:
    # This proves the actual unapproved-plan boundary rather than relying on a
    # synthetic missing-decision object.
    repo, output = _prepare_project(tmp_path)
    target = next(
        node
        for node in GraphStoreReader.from_current(output).iter_nodes()
        if node.path == "src/change_safety_app/service.py"
        and node.name == "change_status"
    )
    unapproved_service = ChangeSafetyService(repo, output, repo_id=REPO_ID)
    unapproved = unapproved_service.plan(
        ChangeIntent(
            repo_id=REPO_ID,
            intent_id="intent-unapproved",
            task="unapproved fixture plan",
            targets=[ChangeTarget(repo_id=REPO_ID, kind="symbol", value=target.id)],
        ),
        plan_id="unapproved-real",
        pin_id="pin-unapproved-real",
    ).plan_revision
    with pytest.raises(ChangePlanStateError):
        unapproved_service.verify(
            unapproved.plan_id,
            unapproved.revision,
            unapproved.plan_content_digest,
        )

    repo, output = _prepare_project(tmp_path)
    service, plan = _approved_service_and_plan(repo, output, plan_id="out-of-scope")
    _replace(
        repo,
        "src/change_safety_app/repository.py",
        'return {"change_id": change_id}',
        'return {"change_id": change_id, "source": "unexpected"}',
    )
    _build(repo, output)
    _record_passing_evidence(service, plan)
    report = service.verify(plan.plan_id, plan.revision, plan.plan_content_digest)

    assert report.verdict != "SAFE_TO_PROCEED"
    assert any(
        finding["code"] == "CHANGE_SCOPE_EXCEEDED" for finding in report.findings
    )


def test_e2e_untracked_and_stale_evidence_never_pass(tmp_path: Path) -> None:
    repo, output = _prepare_project(tmp_path)
    service, plan = _approved_service_and_plan(repo, output, plan_id="untracked")
    (repo / "src" / "change_safety_app" / "untracked.py").write_text(
        "VALUE = 'untracked'\n", encoding="utf-8"
    )
    _build(repo, output)
    _record_passing_evidence(service, plan)
    untracked_report = service.verify(
        plan.plan_id, plan.revision, plan.plan_content_digest
    )

    assert untracked_report.verdict != "SAFE_TO_PROCEED"
    assert any(
        path.change_kind == "untracked"
        and path.new_path == "src/change_safety_app/untracked.py"
        for path in untracked_report.actual_changed_paths
    )

    repo, output = _prepare_project(tmp_path)
    service, plan = _approved_service_and_plan(repo, output, plan_id="stale-evidence")
    _record_passing_evidence(service, plan)
    baseline_head = git(repo, "rev-parse", "HEAD")
    _replace(
        repo,
        "src/change_safety_app/service.py",
        '"status": "planned"',
        '"status": "after-evidence"',
    )
    _build(repo, output)
    stale_report = service.verify(plan.plan_id, plan.revision, plan.plan_content_digest)

    assert git(repo, "rev-parse", "HEAD") == baseline_head
    assert stale_report.verdict != "SAFE_TO_PROCEED"
    assert any(finding["code"] == "EVIDENCE_STALE" for finding in stale_report.findings)


def test_e2e_build_and_baseline_integrity_fail_closed(tmp_path: Path) -> None:
    repo, output = _prepare_project(tmp_path)
    service, plan = _approved_service_and_plan(repo, output, plan_id="build-mismatch")
    _replace(
        repo,
        "src/change_safety_app/service.py",
        '"status": "planned"',
        '"status": "not-reindexed"',
    )
    with pytest.raises(CurrentBuildCodeIdentityMismatch):
        _record_passing_evidence(service, plan)

    repo, output = _prepare_project(tmp_path)
    service, plan = _approved_service_and_plan(
        repo, output, plan_id="tampered-baseline"
    )
    baseline_sqlite = (
        output / plan.baseline.build_identity.build_relative_path / "index.sqlite"
    )
    baseline_sqlite.write_bytes(baseline_sqlite.read_bytes() + b"tampered")
    with pytest.raises(BaselineIntegrityMismatch):
        service.verify(plan.plan_id, plan.revision, plan.plan_content_digest)


def test_e2e_purge_projects_report_and_export_without_rewrite(tmp_path: Path) -> None:
    repo, output = _prepare_project(tmp_path)
    service, plan = _approved_service_and_plan(repo, output, plan_id="purge-view")
    evidence_id = _record_passing_evidence(service, plan)[0]
    report = service.verify(plan.plan_id, plan.revision, plan.plan_content_digest)
    immutable_before = service.get_verification_report(
        plan.plan_id, report.verification_id
    ).model_dump(mode="json")
    evidence = service.get_evidence(plan.plan_id, evidence_id)

    service.purge_evidence(
        EvidencePurgeEvent(
            repo_id=REPO_ID,
            purge_id="purge-e2e",
            plan_id=plan.plan_id,
            plan_revision=plan.revision,
            evidence_id=evidence_id,
            requirement_id=evidence.verification_requirement_id,
            actor="retention-operator",
            reason="fixture retention test",
            purge_mode="remove_material",
            artifact_digest=evidence.artifact_digest,
        )
    )
    report_view = service.get_verification_report_view(
        plan.plan_id, report.verification_id
    )
    immutable_after = service.get_verification_report(
        plan.plan_id, report.verification_id
    ).model_dump(mode="json")

    assert immutable_after == immutable_before
    assert report_view.evidence_availability[evidence_id] == "purged"
    assert report_view.evidence_summary["by_availability"] == {"purged": 1}

    export = json.loads(service.audit_export("purge-e2e-export").read_text("utf-8"))
    assert export["sensitive_material_included"] is False
    assert any(
        item.get("event_type") == "evidence_purged"
        and item.get("payload", {}).get("availability_after") == "purged"
        for item in export["records"]
    )


def test_e2e_store_corruption_and_interrupted_activation_remain_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, output = _prepare_project(tmp_path)
    service, plan = _approved_service_and_plan(repo, output, plan_id="store-corrupt")
    pointer = output / "change-safety" / "plans" / plan.plan_id / "current.json"
    pointer.write_text("not-json", encoding="utf-8")
    with pytest.raises(ChangeStoreCorrupt):
        service.verify(plan.plan_id, plan.revision, plan.plan_content_digest)

    repo, output = _prepare_project(tmp_path)
    service = ChangeSafetyService(repo, output, repo_id=REPO_ID)
    target = next(
        node
        for node in GraphStoreReader.from_current(output).iter_nodes()
        if node.path == "src/change_safety_app/service.py"
        and node.name == "change_status"
    )
    revision = service.preview_plan(
        ChangeIntent(
            repo_id=REPO_ID,
            intent_id="intent-interrupted",
            task="interrupted activation fixture",
            targets=[ChangeTarget(repo_id=REPO_ID, kind="symbol", value=target.id)],
        ),
        plan_id="interrupted",
        pin_id="pin-interrupted",
    )

    def fail_revision_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated revision write interruption")

    monkeypatch.setattr(service.store, "write_revision", fail_revision_write)
    with pytest.raises(OSError):
        service.activate_revision(revision)

    reconciliation = service.reconcile_pins()
    assert "pin-interrupted" in reconciliation["orphan_active_pin_ids"]
    assert reconciliation["automatic_release"] is False


def test_e2e_invalid_intent_and_rejected_revision_require_abandon_before_archive(
    tmp_path: Path,
) -> None:
    repo, output = _prepare_project(tmp_path)
    service = ChangeSafetyService(repo, output, repo_id=REPO_ID)
    view = service.plan(
        ChangeIntent(
            repo_id=REPO_ID,
            intent_id="intent-invalid",
            task="free text must not become a target",
            targets=[],
        ),
        plan_id="invalid-intent",
        pin_id="pin-invalid-intent",
    )
    plan = view.plan_revision

    assert plan.planning_verdict == "PLAN_INVALID_INTENT"
    assert view.plan_status == "blocked"
    assert view.pin_projection["active"]

    rejected = service.reject(
        plan.plan_id,
        plan.revision,
        plan.plan_content_digest,
        actor="e2e-reviewer",
        reason="the structured target is missing",
    )
    assert rejected.plan_status == "blocked"
    assert rejected.plan_decision is not None
    assert rejected.plan_decision.status == "rejected"
    with pytest.raises(ChangePlanStateError):
        service.verify(plan.plan_id, plan.revision, plan.plan_content_digest)
    with pytest.raises(ChangePlanStateError):
        service.archive(
            plan.plan_id,
            plan.revision,
            plan.plan_content_digest,
            actor="e2e-reviewer",
            reason="rejection is not an archive shortcut",
        )

    abandoned = service.abandon(
        plan.plan_id,
        plan.revision,
        plan.plan_content_digest,
        actor="e2e-reviewer",
        reason="close the invalid revision",
    )
    archived = service.archive(
        plan.plan_id,
        plan.revision,
        plan.plan_content_digest,
        actor="e2e-reviewer",
        reason="retain audit then release pin",
    )

    assert abandoned.plan_status == "abandoned"
    assert archived.plan_status == "archived"
    assert archived.pin_projection["active"] is False


def test_e2e_unplanned_symbol_in_an_allowed_file_is_blocked(
    tmp_path: Path,
) -> None:
    repo, output = _prepare_project(tmp_path)
    service, plan = _approved_service_and_plan(repo, output, plan_id="symbol-scope")
    service_path = repo / "src" / "change_safety_app" / "service.py"
    service_path.write_text(
        service_path.read_text(encoding="utf-8")
        + "\n\ndef unrelated_helper() -> str:\n    return 'out-of-scope'\n",
        encoding="utf-8",
    )
    _build(repo, output)
    _record_passing_evidence(service, plan)

    report = service.verify(plan.plan_id, plan.revision, plan.plan_content_digest)

    assert report.verdict != "SAFE_TO_PROCEED"
    assert any(
        finding["code"] == "CHANGE_SCOPE_EXCEEDED" for finding in report.findings
    )
