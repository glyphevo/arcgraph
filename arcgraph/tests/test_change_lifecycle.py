from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcgraph.change.contracts import (
    BaselineReference,
    BuildIdentity,
    ChangeIntent,
    ChangePlanRevision,
    ChangeTarget,
    VerificationPlan,
    WorkingTreeIdentity,
)
from arcgraph.change.errors import ChangePlanStateError, ChangeStoreCorrupt
from arcgraph.change.service import ChangeSafetyService


def _revision() -> ChangePlanRevision:
    build = BuildIdentity(
        repo_id="repo",
        index_version="index",
        build_relative_path="builds/index",
        file_manifest_digest="files",
        summary_digest="summary",
        index_sqlite_sha256="sqlite",
        index_sqlite_size=1,
    )
    baseline = BaselineReference(
        repo_id="repo",
        baseline_id="baseline",
        build_identity=build,
        working_tree_identity=WorkingTreeIdentity(
            repo_id="repo",
            clean=True,
            status_digest="clean",
        ),
        pin_id="pin-1",
        baseline_source_identity={"repo_id": "repo"},
    )
    return ChangePlanRevision(
        repo_id="repo",
        plan_id="plan",
        revision=1,
        intent=ChangeIntent(
            repo_id="repo",
            intent_id="intent",
            task="narrow change",
            targets=[ChangeTarget(repo_id="repo", kind="path", value="src/a.py")],
        ),
        baseline=baseline,
        planning_verdict="PLAN_READY",
        verification_plan=VerificationPlan(
            repo_id="repo",
            plan_id="plan",
            plan_revision=1,
            input_digest="verification",
        ),
        input_digest="input",
    )


def _write_build(output_dir: Path) -> None:
    build = output_dir / "builds" / "index"
    build.mkdir(parents=True)
    (build / "index.sqlite").write_bytes(b"sqlite")
    (build / "summary.json").write_text("{}", encoding="utf-8")
    (output_dir / "current.json").write_text(
        json.dumps({"build_dir": "builds/index"}), encoding="utf-8"
    )


def _activated_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ChangeSafetyService, ChangePlanRevision, Path]:
    output = tmp_path / "output"
    _write_build(output)
    monkeypatch.setattr(
        "arcgraph.change.service.validate_baseline_reference", lambda *args: None
    )
    service = ChangeSafetyService(tmp_path, output, repo_id="repo")
    revision = _revision()
    service.activate_revision(revision)
    return service, revision, output


def test_view_rejects_current_pointer_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _revision_record, _output = _activated_service(tmp_path, monkeypatch)
    pointer_path = service.store.root / "plans" / "plan" / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["plan_content_digest"] = "tampered-pointer-digest"
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    with pytest.raises(ChangeStoreCorrupt, match="pointer digest"):
        service.get_view("plan")


def test_view_rejects_current_decision_for_another_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _revision_record, _output = _activated_service(tmp_path, monkeypatch)
    decision = service.store.read_current_decision("plan")
    assert decision is not None
    forged = decision.model_copy(update={"plan_revision": 2})
    monkeypatch.setattr(
        service.store,
        "read_current_decision",
        lambda _plan_id: forged,
    )

    with pytest.raises(ChangeStoreCorrupt, match="current decision"):
        service.get_view("plan")


def test_view_rejects_archived_revision_without_pin_release_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, revision, _output = _activated_service(tmp_path, monkeypatch)
    service.abandon(
        "plan",
        1,
        revision.plan_content_digest,
        actor="reviewer",
        reason="stop work",
    )
    service.archive(
        "plan",
        1,
        revision.plan_content_digest,
        actor="reviewer",
        reason="archive",
    )
    monkeypatch.setattr(service.pins, "active_pin_ids", lambda: set())
    monkeypatch.setattr(service.pins, "list_release_events", lambda: [])

    with pytest.raises(ChangeStoreCorrupt, match="pin release"):
        service.get_view("plan")


def test_exact_current_rejects_revision_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, revision, _output = _activated_service(tmp_path, monkeypatch)
    original = service.store.read_revision

    def forged_revision(plan_id: str, plan_revision: int) -> ChangePlanRevision:
        record = original(plan_id, plan_revision)
        return record.model_copy(update={"plan_content_digest": "forged"})

    monkeypatch.setattr(service.store, "read_revision", forged_revision)

    with pytest.raises(ChangeStoreCorrupt, match="operation binding"):
        service._exact_current("plan", 1, revision.plan_content_digest)


def test_active_pin_requirement_rejects_an_inactive_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, revision, _output = _activated_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service.pins, "active_pin_ids", lambda: set())

    with pytest.raises(ChangeStoreCorrupt, match="active pin"):
        service._require_active_pin(revision)


def test_active_pin_requirement_rejects_cross_plan_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, revision, _output = _activated_service(tmp_path, monkeypatch)
    pin = service.pins.get(revision.baseline.pin_id)
    forged = pin.model_copy(update={"plan_id": "another-plan"})
    monkeypatch.setattr(service.pins, "get", lambda _pin_id: forged)

    with pytest.raises(ChangeStoreCorrupt, match="exact revision"):
        service._require_active_pin(revision)


