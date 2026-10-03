"""Transactional application service for Surgical Change Safety lifecycle writes."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from arcgraph.change.baseline import (
    create_baseline_reference,
    validate_baseline_reference,
)
from arcgraph.change.contracts import (
    ArchiveRecord,
    BuildPin,
    ChangeAuditEvent,
    ChangeCurrentPointer,
    ChangeIntent,
    ChangePlanRevision,
    ChangePlanView,
    ChangeVerificationReport,
    ChangeVerificationReportView,
    EvidencePurgeEvent,
    GraphDelta,
    PlanDecision,
    PlanLifecycleEvent,
    VerificationEvidence,
    verification_requirement_summary,
)
from arcgraph.change.errors import (
    ChangePlanStateError,
    ChangeStoreCorrupt,
    ChangeStoreNotFound,
)
from arcgraph.change.evidence import (
    EvidenceIngressValidator,
    evidence_summary,
    redact_evidence_material,
)
from arcgraph.change.identities import capture_build_identity, capture_code_identity
from arcgraph.change.pins import BuildPinManager
from arcgraph.change.planner import ChangePlanner
from arcgraph.change.paths import normalize_repository_path
from arcgraph.change.store import ChangeStateStore, immutable_export_payload
from arcgraph.change.trusted_runners import TrustedRunnerRegistry
from arcgraph.change.verdicts import verdict_is_blocking
from arcgraph.change.verifier import ChangeVerifier
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.operation_lock import arcgraph_operation_lock

# The lock-free read paths (diff, preview_verify) can observe a store mid-way
# through a locked writer's update; a corrupt verdict there is retried briefly.
_LOCK_FREE_HEALTH_RETRY_SECONDS = 1.0
_LOCK_FREE_HEALTH_RETRY_INTERVAL_SECONDS = 0.05


class ChangeSafetyService:
    """Coordinates store, pin, and lifecycle mutations under one operation lock."""

    def __init__(
        self,
        repo_root: Path,
        output_dir: Path,
        *,
        repo_id: str,
        trusted_runner_registry: TrustedRunnerRegistry | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.output_dir = output_dir.resolve()
        self.repo_id = repo_id
        self.store = ChangeStateStore(self.output_dir, repo_id=repo_id)
        self.pins = BuildPinManager(self.output_dir, repo_id=repo_id)
        self.evidence_ingress = EvidenceIngressValidator(
            self.repo_root,
            self.output_dir,
            repo_id=repo_id,
            trusted_runner_registry=trusted_runner_registry,
        )

    def activate_revision(self, revision: ChangePlanRevision) -> ChangePlanView:
        """Persist a technical plan only after durable pin creation succeeds.

        Pin creation happens before the current pointer becomes visible.  If a
        later write fails, reconciliation reports the retained orphan pin; the
        inverse unsafe state (visible plan without a valid pin) is not created.
        """

        self._require_repo(revision.repo_id)
        with arcgraph_operation_lock(self.output_dir):
            self.store.health_check()
            self.pins.health_check()
            validate_baseline_reference(
                self.repo_root, self.output_dir, revision.baseline
            )
            expected_pin = BuildPin(
                repo_id=self.repo_id,
                pin_id=revision.baseline.pin_id,
                plan_id=revision.plan_id,
                plan_revision=revision.revision,
                plan_content_digest=revision.plan_content_digest,
                index_version=revision.baseline.build_identity.index_version,
                build_relative_path=revision.baseline.build_identity.build_relative_path,
            )
            if self.store.has_current_pointer(revision.plan_id):
                current = self.store.read_current_pointer(revision.plan_id)
            else:
                current = None
            if current is not None:
                if (
                    current.plan_revision != revision.revision
                    or current.plan_content_digest != revision.plan_content_digest
                    or current.pin_id != expected_pin.pin_id
                ):
                    raise ChangePlanStateError(
                        "a different revision is already the visible current plan"
                    )
                self._require_active_pin(revision)
                return self._view(revision.plan_id)
            self.pins.create(expected_pin)
            self.store.write_revision(revision)
            decision = PlanDecision(
                repo_id=self.repo_id,
                decision_id=f"decision-{uuid.uuid4().hex}",
                plan_id=revision.plan_id,
                plan_revision=revision.revision,
                plan_content_digest=revision.plan_content_digest,
                status="pending",
                actor="system",
                reason="initial local workflow decision state",
            )
            self.store.write_decision(decision)
            initial_status = (
                "ready"
                if revision.planning_verdict
                in {"PLAN_READY", "PLAN_READY_WITH_KNOWN_RISKS"}
                else "blocked"
            )
            self._ensure_initial_lifecycle(
                revision,
                initial_status=initial_status,
            )
            self._ensure_activation_audit(
                revision,
                expected_pin,
            )
            # This is the publication boundary.  All immutable authority,
            # lifecycle, decision, and audit records must exist before a
            # reader can discover the revision through current.json.
            self.store.write_current_pointer(
                ChangeCurrentPointer(
                    repo_id=self.repo_id,
                    plan_id=revision.plan_id,
                    plan_revision=revision.revision,
                    plan_content_digest=revision.plan_content_digest,
                    pin_id=expected_pin.pin_id,
                )
            )
            return self._view(revision.plan_id)

    def _ensure_initial_lifecycle(
        self,
        revision: ChangePlanRevision,
        *,
        initial_status: str,
    ) -> None:
        """Write or validate the one resumable activation lifecycle event."""

        events = self.store.read_lifecycle_events(revision.plan_id, revision.revision)
        if not events:
            self.store.append_lifecycle_event(
                PlanLifecycleEvent(
                    repo_id=self.repo_id,
                    event_id=f"lifecycle-{uuid.uuid4().hex}",
                    plan_id=revision.plan_id,
                    plan_revision=revision.revision,
                    prior_status=None,
                    next_status=initial_status,  # type: ignore[arg-type]
                    reason="initial technical planning result",
                )
            )
            return
        if len(events) != 1 or (
            events[0].prior_status is not None
            or events[0].next_status != initial_status
        ):
            raise ChangeStoreCorrupt(
                "incomplete activation has an incompatible lifecycle history"
            )

    def _ensure_activation_audit(
        self,
        revision: ChangePlanRevision,
        pin: BuildPin,
    ) -> None:
        """Make the activation audit retry-safe before publishing current.json."""

        for event in self.store.list_audit_events(revision.plan_id):
            if (
                event.event_type == "plan_activated"
                and event.plan_revision == revision.revision
                and event.subject_id == pin.pin_id
                and event.payload.get("plan_content_digest")
                == revision.plan_content_digest
            ):
                return
        self._audit(
            "plan_activated",
            plan_id=revision.plan_id,
            revision=revision.revision,
            subject_id=pin.pin_id,
            reason="revision and baseline pin activated",
            payload={"plan_content_digest": revision.plan_content_digest},
        )

    def plan(
        self,
        intent: ChangeIntent,
        *,
        plan_id: str | None = None,
        revision: int = 1,
        pin_id: str,
        openapi_input: str | Path | None = None,
    ) -> ChangePlanView:
        """Create and activate an exact plan through the domain boundary.

        Interfaces provide explicit input only.  Baseline capture, graph target
        resolution, planner semantics, and durable activation remain below this
        service method so neither CLI nor MCP can implement divergent rules.
        """

        revision_record = self.preview_plan(
            intent,
            plan_id=plan_id,
            revision=revision,
            pin_id=pin_id,
            openapi_input=openapi_input,
        )
        return self.activate_revision(revision_record)

    def preview_plan(
        self,
        intent: ChangeIntent,
        *,
        plan_id: str | None = None,
        revision: int = 1,
        pin_id: str,
        openapi_input: str | Path | None = None,
    ) -> ChangePlanRevision:
        """Compute a plan without publishing a pin, revision, or pointer.

        MCP uses this method for previews.  It is deliberately unable to leave
        any plan or pin state behind; durable activation remains in ``plan``.
        """

        self._require_repo(intent.repo_id)
        _validate_intent_paths(intent, self.repo_root)
        if openapi_input is not None:
            normalize_repository_path(openapi_input, self.repo_root)
        baseline = create_baseline_reference(
            self.repo_root,
            self.output_dir,
            repo_id=self.repo_id,
            pin_id=pin_id,
        )
        return ChangePlanner(
            self.repo_root,
            repo_id=self.repo_id,
        ).plan(
            intent,
            baseline,
            GraphStoreReader.from_current(self.output_dir).iter_nodes(),
            plan_id=plan_id,
            revision=revision,
            openapi_input=openapi_input,
        )

    def approve(
        self,
        plan_id: str,
        revision: int,
        plan_content_digest: str,
        *,
        actor: str,
        reason: str,
    ) -> ChangePlanView:
        with arcgraph_operation_lock(self.output_dir):
            self.store.health_check()
            self.pins.health_check()
            record, status, decision = self._exact_current(
                plan_id, revision, plan_content_digest
            )
            if status != "ready":
                raise ChangePlanStateError(
                    "only a technically ready revision can be approved"
                )
            self._require_active_pin(record)
            if decision is None or decision.status != "pending":
                raise ChangePlanStateError("only a pending decision can be approved")
            self.store.write_decision(
                PlanDecision(
                    repo_id=self.repo_id,
                    decision_id=f"decision-{uuid.uuid4().hex}",
                    plan_id=record.plan_id,
                    plan_revision=record.revision,
                    plan_content_digest=record.plan_content_digest,
                    status="approved",
                    actor=actor,
                    reason=reason,
                )
            )
            self._audit(
                "plan_approved",
                plan_id=plan_id,
                revision=revision,
                reason=reason,
            )
            return self._view(plan_id)

    def reject(
        self,
        plan_id: str,
        revision: int,
        plan_content_digest: str,
        *,
        actor: str,
        reason: str,
    ) -> ChangePlanView:
        with arcgraph_operation_lock(self.output_dir):
            self.store.health_check()
            self.pins.health_check()
            record, status, decision = self._exact_current(
                plan_id, revision, plan_content_digest
            )
            if status not in {"draft", "blocked", "ready"}:
                raise ChangePlanStateError("this revision can no longer be rejected")
            if decision is None or decision.status != "pending":
                raise ChangePlanStateError("only a pending decision can be rejected")
            self.store.write_decision(
                PlanDecision(
                    repo_id=self.repo_id,
                    decision_id=f"decision-{uuid.uuid4().hex}",
                    plan_id=record.plan_id,
                    plan_revision=record.revision,
                    plan_content_digest=record.plan_content_digest,
                    status="rejected",
                    actor=actor,
                    reason=reason,
                )
            )
            # Rejection is an authorization decision, not a technical
            # lifecycle transition.  A ready revision remains technically
            # ready but is no longer eligible for formal verification because
            # its exact Decision is rejected.  This preserves the required
            # separation of PlanDecision from PlanStatus.
            self._audit(
                "plan_rejected", plan_id=plan_id, revision=revision, reason=reason
            )
            return self._view(plan_id)

    def abandon(
        self,
        plan_id: str,
        revision: int,
        plan_content_digest: str,
        *,
        actor: str,
        reason: str,
    ) -> ChangePlanView:
        del actor  # audit tags are attached through the reason in phase one.
        with arcgraph_operation_lock(self.output_dir):
            self.store.health_check()
            self.pins.health_check()
            record, status, _decision = self._exact_current(
                plan_id, revision, plan_content_digest
            )
            self._require_active_pin(record)
            if status not in {"draft", "blocked", "ready"}:
                raise ChangePlanStateError(
                    "only draft, blocked, or ready revisions can be abandoned"
                )
            self._transition(
                record, prior=status, next_status="abandoned", reason=reason
            )
            # Abandon intentionally retains the pin; only a successful archive
            # may begin pin release.
            self._audit(
                "plan_abandoned", plan_id=plan_id, revision=revision, reason=reason
            )
            return self._view(plan_id)

    def archive(
        self,
        plan_id: str,
        revision: int,
        plan_content_digest: str,
        *,
        actor: str,
        reason: str,
    ) -> ChangePlanView:
        with arcgraph_operation_lock(self.output_dir):
            self.store.health_check()
            self.pins.health_check()
            record, status, _decision = self._exact_current(
                plan_id, revision, plan_content_digest
            )
            if status == "archived":
                archive = self._matching_archive_record(record)
                if record.baseline.pin_id not in self.pins.active_pin_ids():
                    return self._view(plan_id)
                validate_baseline_reference(
                    self.repo_root, self.output_dir, record.baseline
                )
                self._release_archived_pin(record, archive)
                self._audit(
                    "plan_archive_release_completed",
                    plan_id=plan_id,
                    revision=revision,
                    reason=f"recovered:{archive.archive_id}",
                )
                return self._view(plan_id)
            if status not in {"verified", "failed", "abandoned"}:
                raise ChangePlanStateError(
                    "only verified, failed, or abandoned revisions can be archived"
                )
            self._require_active_pin(record)
            validate_baseline_reference(
                self.repo_root, self.output_dir, record.baseline
            )
            archive = self._archive_record_for_retry(record)
            if archive is None:
                archive = ArchiveRecord(
                    repo_id=self.repo_id,
                    archive_id=f"archive-{uuid.uuid4().hex}",
                    plan_id=plan_id,
                    plan_revision=revision,
                    plan_content_digest=record.plan_content_digest,
                    status_before_archive=status,
                    actor=actor,
                    reason=reason,
                )
                self.store.write_archive_record(archive)
            self._transition(
                record,
                prior=status,
                next_status="archived",
                reason=archive.reason,
            )
            # The archive record and lifecycle event are durable before any
            # release.  A release failure leaves the pin active, which is safe.
            self._release_archived_pin(record, archive)
            self._audit(
                "plan_archived",
                plan_id=plan_id,
                revision=revision,
                reason=archive.reason,
            )
            return self._view(plan_id)

    def record_evidence(
        self,
        evidence: VerificationEvidence,
        *,
        material: dict[str, Any] | None = None,
    ) -> VerificationEvidence:
        """Persist evidence only through the service's locked ingress boundary."""

        self._require_repo(evidence.repo_id)
        with arcgraph_operation_lock(self.output_dir):
            self.store.health_check()
            self.pins.health_check()
            record, _status, _decision = self._exact_current(
                evidence.plan_id,
                evidence.plan_revision,
                evidence.plan_content_digest,
            )
            self._require_active_pin(record)
            sanitized = self.evidence_ingress.validate(
                evidence,
                record,
                material=material,
            )
            self.store.write_evidence(
                sanitized,
                material=redact_evidence_material(material),
            )
            self._audit(
                "evidence_recorded",
                plan_id=evidence.plan_id,
                revision=evidence.plan_revision,
                subject_id=sanitized.evidence_id,
                reason="exact evidence ingress binding validated",
            )
            return sanitized

    def record_evidence_submission(
        self,
        *,
        evidence_id: str,
        verification_requirement_id: str,
        plan_id: str,
        plan_revision: int,
        plan_content_digest: str,
        result: str,
        attestation_level: str,
        producer_identity: str,
        artifact_digest: str,
        stdout_digest: str | None = None,
        stderr_digest: str | None = None,
        runner_metadata: dict[str, Any] | None = None,
        material: dict[str, Any] | None = None,
    ) -> VerificationEvidence:
        """Capture actual CodeIdentity before accepting a CLI evidence payload.

        The interface supplies declared evidence attributes only.  Current build
        and code identity construction remain in the service, and
        ``record_evidence`` re-captures them under the operation lock before an
        immutable record can become visible.
        """

        current_build = capture_build_identity(
            self.repo_root,
            self.output_dir,
            repo_id=self.repo_id,
        )
        source_paths = [
            record.path
            for record in GraphStoreReader.from_current(self.output_dir).iter_files()
        ]
        evidence = VerificationEvidence(
            repo_id=self.repo_id,
            evidence_id=evidence_id,
            verification_requirement_id=verification_requirement_id,
            plan_id=plan_id,
            plan_revision=plan_revision,
            plan_content_digest=plan_content_digest,
            result=result,  # type: ignore[arg-type]
            current_code_identity=capture_code_identity(
                self.repo_root,
                repo_id=self.repo_id,
                build_identity=current_build,
                source_paths=source_paths,
            ),
            attestation_level=attestation_level,  # type: ignore[arg-type]
            producer_identity=producer_identity,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            runner_metadata=runner_metadata or {},
            artifact_digest=artifact_digest,
        )
        return self.record_evidence(evidence, material=material)

    def verify(
        self,
        plan_id: str,
        revision: int,
        plan_content_digest: str,
    ) -> ChangeVerificationReport:
        """Persist one exact verification report and its terminal lifecycle state."""

        with arcgraph_operation_lock(self.output_dir):
            self.store.health_check()
            self.pins.health_check()
            record, status, decision = self._exact_current(
                plan_id,
                revision,
                plan_content_digest,
            )
            if status != "ready":
                raise ChangePlanStateError(
                    "only a technically ready revision can be verified"
                )
            self._require_active_pin(record)
            evidence = self.store.list_evidence(plan_id)
            availability = {
                item.evidence_id: self.store.evidence_material_projection(
                    plan_id, item.evidence_id
                )["availability"]
                for item in evidence
            }
            report = self._verifier().verify(
                record,
                decision,
                evidence=evidence,
                material_availability=availability,
            )
            self.store.write_verification_report(report)
            next_status = (
                "failed" if verdict_is_blocking(report.verdict) else "verified"
            )
            self._transition(
                record,
                prior=status,
                next_status=next_status,
                reason=f"verification:{report.verification_id}:{report.verdict}",
            )
            self._audit(
                "plan_verified",
                plan_id=plan_id,
                revision=revision,
                subject_id=report.verification_id,
                reason=report.verdict,
            )
            return report

    def _lock_free_health_check(self) -> None:
        """Run the store health check without the operation lock.

        A concurrent writer holding the lock can leave the store momentarily
        between consistent states (an atomic write's temporary file, a record
        written before its pointer), which the check reports as corrupt.  Such
        a verdict is retried briefly; corruption that persists is raised.
        """

        deadline = time.monotonic() + _LOCK_FREE_HEALTH_RETRY_SECONDS
        while True:
            try:
                self.store.health_check()
                return
            except ChangeStoreCorrupt:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_LOCK_FREE_HEALTH_RETRY_INTERVAL_SECONDS)

    def diff(
        self,
        plan_id: str,
        revision: int,
        plan_content_digest: str,
    ) -> GraphDelta:
        """Return a current graph delta without a lifecycle mutation.

        Runs without the operation lock; a corrupt verdict caused by a
        concurrent write is retried briefly before it is reported.
        """

        self._lock_free_health_check()
        record, _status, _decision = self._exact_current(
            plan_id,
            revision,
            plan_content_digest,
        )
        return self._verifier().graph_delta(record)

    def preview_verify(
        self,
        plan_id: str,
        revision: int,
        plan_content_digest: str,
    ) -> ChangeVerificationReport:
        """Compute a verification result without persisting it or transitioning.

        This is the MCP-safe counterpart to ``verify``.  It deliberately shares
        the exact verifier and evidence availability projection but performs no
        state-store writes and does not acquire a mutation lock.  A corrupt
        verdict caused by a concurrent write is retried briefly before it is
        reported.
        """

        self._lock_free_health_check()
        record, _status, decision = self._exact_current(
            plan_id,
            revision,
            plan_content_digest,
        )
        evidence = self.store.list_evidence(plan_id)
        availability = {
            item.evidence_id: self.store.evidence_material_projection(
                plan_id, item.evidence_id
            )["availability"]
            for item in evidence
        }
        return self._verifier().verify(
            record,
            decision,
            evidence=evidence,
            material_availability=availability,
        )

    def purge_evidence(
        self,
        event: EvidencePurgeEvent,
    ) -> dict[str, Any]:
        self._require_repo(event.repo_id)
        with arcgraph_operation_lock(self.output_dir):
            self.store.health_check()
            self.pins.health_check()
            self._exact_current(
                event.plan_id,
                event.plan_revision,
                self.store.read_evidence(
                    event.plan_id, event.evidence_id
                ).plan_content_digest,
            )
            path = self.store.purge_evidence_material(event)
            return {"status": "purged", "purge_event_path": str(path)}

    def reconcile_pins(self) -> dict[str, Any]:
        with arcgraph_operation_lock(self.output_dir):
            self.store.health_check()
            self.pins.health_check()

            def plan_exists(pin: BuildPin) -> bool:
                try:
                    pointer = self.store.read_current_pointer(pin.plan_id)
                    decision = self.store.read_current_decision(pin.plan_id)
                    events = self.store.read_lifecycle_events(
                        pin.plan_id, pin.plan_revision
                    )
                    audit_events = self.store.list_audit_events(pin.plan_id)
                except ChangeStoreNotFound:
                    return False
                return (
                    pointer.plan_revision == pin.plan_revision
                    and pointer.plan_content_digest == pin.plan_content_digest
                    and pointer.pin_id == pin.pin_id
                    and decision is not None
                    and decision.plan_revision == pin.plan_revision
                    and decision.plan_content_digest == pin.plan_content_digest
                    and bool(events)
                    and any(
                        event.event_type == "plan_activated"
                        and event.plan_revision == pin.plan_revision
                        and event.subject_id == pin.pin_id
                        and event.payload.get("plan_content_digest")
                        == pin.plan_content_digest
                        for event in audit_events
                    )
                )

            return self.pins.reconcile(plan_exists_fn=plan_exists)

    def audit_export(self, export_id: str) -> Path:
        with arcgraph_operation_lock(self.output_dir):
            self.store.health_check()
            self.pins.health_check()
            records = []
            for plan_id in self.store.list_plan_ids():
                revisions = self.store.list_revisions(plan_id)
                records.extend(revisions)
                records.extend(self.store.list_decisions(plan_id))
                records.extend(self.store.list_evidence(plan_id))
                records.extend(self.store.list_purge_events(plan_id))
                records.extend(self.store.list_archive_records(plan_id))
                records.extend(self.store.list_verification_reports(plan_id))
                for revision in revisions:
                    records.extend(
                        self.store.read_lifecycle_events(plan_id, revision.revision)
                    )
            records.extend(self.store.list_audit_events())
            records.extend(self.pins.list_records())
            records.extend(self.pins.list_release_events())
            payload = immutable_export_payload(
                repo_id=self.repo_id,
                export_id=export_id,
                records=records,
            )
            return self.store.write_export(export_id, payload)

    def list_evidence(self, plan_id: str) -> list[VerificationEvidence]:
        return self.store.list_evidence(plan_id)

    def get_evidence(self, plan_id: str, evidence_id: str) -> VerificationEvidence:
        return self.store.read_evidence(plan_id, evidence_id)

    def evidence_material_availability(self, plan_id: str, evidence_id: str) -> str:
        """Return only the dynamic availability projection for one record."""

        return str(
            self.store.evidence_material_projection(plan_id, evidence_id)[
                "availability"
            ]
        )

    def get_verification_report(
        self,
        plan_id: str,
        verification_id: str,
    ) -> ChangeVerificationReport:
        """Return the immutable verification record exactly as it was written."""

        return self.store.read_verification_report(plan_id, verification_id)

    def get_verification_report_view(
        self,
        plan_id: str,
        verification_id: str,
    ) -> ChangeVerificationReportView:
        """Return a current Evidence availability projection for one report.

        This deliberately does not rewrite the immutable report.  It lets a
        historical report show Purged/Redacted evidence after retention actions
        while preserving its original decision-time evidence summary.
        """

        report = self.get_verification_report(plan_id, verification_id)
        availability = {
            evidence_id: self.store.evidence_material_projection(plan_id, evidence_id)[
                "availability"
            ]
            for evidence_id in report.evidence_ids
        }
        report_evidence = [
            self.store.read_evidence(plan_id, evidence_id)
            for evidence_id in report.evidence_ids
        ]
        return ChangeVerificationReportView(
            repo_id=self.repo_id,
            report=report,
            evidence_availability=availability,
            evidence_summary=evidence_summary(report_evidence, availability),
        )

    def get_view(self, plan_id: str) -> ChangePlanView:
        return self._view(plan_id)

    def list_views(self) -> list[ChangePlanView]:
        return [self._view(plan_id) for plan_id in self.store.list_plan_ids()]

    def _view(self, plan_id: str) -> ChangePlanView:
        pointer = self.store.read_current_pointer(plan_id)
        revision = self.store.read_revision(plan_id, pointer.plan_revision)
        if revision.plan_content_digest != pointer.plan_content_digest:
            raise ChangeStoreCorrupt("current pointer digest does not match revision")
        decision = self.store.read_current_decision(plan_id)
        if decision and (
            decision.plan_revision != revision.revision
            or decision.plan_content_digest != revision.plan_content_digest
        ):
            raise ChangeStoreCorrupt(
                "current decision does not bind the current revision"
            )
        events = self.store.read_lifecycle_events(plan_id, revision.revision)
        status = events[-1].next_status if events else "draft"
        evidence = self.store.list_evidence(plan_id)
        availability: dict[str, str] = {}
        for item in evidence:
            availability[item.evidence_id] = self.store.evidence_material_projection(
                plan_id, item.evidence_id
            )["availability"]
        active_pin_ids = self.pins.active_pin_ids()
        pin_active = pointer.pin_id in active_pin_ids
        archive_projection: dict[str, Any] = {"status": "active"}
        if status == "archived":
            archive = self._matching_archive_record(revision)
            if pin_active:
                archive_projection = {
                    "status": "release_pending",
                    "archive_id": archive.archive_id,
                }
            elif not any(
                event.pin_id == pointer.pin_id
                and event.plan_id == plan_id
                and event.plan_revision == revision.revision
                for event in self.pins.list_release_events()
            ):
                raise ChangeStoreCorrupt(
                    "archived revision has no matching immutable pin release"
                )
            else:
                archive_projection = {
                    "status": "archived",
                    "archive_id": archive.archive_id,
                }
        return ChangePlanView(
            repo_id=self.repo_id,
            plan_id=plan_id,
            revision=revision.revision,
            plan_revision=revision,
            plan_status=status,
            plan_decision=decision,
            verification_summary=verification_requirement_summary(
                revision.verification_plan
            ),
            evidence_summary=evidence_summary(evidence, availability),
            archive_projection=archive_projection,
            pin_projection={
                "pin_id": pointer.pin_id,
                "active": pin_active,
            },
        )

    def _exact_current(
        self,
        plan_id: str,
        revision: int,
        plan_content_digest: str,
    ) -> tuple[ChangePlanRevision, str, PlanDecision | None]:
        pointer = self.store.read_current_pointer(plan_id)
        if (
            pointer.plan_revision != revision
            or pointer.plan_content_digest != plan_content_digest
        ):
            raise ChangePlanStateError(
                "operation must bind the exact current revision and digest"
            )
        record = self.store.read_revision(plan_id, revision)
        if record.plan_content_digest != plan_content_digest:
            raise ChangeStoreCorrupt("revision digest does not match operation binding")
        events = self.store.read_lifecycle_events(plan_id, revision)
        status = events[-1].next_status if events else "draft"
        decision = self.store.read_current_decision(plan_id)
        return record, status, decision

    def _require_active_pin(self, record: ChangePlanRevision) -> BuildPin:
        pin = self.pins.get(record.baseline.pin_id)
        if pin.pin_id not in self.pins.active_pin_ids():
            raise ChangeStoreCorrupt("current revision does not retain an active pin")
        if (
            pin.plan_id != record.plan_id
            or pin.plan_revision != record.revision
            or pin.plan_content_digest != record.plan_content_digest
            or pin.index_version != record.baseline.build_identity.index_version
        ):
            raise ChangeStoreCorrupt("active pin does not bind the exact revision")
        return pin

    def _matching_archive_record(self, record: ChangePlanRevision) -> ArchiveRecord:
        archive = self._archive_record_for_retry(record)
        if archive is None:
            raise ChangeStoreCorrupt(
                "archived revision does not have an immutable archive record"
            )
        return archive

    def _archive_record_for_retry(
        self,
        record: ChangePlanRevision,
    ) -> ArchiveRecord | None:
        records = [
            archive
            for archive in self.store.list_archive_records(record.plan_id)
            if archive.plan_revision == record.revision
            and archive.plan_content_digest == record.plan_content_digest
        ]
        if len(records) > 1:
            raise ChangeStoreCorrupt("revision has multiple immutable archive records")
        return records[0] if records else None

    def _release_archived_pin(
        self,
        record: ChangePlanRevision,
        archive: ArchiveRecord,
    ) -> None:
        """Tie the release authorization to fresh store and pin health checks."""

        self.store.health_check()
        self.pins.health_check()
        self.pins.release(
            record.baseline.pin_id,
            reason=f"archived:{archive.archive_id}",
            store_healthy=True,
        )

    def _verifier(self) -> ChangeVerifier:
        return ChangeVerifier(
            self.repo_root,
            self.output_dir,
            repo_id=self.repo_id,
            trusted_runner_registry=self.evidence_ingress.trusted_runner_registry,
        )

    def _transition(
        self,
        record: ChangePlanRevision,
        *,
        prior: str,
        next_status: str,
        reason: str,
    ) -> None:
        self.store.append_lifecycle_event(
            PlanLifecycleEvent(
                repo_id=self.repo_id,
                event_id=f"lifecycle-{uuid.uuid4().hex}",
                plan_id=record.plan_id,
                plan_revision=record.revision,
                prior_status=prior,  # type: ignore[arg-type]
                next_status=next_status,  # type: ignore[arg-type]
                reason=reason,
            )
        )

    def _audit(
        self,
        event_type: str,
        *,
        plan_id: str,
        revision: int,
        subject_id: str | None = None,
        reason: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.store.append_audit_event(
            ChangeAuditEvent(
                repo_id=self.repo_id,
                audit_event_id=f"audit-{uuid.uuid4().hex}",
                event_type=event_type,
                plan_id=plan_id,
                plan_revision=revision,
                subject_id=subject_id,
                reason=reason,
                payload=payload or {},
            )
        )

    def _require_repo(self, repo_id: str) -> None:
        if repo_id != self.repo_id:
            raise ChangeStoreCorrupt("service request repo_id does not match")


def _validate_intent_paths(intent: ChangeIntent, repo_root: Path) -> None:
    """Reject path-like interface inputs that escape the analyzed repository."""

    for target in intent.targets:
        if target.kind == "path":
            normalize_repository_path(target.value, repo_root)
    for declaration in intent.user_declared_protected_surfaces:
        if _looks_like_declared_path(declaration):
            normalize_repository_path(declaration, repo_root)


def _looks_like_declared_path(value: str) -> bool:
    if not value or ":" in value:
        return False
    return (
        "/" in value
        or "\\" in value
        or value.endswith((".py", ".pyi", ".ts", ".tsx", ".js", ".json"))
        or value.startswith((".", ".."))
    )
