from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from arcgraph.change.contracts import (
    BaselineReference,
    BuildIdentity,
    ChangeIntent,
    ChangeCurrentPointer,
    ChangePlanRevision,
    ChangeTarget,
    CodeIdentity,
    EvidencePurgeEvent,
    PlanDecision,
    PlanLifecycleEvent,
    VerificationEvidence,
    VerificationPlan,
    VerificationRequirement,
    WorkingTreeIdentity,
)
from arcgraph.change.errors import ChangeStoreCorrupt
from arcgraph.change.store import (
    ChangeStateStore,
    atomic_write_json,
    read_json_object,
)


def _revision() -> ChangePlanRevision:
    repo_id = "repo"
    build = BuildIdentity(
        repo_id=repo_id,
        index_version="index-1",
        build_relative_path="builds/index-1",
        file_manifest_digest="files",
        summary_digest="summary",
        index_sqlite_sha256="sqlite",
        index_sqlite_size=1,
    )
    baseline = BaselineReference(
        repo_id=repo_id,
        baseline_id="baseline",
        build_identity=build,
        working_tree_identity=WorkingTreeIdentity(
            repo_id=repo_id,
            clean=True,
            status_digest="clean",
        ),
        pin_id="pin-1",
        baseline_source_identity={"repo_id": repo_id},
    )
    intent = ChangeIntent(
        repo_id=repo_id,
        intent_id="intent",
        task="narrow change",
        targets=[ChangeTarget(repo_id=repo_id, kind="path", value="src/a.py")],
    )
    verification = VerificationPlan(
        repo_id=repo_id,
        plan_id="plan",
        plan_revision=1,
        input_digest="verification",
        requirements=[
            VerificationRequirement(
                repo_id=repo_id,
                requirement_id="req-1",
                category="test",
                level="required",
                subject="src/a.py",
                reason="scope",
                acceptance_condition="passes",
            )
        ],
    )
    return ChangePlanRevision(
        repo_id=repo_id,
        plan_id="plan",
        revision=1,
        intent=intent,
        baseline=baseline,
        planning_verdict="PLAN_READY",
        verification_plan=verification,
        input_digest="input",
    )


def test_store_writes_immutable_revision_and_rejects_a_conflict(tmp_path: Path) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    revision = _revision()

    store.write_revision(revision)
    assert (
        store.read_revision("plan", 1).plan_content_digest
        == revision.plan_content_digest
    )

    conflicting = revision.model_copy(
        update={"input_digest": "different", "plan_content_digest": ""}
    )
    with pytest.raises(ChangeStoreCorrupt):
        store.write_revision(conflicting)


def test_purge_keeps_immutable_evidence_record_and_writes_audit_event(
    tmp_path: Path,
) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    revision = _revision()
    store.write_revision(revision)
    identity = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="files",
        index_version="index-1",
        build_identity_digest="build",
    )
    evidence = VerificationEvidence(
        repo_id="repo",
        evidence_id="evidence-1",
        verification_requirement_id="req-1",
        plan_id="plan",
        plan_revision=1,
        plan_content_digest=revision.plan_content_digest,
        result="pass",
        current_code_identity=identity,
        attestation_level="artifact_backed",
        producer_identity="local-test",
        artifact_digest="artifact",
    )
    store.write_evidence(evidence, material={"stdout": "sensitive"})
    store.purge_evidence_material(
        EvidencePurgeEvent(
            repo_id="repo",
            purge_id="purge-1",
            plan_id="plan",
            plan_revision=1,
            evidence_id="evidence-1",
            requirement_id="req-1",
            actor="operator",
            reason="retention",
            purge_mode="remove_material",
            artifact_digest="artifact",
        )
    )

    assert store.read_evidence("plan", "evidence-1").artifact_digest == "artifact"
    assert (
        store.evidence_material_projection("plan", "evidence-1")["availability"]
        == "purged"
    )


def test_store_fails_closed_on_a_corrupt_record_header(tmp_path: Path) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    revision = _revision()
    path = store.write_revision(revision)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["repo_id"] = "other-repo"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ChangeStoreCorrupt):
        store.read_revision("plan", 1)