def test_archive_match_requires_an_immutable_archive_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, revision, _output = _activated_service(tmp_path, monkeypatch)

    with pytest.raises(ChangeStoreCorrupt, match="archive record"):
        service._matching_archive_record(revision)


def test_rejected_revision_cannot_be_reapproved_and_abandon_retains_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    _write_build(output)
    monkeypatch.setattr(
        "arcgraph.change.service.validate_baseline_reference", lambda *args: None
    )
    service = ChangeSafetyService(tmp_path, output, repo_id="repo")
    revision = _revision()
    activated = service.activate_revision(revision)
    assert activated.plan_status == "ready"
    assert activated.pin_projection["active"]

    rejected = service.reject(
        "plan",
        1,
        revision.plan_content_digest,
        actor="reviewer",
        reason="not ready for this change",
    )
    assert rejected.plan_decision is not None
    assert rejected.plan_decision.status == "rejected"
    assert rejected.plan_status == "ready"
    assert rejected.pin_projection["active"]
    with pytest.raises(ChangePlanStateError):
        service.approve(
            "plan",
            1,
            revision.plan_content_digest,
            actor="reviewer",
            reason="cannot reverse rejection",
        )
    with pytest.raises(ChangePlanStateError):
        service.verify("plan", 1, revision.plan_content_digest)
    assert service.get_view("plan").plan_status == "ready"
    assert service.store.list_verification_reports("plan") == []

    abandoned = service.abandon(
        "plan",
        1,
        revision.plan_content_digest,
        actor="reviewer",
        reason="stop work",
    )
    assert abandoned.plan_status == "abandoned"
    assert abandoned.pin_projection["active"]

    archived = service.archive(
        "plan",
        1,
        revision.plan_content_digest,
        actor="reviewer",
        reason="record retained before pin release",
    )
    assert archived.plan_status == "archived"
    assert not archived.pin_projection["active"]


def test_archive_release_failure_stays_recoverable_and_is_not_reported_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    _write_build(output)
    monkeypatch.setattr(
        "arcgraph.change.service.validate_baseline_reference", lambda *args: None
    )
    service = ChangeSafetyService(tmp_path, output, repo_id="repo")
    revision = _revision()
    service.activate_revision(revision)
    service.abandon(
        "plan",
        1,
        revision.plan_content_digest,
        actor="reviewer",
        reason="stop work",
    )

    def fail_release(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated pin release failure")

    with monkeypatch.context() as context:
        context.setattr(service.pins, "release", fail_release)
        with pytest.raises(OSError, match="simulated pin release failure"):
            service.archive(
                "plan",
                1,
                revision.plan_content_digest,
                actor="reviewer",
                reason="archive retained record",
            )

    pending = service.get_view("plan")
    assert pending.plan_status == "archived"
    assert pending.archive_projection["status"] == "release_pending"
    assert pending.pin_projection["active"]

    archived = service.archive(
        "plan",
        1,
        revision.plan_content_digest,
        actor="reviewer",
        reason="complete retained release",
    )

    assert archived.archive_projection["status"] == "archived"
    assert not archived.pin_projection["active"]


def test_archive_refuses_to_release_a_pin_when_store_health_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, revision, _output = _activated_service(tmp_path, monkeypatch)
    service.abandon(
        "plan",
        1,
        revision.plan_content_digest,
        actor="reviewer",
        reason="stop work",
    )
    released = False

    def fail_health() -> None:
        raise ChangeStoreCorrupt("simulated unhealthy store")

    def observe_release(*_args: object, **_kwargs: object) -> None:
        nonlocal released
        released = True

    monkeypatch.setattr(service.store, "health_check", fail_health)
    monkeypatch.setattr(service.pins, "release", observe_release)

    with pytest.raises(ChangeStoreCorrupt, match="unhealthy store"):
        service.archive(
            "plan",
            1,
            revision.plan_content_digest,
            actor="reviewer",
            reason="must fail before release",
        )

    assert released is False
    assert revision.baseline.pin_id in service.pins.active_pin_ids()


def test_archive_transition_interruption_reuses_the_single_archive_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    _write_build(output)
    monkeypatch.setattr(
        "arcgraph.change.service.validate_baseline_reference", lambda *args: None
    )
    service = ChangeSafetyService(tmp_path, output, repo_id="repo")
    revision = _revision()
    service.activate_revision(revision)
    service.abandon(
        "plan",
        1,
        revision.plan_content_digest,
        actor="reviewer",
        reason="stop work",
    )
    original = service.store.append_lifecycle_event

    def interrupt_archive_transition(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated archive transition interruption")

    with monkeypatch.context() as context:
        context.setattr(
            service.store,
            "append_lifecycle_event",
            interrupt_archive_transition,
        )
        with pytest.raises(OSError, match="archive transition interruption"):
            service.archive(
                "plan",
                1,
                revision.plan_content_digest,
                actor="reviewer",
                reason="archive once",
            )

    assert len(service.store.list_archive_records("plan")) == 1
    monkeypatch.setattr(service.store, "append_lifecycle_event", original)

    archived = service.archive(
        "plan",
        1,
        revision.plan_content_digest,
        actor="another-reviewer",
        reason="retry must reuse first authority",
    )

    records = service.store.list_archive_records("plan")
    assert len(records) == 1
    assert records[0].reason == "archive once"
    assert archived.plan_status == "archived"


@pytest.mark.parametrize(
    "failure_point",
    [
        "pin_after_activation",
        "revision",
        "decision",
        "lifecycle",
        "current_pointer",
        "activation_audit",
    ],
)
def test_activation_interruptions_are_invisible_reconcilable_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    """Every durable activation boundary must recover without an unsafe owner."""

    output = tmp_path / "output"
    _write_build(output)
    monkeypatch.setattr(
        "arcgraph.change.service.validate_baseline_reference", lambda *args: None
    )
    service = ChangeSafetyService(tmp_path, output, repo_id="repo")
    revision = _revision()

    if failure_point == "pin_after_activation":
        original = service.pins.create

        def fail_after_pin(*args: object, **kwargs: object) -> object:
            original(*args, **kwargs)
            raise OSError("simulated pin activation interruption")

        monkeypatch.setattr(service.pins, "create", fail_after_pin)
    elif failure_point == "revision":
        original = service.store.write_revision

        def fail_revision(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated revision interruption")

        monkeypatch.setattr(service.store, "write_revision", fail_revision)
    elif failure_point == "decision":
        original = service.store.write_decision

        def fail_decision(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated decision interruption")

        monkeypatch.setattr(service.store, "write_decision", fail_decision)
    elif failure_point == "lifecycle":
        original = service.store.append_lifecycle_event

        def fail_lifecycle(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated lifecycle interruption")

        monkeypatch.setattr(service.store, "append_lifecycle_event", fail_lifecycle)
    elif failure_point == "current_pointer":
        original = service.store.write_current_pointer

        def fail_current_pointer(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated current pointer interruption")

        monkeypatch.setattr(
            service.store, "write_current_pointer", fail_current_pointer
        )
    else:
        original = service._audit

        def fail_audit(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated activation audit interruption")

        monkeypatch.setattr(service, "_audit", fail_audit)

    with pytest.raises(OSError, match="simulated"):
        service.activate_revision(revision)

    assert not (output / "change-safety" / "plans" / "plan" / "current.json").exists()
    reconciliation = service.reconcile_pins()
    assert "pin-1" in reconciliation["orphan_active_pin_ids"]

    if failure_point == "pin_after_activation":
        monkeypatch.setattr(service.pins, "create", original)
    elif failure_point == "revision":
        monkeypatch.setattr(service.store, "write_revision", original)
    elif failure_point == "decision":
        monkeypatch.setattr(service.store, "write_decision", original)
    elif failure_point == "lifecycle":
        monkeypatch.setattr(service.store, "append_lifecycle_event", original)
    elif failure_point == "current_pointer":
        monkeypatch.setattr(service.store, "write_current_pointer", original)
    else:
        monkeypatch.setattr(service, "_audit", original)

    recovered = service.activate_revision(revision)
    assert recovered.plan_status == "ready"
    assert recovered.pin_projection["active"]
    service.store.health_check()


def test_activation_publication_write_failure_after_replace_is_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-replace I/O error leaves a complete publication safe to replay."""

    output = tmp_path / "output"
    _write_build(output)
    monkeypatch.setattr(
        "arcgraph.change.service.validate_baseline_reference", lambda *args: None
    )
    service = ChangeSafetyService(tmp_path, output, repo_id="repo")
    revision = _revision()
    original = service.store.write_current_pointer

    def publish_then_fail(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        raise OSError("simulated post-publication fsync interruption")

    monkeypatch.setattr(service.store, "write_current_pointer", publish_then_fail)
    with pytest.raises(OSError, match="post-publication"):
        service.activate_revision(revision)

    assert (output / "change-safety" / "plans" / "plan" / "current.json").exists()
    service.store.health_check()
    reconciliation = service.reconcile_pins()
    assert reconciliation["orphan_active_pin_ids"] == []

    monkeypatch.setattr(service.store, "write_current_pointer", original)
    replayed = service.activate_revision(revision)
    assert replayed.plan_status == "ready"


def test_reconcile_pins_propagates_store_corruption_instead_of_orphaning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, revision, _output = _activated_service(tmp_path, monkeypatch)

    monkeypatch.setattr(service.store, "health_check", lambda: None)
    monkeypatch.setattr(service.pins, "health_check", lambda: None)

    def corrupt_pointer(_plan_id: str) -> object:
        raise ChangeStoreCorrupt("simulated corrupt pointer")

    monkeypatch.setattr(service.store, "read_current_pointer", corrupt_pointer)

    with pytest.raises(ChangeStoreCorrupt, match="corrupt pointer"):
        service.reconcile_pins()

    assert revision.baseline.pin_id in service.pins.active_pin_ids()