def test_read_fails_closed_when_the_record_cannot_be_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "plans" / "plan" / "current.json"
    atomic_write_json(target, {"revision": 1}, root=tmp_path)
    original = os.lstat

    def denied(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if os.fspath(path).endswith("current.json"):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", denied)

    with pytest.raises(ChangeStoreCorrupt, match="cannot inspect") as caught:
        read_json_object(target, root=tmp_path)
    assert isinstance(caught.value.__cause__, PermissionError)


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="needs POSIX permissions that apply to the current user",
)
def test_read_fails_closed_when_a_parent_directory_denies_search(
    tmp_path: Path,
) -> None:
    target = tmp_path / "plans" / "plan" / "current.json"
    atomic_write_json(target, {"revision": 1}, root=tmp_path)
    target.parent.chmod(0o600)
    try:
        with pytest.raises(ChangeStoreCorrupt, match="cannot inspect"):
            read_json_object(target, root=tmp_path)
    finally:
        target.parent.chmod(0o700)


def test_health_check_rejects_unsafe_entries_and_cross_plan_events(
    tmp_path: Path,
) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    revision = _revision()
    store.write_revision(revision)
    lifecycle = store.root / "plans" / "plan" / "lifecycle"
    lifecycle.mkdir(parents=True)
    (lifecycle / "sidecar.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ChangeStoreCorrupt, match="unsafe entry"):
        store.health_check()

    (lifecycle / "sidecar.txt").unlink()
    event = PlanLifecycleEvent(
        repo_id="repo",
        event_id="lifecycle-foreign",
        plan_id="other-plan",
        plan_revision=1,
        prior_status=None,
        next_status="ready",
        reason="forged parent binding",
    )
    (lifecycle / f"{event.event_id}.json").write_text(
        json.dumps(event.model_dump(mode="json")),
        encoding="utf-8",
    )

    with pytest.raises(ChangeStoreCorrupt, match="does not match its plan directory"):
        store.health_check()


def test_health_check_rejects_lifecycle_event_for_a_missing_revision(
    tmp_path: Path,
) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    revision = _revision()
    store.write_revision(revision)
    event = PlanLifecycleEvent(
        repo_id="repo",
        event_id="lifecycle-unbound",
        plan_id="plan",
        plan_revision=2,
        prior_status=None,
        next_status="ready",
        reason="forged revision binding",
    )
    path = store.root / "plans" / "plan" / "lifecycle" / f"{event.event_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(event.model_dump(mode="json")), encoding="utf-8")

    with pytest.raises(ChangeStoreCorrupt, match="does not bind an immutable"):
        store.health_check()


def test_health_check_rejects_unrecognized_plan_layout_entries(
    tmp_path: Path,
) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    revision = _revision()
    store.write_revision(revision)
    (store.root / "plans" / "plan" / "unexpected.txt").write_text(
        "unexpected",
        encoding="utf-8",
    )

    with pytest.raises(ChangeStoreCorrupt, match="unrecognized entry"):
        store.health_check()


def test_health_check_rejects_orphan_evidence_material(tmp_path: Path) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    revision = _revision()
    store.write_revision(revision)
    identity = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="files",
        index_version="index-1",
        build_identity_digest="build",
    )
    evidence = VerificationEvidence(
        repo_id="repo",
        evidence_id="evidence-1",
        verification_requirement_id="req-1",
        plan_id="plan",
        plan_revision=1,
        plan_content_digest=revision.plan_content_digest,
        result="pass",
        current_code_identity=identity,
        attestation_level="artifact_backed",
        producer_identity="local-test",
        artifact_digest="artifact",
    )
    store.write_evidence(evidence)
    material_directory = (
        store.root / "plans" / "plan" / "evidence-material" / "evidence-1"
    )
    (material_directory / "unexpected.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ChangeStoreCorrupt, match="does not match its immutable"):
        store.health_check()


def test_failed_evidence_publication_removes_unreachable_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    revision = _revision()
    identity = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="files",
        index_version="index-1",
        build_identity_digest="build",
    )
    evidence = VerificationEvidence(
        repo_id="repo",
        evidence_id="evidence-rollback",
        verification_requirement_id="req-1",
        plan_id="plan",
        plan_revision=1,
        plan_content_digest=revision.plan_content_digest,
        result="pass",
        current_code_identity=identity,
        attestation_level="artifact_backed",
        producer_identity="local-test",
        artifact_digest="artifact",
    )

    def fail_publication(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated record publication failure")

    monkeypatch.setattr(store, "_write_immutable_model", fail_publication)
    with pytest.raises(OSError, match="simulated"):
        store.write_evidence(evidence)

    material_root = store.root / "plans" / "plan" / "evidence-material"
    assert not material_root.exists()


def test_evidence_record_published_before_an_error_retains_its_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    revision = _revision()
    store.write_revision(revision)
    identity = CodeIdentity(
        repo_id="repo",
        working_tree_clean=True,
        working_tree_status_digest="clean",
        file_manifest_digest="files",
        index_version="index-1",
        build_identity_digest="build",
    )
    evidence = VerificationEvidence(
        repo_id="repo",
        evidence_id="evidence-published",
        verification_requirement_id="req-1",
        plan_id="plan",
        plan_revision=1,
        plan_content_digest=revision.plan_content_digest,
        result="pass",
        current_code_identity=identity,
        attestation_level="artifact_backed",
        producer_identity="local-test",
        artifact_digest="artifact",
    )
    original_publish = store._write_immutable_model

    def publish_then_fail(*args: object, **kwargs: object) -> None:
        original_publish(*args, **kwargs)
        raise OSError("simulated post-publication failure")

    monkeypatch.setattr(store, "_write_immutable_model", publish_then_fail)
    with pytest.raises(OSError, match="post-publication"):
        store.write_evidence(evidence, material={"stdout": "retained"})

    assert store.read_evidence("plan", evidence.evidence_id) == evidence
    assert (
        store.evidence_material_projection("plan", evidence.evidence_id)["availability"]
        == "available"
    )
    store.health_check()


def test_terminal_decision_write_recovers_after_pointer_publication_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    revision = _revision()
    store.write_revision(revision)
    pending = PlanDecision(
        repo_id="repo",
        decision_id="decision-pending",
        plan_id="plan",
        plan_revision=1,
        plan_content_digest=revision.plan_content_digest,
        status="pending",
        actor="system",
        reason="initial decision",
    )
    store.write_decision(pending)
    original_write = store._write_model

    def fail_pointer(path: Path, model: object) -> None:
        if path.name == "decision-current.json":
            raise OSError("simulated decision pointer failure")
        original_write(path, model)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_write_model", fail_pointer)
    with pytest.raises(OSError, match="pointer failure"):
        store.write_decision(
            PlanDecision(
                repo_id="repo",
                decision_id="decision-approved-first-attempt",
                plan_id="plan",
                plan_revision=1,
                plan_content_digest=revision.plan_content_digest,
                status="approved",
                actor="reviewer",
                reason="approved",
            )
        )
    assert store.read_current_decision("plan") == pending

    monkeypatch.setattr(store, "_write_model", original_write)
    store.write_decision(
        PlanDecision(
            repo_id="repo",
            decision_id="decision-approved-retry",
            plan_id="plan",
            plan_revision=1,
            plan_content_digest=revision.plan_content_digest,
            status="approved",
            actor="reviewer",
            reason="approved retry",
        )
    )

    assert store.read_current_decision("plan").status == "approved"
    assert len(store.list_decisions("plan")) == 2
    store.health_check()


def test_health_check_rejects_current_and_decision_pointers_for_different_revisions(
    tmp_path: Path,
) -> None:
    store = ChangeStateStore(tmp_path / "output", repo_id="repo")
    first = _revision()
    second_payload = first.model_dump(mode="json")
    second_payload["revision"] = 2
    second_payload["input_digest"] = "input-2"
    second_payload["plan_content_digest"] = ""
    second_payload["verification_plan"]["plan_revision"] = 2
    second = ChangePlanRevision.model_validate(second_payload)
    store.write_revision(first)
    store.write_revision(second)
    store.write_decision(
        PlanDecision(
            repo_id="repo",
            decision_id="decision-first",
            plan_id="plan",
            plan_revision=1,
            plan_content_digest=first.plan_content_digest,
            status="pending",
            actor="system",
            reason="first revision",
        )
    )
    store.write_decision(
        PlanDecision(
            repo_id="repo",
            decision_id="decision-second",
            plan_id="plan",
            plan_revision=2,
            plan_content_digest=second.plan_content_digest,
            status="pending",
            actor="system",
            reason="second revision",
        )
    )
    store.write_current_pointer(
        ChangeCurrentPointer(
            repo_id="repo",
            plan_id="plan",
            plan_revision=1,
            plan_content_digest=first.plan_content_digest,
            pin_id="pin-1",
        )
    )

    with pytest.raises(ChangeStoreCorrupt, match="decision current pointer"):
        store.health_check()
