"""Output-relative, atomic state store for Change Safety records."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from arcgraph.change.contracts import (
    ArchiveRecord,
    CHANGE_CONTRACT_VERSION,
    ChangeAuditEvent,
    ChangeCurrentPointer,
    ChangePlanRevision,
    ChangeVerificationReport,
    DecisionCurrentPointer,
    EvidencePurgeEvent,
    PlanDecision,
    PlanLifecycleEvent,
    VerificationEvidence,
    canonical_digest,
)
from arcgraph.change.errors import (
    ChangeSafetyError,
    ChangeStoreCorrupt,
    ChangeStoreNotFound,
)
from arcgraph.change.evidence import redact_sensitive_text
from arcgraph.change.paths import ensure_contained_path, resolve_under_root
from arcgraph.core.schemas import SCHEMA_VERSION
from arcgraph.core.sharing_retry import (
    is_regular_file,
    path_exists,
    retry_sharing_violation,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


def atomic_write_json(path: Path, payload: dict[str, Any], *, root: Path) -> None:
    """Write one JSON document atomically, after proving output containment."""

    contained = ensure_contained_path(root, path)
    parent = ensure_contained_path(root, contained.parent)
    parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    fd, raw_temp = tempfile.mkstemp(prefix=".change-safety-", suffix=".tmp", dir=parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        ensure_contained_path(root, temp_path)
        retry_sharing_violation(lambda: os.replace(temp_path, contained))
        _fsync_directory(parent)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def read_json_object(path: Path, *, root: Path) -> dict[str, Any]:
    contained = ensure_contained_path(root, path)
    try:
        # pathlib answers "missing" for absent paths but raises other errors,
        # such as a parent directory that denies search permission.  Store
        # paths reject symlinks when they are named, so is_symlink() only
        # catches one created since.
        present = not contained.is_symlink() and is_regular_file(contained)
    except OSError as exc:
        raise ChangeStoreCorrupt(
            f"cannot inspect JSON state record {contained.name}"
        ) from exc
    if not present:
        raise ChangeStoreNotFound(f"state record does not exist: {contained.name}")
    try:
        payload = json.loads(
            retry_sharing_violation(lambda: contained.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ChangeStoreCorrupt(
            f"cannot read JSON state record {contained.name}"
        ) from exc
    if not isinstance(payload, dict):
        raise ChangeStoreCorrupt(f"state record {contained.name} is not a JSON object")
    return payload


class ChangeStateStore:
    """Store immutable records plus mutable pointers under ``output/change-safety``.

    Callers must hold ``arcgraph_operation_lock(output_dir)`` across a compound
    mutation.  The store's atomic writes protect individual documents; the
    service owns the multi-document transaction ordering and lifecycle rules.
    """

    def __init__(self, output_dir: Path, *, repo_id: str) -> None:
        self.output_dir = output_dir.resolve()
        self.repo_id = repo_id
        self.root = resolve_under_root(self.output_dir, "change-safety")

    def write_revision(self, revision: ChangePlanRevision) -> Path:
        self._require_repo(revision.repo_id)
        path = self.revision_path(revision.plan_id, revision.revision)
        if path.exists():
            existing = self.read_revision(revision.plan_id, revision.revision)
            if existing.plan_content_digest != revision.plan_content_digest:
                raise ChangeStoreCorrupt(
                    "immutable revision path already has another digest"
                )
            return path
        self._write_model(path, revision)
        return path

    def read_revision(self, plan_id: str, revision: int) -> ChangePlanRevision:
        record = self._read_model(
            self.revision_path(plan_id, revision), ChangePlanRevision
        )
        if record.plan_id != plan_id or record.revision != revision:
            raise ChangeStoreCorrupt(
                "revision record does not match its plan directory or file name"
            )
        return record

    def list_revisions(self, plan_id: str) -> list[ChangePlanRevision]:
        directory = self._path("plans", plan_id, "revisions")
        paths = self._json_record_paths(directory, label="revision")
        revisions = [self._read_model(path, ChangePlanRevision) for path in paths]
        for path, revision in zip(paths, revisions, strict=True):
            if (
                revision.plan_id != plan_id
                or not path.stem.isdecimal()
                or path.name != f"{revision.revision}.json"
            ):
                raise ChangeStoreCorrupt(
                    "revision record does not match its plan directory or file name"
                )
        return revisions

    def list_plan_ids(self) -> list[str]:
        directory = self._path("plans")
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise ChangeStoreCorrupt("plans directory is unsafe")
        ids: list[str] = []
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_dir():
                raise ChangeStoreCorrupt("plans directory contains an unsafe entry")
            try:
                self._path("plans", path.name)
            except ChangeSafetyError as exc:
                raise ChangeStoreCorrupt(
                    "plans directory contains an unsafe plan identifier"
                ) from exc
            ids.append(path.name)
        return sorted(ids)

    def write_current_pointer(self, pointer: ChangeCurrentPointer) -> Path:
        self._require_repo(pointer.repo_id)
        path = self._path("plans", pointer.plan_id, "current.json")
        self._write_model(path, pointer)
        return path

    def read_current_pointer(self, plan_id: str) -> ChangeCurrentPointer:
        pointer = self._read_model(
            self._path("plans", plan_id, "current.json"), ChangeCurrentPointer
        )
        if pointer.plan_id != plan_id:
            raise ChangeStoreCorrupt(
                "current pointer does not match its plan directory"
            )
        return pointer

    def has_current_pointer(self, plan_id: str) -> bool:
        """Return whether the mutable current pointer has been published."""

        return self._path("plans", plan_id, "current.json").exists()

    def append_lifecycle_event(self, event: PlanLifecycleEvent) -> Path:
        self._require_repo(event.repo_id)
        path = self._path(
            "plans",
            event.plan_id,
            "lifecycle",
            f"{event.event_id}.json",
        )
        self._write_immutable_model(path, event)
        return path

    def read_lifecycle_events(
        self, plan_id: str, revision: int
    ) -> list[PlanLifecycleEvent]:
        return [
            event
            for event in self._list_lifecycle_events(plan_id)
            if event.plan_revision == revision
        ]

    def _list_lifecycle_events(self, plan_id: str) -> list[PlanLifecycleEvent]:
        directory = self._path("plans", plan_id, "lifecycle")
        paths = self._json_record_paths(directory, label="lifecycle event")
        events = [self._read_model(path, PlanLifecycleEvent) for path in paths]
        for path, event in zip(paths, events, strict=True):
            if event.plan_id != plan_id or path.name != f"{event.event_id}.json":
                raise ChangeStoreCorrupt(
                    "lifecycle event does not match its plan directory or file name"
                )
        return sorted(
            events,
            key=lambda event: (event.recorded_at, event.event_id),
        )

    def write_decision(self, decision: PlanDecision) -> Path:
        self._require_repo(decision.repo_id)
        existing = [
            record
            for record in self.list_decisions(decision.plan_id)
            if record.plan_revision == decision.plan_revision
            and record.plan_content_digest == decision.plan_content_digest
        ]
        pending = [record for record in existing if record.status == "pending"]
        terminal = [record for record in existing if record.status != "pending"]
        selected = decision
        if decision.status == "pending":
            if not existing:
                self._write_immutable_model(
                    self._path(
                        "plans",
                        decision.plan_id,
                        "decisions",
                        f"{decision.decision_id}.json",
                    ),
                    decision,
                )
            elif len(pending) == 1 and not terminal:
                selected = pending[0]
            else:
                raise ChangeStoreCorrupt(
                    "decision history does not permit another pending decision"
                )
        elif terminal:
            if (
                len(pending) != 1
                or len(terminal) != 1
                or terminal[0].status != decision.status
            ):
                raise ChangeStoreCorrupt(
                    "decision history does not permit another terminal decision"
                )
            selected = terminal[0]
        elif len(pending) == 1:
            self._write_immutable_model(
                self._path(
                    "plans",
                    decision.plan_id,
                    "decisions",
                    f"{decision.decision_id}.json",
                ),
                decision,
            )
        else:
            raise ChangeStoreCorrupt(
                "terminal decision requires exactly one pending decision"
            )
        path = self._path(
            "plans",
            selected.plan_id,
            "decisions",
            f"{selected.decision_id}.json",
        )
        self._write_model(
            self._path("plans", selected.plan_id, "decision-current.json"),
            DecisionCurrentPointer(
                repo_id=self.repo_id,
                plan_id=selected.plan_id,
                plan_revision=selected.plan_revision,
                plan_content_digest=selected.plan_content_digest,
                decision_id=selected.decision_id,
            ),
        )
        return path

    def read_current_decision(self, plan_id: str) -> PlanDecision | None:
        pointer_path = self._path("plans", plan_id, "decision-current.json")
        # The pointer is replaced in place, so read it rather than trusting an
        # exists() check that can miss it on Windows during the replace.
        try:
            pointer = self._read_model(pointer_path, DecisionCurrentPointer)
        except ChangeStoreNotFound:
            # Only a pointer that is really absent means "no decision";
            # anything else occupying its place stays an error.  _path has
            # already rejected a symlink in that place.
            if pointer_path.exists():
                raise
            return None
        if pointer.plan_id != plan_id:
            raise ChangeStoreCorrupt(
                "decision pointer does not match its plan directory"
            )
        decision = self._read_model(
            self._path(
                "plans",
                plan_id,
                "decisions",
                f"{pointer.decision_id}.json",
            ),
            PlanDecision,
        )
        if (
            pointer.plan_id != plan_id
            or decision.plan_id != plan_id
            or decision.plan_revision != pointer.plan_revision
            or decision.plan_content_digest != pointer.plan_content_digest
        ):
            raise ChangeStoreCorrupt(
                "decision pointer does not match its immutable decision"
            )
        return decision

    def list_decisions(self, plan_id: str) -> list[PlanDecision]:
        directory = self._path("plans", plan_id, "decisions")
        paths = self._json_record_paths(directory, label="decision")
        decisions = [self._read_model(path, PlanDecision) for path in paths]
        for path, decision in zip(paths, decisions, strict=True):
            if (
                decision.plan_id != plan_id
                or path.name != f"{decision.decision_id}.json"
            ):
                raise ChangeStoreCorrupt(
                    "decision record does not match its plan directory or file name"
                )
        return sorted(
            decisions,
            key=lambda item: (item.plan_revision, item.decided_at, item.decision_id),
        )

    def write_evidence(
        self,
        evidence: VerificationEvidence,
        *,
        material: dict[str, Any] | None = None,
    ) -> Path:
        self._require_repo(evidence.repo_id)
        record_path = self._path(
            "plans",
            evidence.plan_id,
            "evidence",
            f"{evidence.evidence_id}.json",
        )
        if record_path.exists():
            existing = self.read_evidence(evidence.plan_id, evidence.evidence_id)
            if existing.payload_digest != evidence.payload_digest:
                raise ChangeStoreCorrupt(
                    "immutable evidence id already has another payload"
                )
            return record_path
        material_payload = {
            "schema_version": SCHEMA_VERSION,
            "change_contract_version": CHANGE_CONTRACT_VERSION,
            "repo_id": self.repo_id,
            "plan_id": evidence.plan_id,
            "plan_revision": evidence.plan_revision,
            "evidence_id": evidence.evidence_id,
            "artifact_digest": evidence.artifact_digest,
            "availability": "available",
            "material": material or {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        material_path = self._evidence_material_path(evidence)
        # Material is deliberately written first.  A material-write failure
        # cannot expose a record that looks complete but has no controlled
        # material projection.  If publication fails before the immutable
        # record becomes visible, remove the unpublished material rather than
        # retaining an orphan under the authoritative state root.  A failure
        # after an atomic record replacement leaves the complete pair intact
        # for safe retry and health-check recovery.
        self._write_json(material_path, material_payload)
        try:
            self._write_immutable_model(record_path, evidence)
        except Exception:
            if not record_path.exists():
                self._remove_unpublished_material(material_path)
            raise
        return record_path

    def read_evidence(self, plan_id: str, evidence_id: str) -> VerificationEvidence:
        evidence = self._read_model(
            self._path("plans", plan_id, "evidence", f"{evidence_id}.json"),
            VerificationEvidence,
        )
        if evidence.plan_id != plan_id or evidence.evidence_id != evidence_id:
            raise ChangeStoreCorrupt(
                "evidence record does not match its plan directory or file name"
            )
        return evidence

    def list_evidence(self, plan_id: str) -> list[VerificationEvidence]:
        directory = self._path("plans", plan_id, "evidence")
        paths = self._json_record_paths(directory, label="evidence record")
        evidence = [self._read_model(path, VerificationEvidence) for path in paths]
        for path, record in zip(paths, evidence, strict=True):
            if record.plan_id != plan_id or path.name != f"{record.evidence_id}.json":
                raise ChangeStoreCorrupt(
                    "evidence record does not match its plan directory or file name"
                )
        return evidence

    def evidence_material_projection(
        self,
        plan_id: str,
        evidence_id: str,
    ) -> dict[str, Any]:
        evidence = self.read_evidence(plan_id, evidence_id)
        path = self._evidence_material_path(evidence)
        if not path.exists():
            return {"availability": "missing"}
        payload = self._read_evidence_material(evidence)
        return {
            "availability": payload["availability"],
            "updated_at": payload.get("updated_at"),
        }

    def purge_evidence_material(
        self,
        event: EvidencePurgeEvent,
    ) -> Path:
        self._require_repo(event.repo_id)
        evidence = self.read_evidence(event.plan_id, event.evidence_id)
        if evidence.plan_revision != event.plan_revision:
            raise ChangeStoreCorrupt(
                "purge revision does not match the evidence record"
            )
        if evidence.verification_requirement_id != event.requirement_id:
            raise ChangeStoreCorrupt(
                "purge requirement does not match the evidence record"
            )
        if evidence.artifact_digest != event.artifact_digest:
            raise ChangeStoreCorrupt(
                "purge artifact digest does not match the evidence record"
            )
        material_path = self._evidence_material_path(evidence)
        if not material_path.exists():
            raise ChangeStoreCorrupt("evidence material is unavailable for purge")
        material = self._read_evidence_material(evidence)
        prior_availability = material.get("availability")
        if prior_availability == "purged":
            raise ChangeStoreCorrupt("purged evidence material cannot be restored")
        if (
            prior_availability == "redacted"
            and event.purge_mode == "replace_with_redacted"
        ):
            raise ChangeStoreCorrupt("evidence material is already redacted")
        if (
            event.purge_mode == "replace_with_redacted"
            and event.redacted_material_digest != canonical_digest({"redacted": True})
        ):
            raise ChangeStoreCorrupt(
                "redacted material digest does not bind the retained projection"
            )
        availability = (
            "redacted" if event.purge_mode == "replace_with_redacted" else "purged"
        )
        self._write_json(
            material_path,
            {
                "schema_version": SCHEMA_VERSION,
                "change_contract_version": CHANGE_CONTRACT_VERSION,
                "repo_id": self.repo_id,
                "plan_id": event.plan_id,
                "plan_revision": event.plan_revision,
                "evidence_id": event.evidence_id,
                "availability": availability,
                "artifact_digest": event.artifact_digest,
                "redacted_material_digest": event.redacted_material_digest,
                "updated_at": event.recorded_at.isoformat(),
                "material": (
                    {"redacted": True}
                    if event.purge_mode == "replace_with_redacted"
                    else {}
                ),
            },
        )
        event_path = self._path(
            "plans", event.plan_id, "evidence-purges", f"{event.purge_id}.json"
        )
        self._write_immutable_model(event_path, event)
        self.append_audit_event(
            ChangeAuditEvent(
                repo_id=self.repo_id,
                audit_event_id=f"purge-{event.purge_id}",
                event_type="evidence_purged",
                plan_id=event.plan_id,
                plan_revision=event.plan_revision,
                subject_id=event.evidence_id,
                reason=event.reason,
                payload={
                    "purge_mode": event.purge_mode,
                    "availability_before": prior_availability or "missing",
                    "availability_after": availability,
                    "requirement_id": event.requirement_id,
                    "artifact_digest": event.artifact_digest,
                    "redacted_material_digest": event.redacted_material_digest,
                },
            )
        )
        return event_path

    def list_purge_events(self, plan_id: str) -> list[EvidencePurgeEvent]:
        directory = self._path("plans", plan_id, "evidence-purges")
        paths = self._json_record_paths(directory, label="evidence purge")
        events = [self._read_model(path, EvidencePurgeEvent) for path in paths]
        for path, event in zip(paths, events, strict=True):
            if event.plan_id != plan_id or path.name != f"{event.purge_id}.json":
                raise ChangeStoreCorrupt(
                    "evidence purge does not match its plan directory or file name"
                )
        return events

    def write_archive_record(self, record: ArchiveRecord) -> Path:
        self._require_repo(record.repo_id)
        path = self._path(
            "plans", record.plan_id, "archive", f"{record.archive_id}.json"
        )
        self._write_immutable_model(path, record)
        return path

    def list_archive_records(self, plan_id: str) -> list[ArchiveRecord]:
        directory = self._path("plans", plan_id, "archive")
        paths = self._json_record_paths(directory, label="archive record")
        records = [self._read_model(path, ArchiveRecord) for path in paths]
        for path, record in zip(paths, records, strict=True):
            if record.plan_id != plan_id or path.name != f"{record.archive_id}.json":
                raise ChangeStoreCorrupt(
                    "archive record does not match its plan directory or file name"
                )
        return records

    def append_audit_event(self, event: ChangeAuditEvent) -> Path:
        self._require_repo(event.repo_id)
        if not event.plan_id:
            raise ChangeStoreCorrupt("audit events must bind an exact plan")
        sequence = len(self.list_audit_events(event.plan_id)) + 1
        path = self._path(
            "plans",
            event.plan_id,
            "audit",
            "events",
            f"{sequence:020d}-{event.audit_event_id}.json",
        )
        self._write_immutable_model(path, event)
        return path

    def list_audit_events(self, plan_id: str | None = None) -> list[ChangeAuditEvent]:
        if plan_id is None:
            events: list[ChangeAuditEvent] = []
            for current_plan_id in self.list_plan_ids():
                events.extend(self.list_audit_events(current_plan_id))
            return sorted(
                events,
                key=lambda event: (
                    event.recorded_at,
                    event.plan_id or "",
                    event.audit_event_id,
                ),
            )
        directory = self._path("plans", plan_id, "audit", "events")
        paths = self._json_record_paths(directory, label="audit event")
        events = [self._read_model(path, ChangeAuditEvent) for path in paths]
        for sequence, (path, event) in enumerate(
            zip(paths, events, strict=True), start=1
        ):
            suffix = f"-{event.audit_event_id}.json"
            if event.plan_id != plan_id or not (
                path.name.endswith(suffix)
                and path.name[: -len(suffix)].isdecimal()
                and len(path.name[: -len(suffix)]) == 20
                and int(path.name[: -len(suffix)]) == sequence
            ):
                raise ChangeStoreCorrupt(
                    "audit event does not match its plan directory or file name"
                )
        return events

    def write_verification_report(
        self,
        report: ChangeVerificationReport,
    ) -> Path:
        self._require_repo(report.repo_id)
        path = self._path(
            "plans", report.plan_id, "verifications", f"{report.verification_id}.json"
        )
        self._write_immutable_model(path, report)
        return path

    def read_verification_report(
        self,
        plan_id: str,
        verification_id: str,
    ) -> ChangeVerificationReport:
        report = self._read_model(
            self._path("plans", plan_id, "verifications", f"{verification_id}.json"),
            ChangeVerificationReport,
        )
        if report.plan_id != plan_id or report.verification_id != verification_id:
            raise ChangeStoreCorrupt(
                "verification report does not match its plan directory or file name"
            )
        return report

    def list_verification_reports(self, plan_id: str) -> list[ChangeVerificationReport]:
        directory = self._path("plans", plan_id, "verifications")
        paths = self._json_record_paths(directory, label="verification report")
        reports = [self._read_model(path, ChangeVerificationReport) for path in paths]
        for path, report in zip(paths, reports, strict=True):
            if (
                report.plan_id != plan_id
                or path.name != f"{report.verification_id}.json"
            ):
                raise ChangeStoreCorrupt(
                    "verification report does not match its plan directory or file name"
                )
        return reports

    def export_path(self, export_id: str) -> Path:
        return self._path("exports", f"{export_id}.json")

    def write_export(self, export_id: str, payload: dict[str, Any]) -> Path:
        path = self.export_path(export_id)
        self._write_json(path, payload)
        return path

    def health_check(self) -> None:
        """Check pointers and directory containment before irreversible lifecycle work."""

        if not self.root.exists():
            return
        if self.root.is_symlink() or not self.root.is_dir():
            raise ChangeStoreCorrupt("change-safety store root is unsafe")
        self._validate_root_layout()
        for plan_id in self.list_plan_ids():
            self._validate_plan_layout(plan_id)
            revisions = self.list_revisions(plan_id)
            revisions_by_number = {
                revision.revision: revision for revision in revisions
            }
            decisions = self.list_decisions(plan_id)
            evidence = self.list_evidence(plan_id)
            purge_events = self.list_purge_events(plan_id)
            archives = self.list_archive_records(plan_id)
            reports = self.list_verification_reports(plan_id)
            audit_events = self.list_audit_events(plan_id)
            lifecycle_events = self._list_lifecycle_events(plan_id)
            if any(
                event.plan_revision not in revisions_by_number
                for event in lifecycle_events
            ):
                raise ChangeStoreCorrupt(
                    "lifecycle event does not bind an immutable plan revision"
                )
            for revision in revisions:
                events = [
                    event
                    for event in lifecycle_events
                    if event.plan_revision == revision.revision
                ]
                previous_status = None
                for event in events:
                    if event.prior_status != previous_status:
                        raise ChangeStoreCorrupt(
                            "lifecycle event chain does not bind the prior status"
                        )
                    previous_status = event.next_status
                if previous_status in {"verified", "failed"} and not any(
                    report.plan_revision == revision.revision for report in reports
                ):
                    raise ChangeStoreCorrupt(
                        "terminal verification status has no immutable report"
                    )
                if previous_status == "archived" and not any(
                    archive.plan_revision == revision.revision
                    and archive.plan_content_digest == revision.plan_content_digest
                    for archive in archives
                ):
                    raise ChangeStoreCorrupt(
                        "archived status has no immutable archive record"
                    )
            for decision in decisions:
                revision = revisions_by_number.get(decision.plan_revision)
                if (
                    revision is None
                    or decision.plan_content_digest != revision.plan_content_digest
                ):
                    raise ChangeStoreCorrupt(
                        "decision does not bind an immutable plan revision"
                    )
            for revision_number in revisions_by_number:
                revision_decisions = [
                    decision
                    for decision in decisions
                    if decision.plan_revision == revision_number
                ]
                if not revision_decisions:
                    continue
                pending = [
                    decision
                    for decision in revision_decisions
                    if decision.status == "pending"
                ]
                terminal = [
                    decision
                    for decision in revision_decisions
                    if decision.status != "pending"
                ]
                if len(pending) != 1 or len(terminal) > 1:
                    raise ChangeStoreCorrupt(
                        "decision history does not represent one pending-to-terminal transition"
                    )
            evidence_by_id = {record.evidence_id: record for record in evidence}
            for record in evidence:
                revision = revisions_by_number.get(record.plan_revision)
                if (
                    revision is None
                    or record.plan_content_digest != revision.plan_content_digest
                    or record.verification_requirement_id
                    not in {
                        requirement.requirement_id
                        for requirement in revision.verification_plan.requirements
                    }
                ):
                    raise ChangeStoreCorrupt(
                        "evidence does not bind an immutable plan requirement"
                    )
            self._validate_evidence_material_tree(plan_id, evidence)
            for event in purge_events:
                record = evidence_by_id.get(event.evidence_id)
                if (
                    record is None
                    or event.plan_revision != record.plan_revision
                    or event.requirement_id != record.verification_requirement_id
                    or event.artifact_digest != record.artifact_digest
                ):
                    raise ChangeStoreCorrupt(
                        "evidence purge does not bind an immutable evidence record"
                    )
            for archive in archives:
                revision = revisions_by_number.get(archive.plan_revision)
                if (
                    revision is None
                    or archive.plan_content_digest != revision.plan_content_digest
                ):
                    raise ChangeStoreCorrupt(
                        "archive does not bind an immutable plan revision"
                    )
            for report in reports:
                revision = revisions_by_number.get(report.plan_revision)
                if (
                    revision is None
                    or report.baseline_build_identity
                    != revision.baseline.build_identity
                    or report.baseline_source_identity
                    != revision.baseline.baseline_source_identity
                ):
                    raise ChangeStoreCorrupt(
                        "verification report does not bind its immutable baseline"
                    )
            if any(
                event.plan_revision not in revisions_by_number for event in audit_events
            ):
                raise ChangeStoreCorrupt(
                    "audit event does not bind an immutable plan revision"
                )
            pointer = self._path("plans", plan_id, "current.json")
            current: ChangeCurrentPointer | None = None
            # Both pointers are replaced in place; path_exists keeps a Windows
            # replace from reading as a missing pointer on these lock-free paths.
            if path_exists(pointer):
                current = self._read_model(pointer, ChangeCurrentPointer)
                revision = self.read_revision(plan_id, current.plan_revision)
                if (
                    current.plan_id != plan_id
                    or revision.plan_id != plan_id
                    or revision.plan_content_digest != current.plan_content_digest
                ):
                    raise ChangeStoreCorrupt(
                        "current pointer does not match its immutable revision"
                    )
            decision_pointer = self._path("plans", plan_id, "decision-current.json")
            if current is not None and not path_exists(decision_pointer):
                raise ChangeStoreCorrupt(
                    "visible current revision has no decision current pointer"
                )
            if path_exists(decision_pointer):
                current_decision = self._read_model(
                    decision_pointer, DecisionCurrentPointer
                )
                decision = self._read_model(
                    self._path(
                        "plans",
                        plan_id,
                        "decisions",
                        f"{current_decision.decision_id}.json",
                    ),
                    PlanDecision,
                )
                if (
                    current_decision.plan_id != plan_id
                    or decision.plan_id != plan_id
                    or decision.plan_revision != current_decision.plan_revision
                    or decision.plan_content_digest
                    != current_decision.plan_content_digest
                ):
                    raise ChangeStoreCorrupt(
                        "decision pointer does not match its immutable decision"
                    )
                if current is not None and (
                    current.plan_revision != current_decision.plan_revision
                    or current.plan_content_digest
                    != current_decision.plan_content_digest
                ):
                    raise ChangeStoreCorrupt(
                        "current pointer does not match the decision current pointer"
                    )
            if current is not None:
                current_events = [
                    event
                    for event in lifecycle_events
                    if event.plan_revision == current.plan_revision
                ]
                if not current_events:
                    raise ChangeStoreCorrupt(
                        "visible current revision has no lifecycle authority"
                    )
                if not any(
                    event.event_type == "plan_activated"
                    and event.plan_revision == current.plan_revision
                    and event.subject_id == current.pin_id
                    and event.payload.get("plan_content_digest")
                    == current.plan_content_digest
                    for event in audit_events
                ):
                    raise ChangeStoreCorrupt(
                        "visible current revision has no matching activation audit event"
                    )

    def revision_path(self, plan_id: str, revision: int) -> Path:
        if revision < 1:
            raise ChangeStoreCorrupt("plan revision must be a positive integer")
        return self._path("plans", plan_id, "revisions", f"{revision}.json")

    def _path(self, *components: str) -> Path:
        return resolve_under_root(self.root, *components)

    def _json_record_paths(self, directory: Path, *, label: str) -> list[Path]:
        """Return a directory's entire JSON record set or reject it as corrupt."""

        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise ChangeStoreCorrupt(f"{label} directory is unsafe")
        paths = sorted(directory.iterdir(), key=lambda item: item.name)
        if any(
            path.is_symlink() or not path.is_file() or path.suffix != ".json"
            for path in paths
        ):
            raise ChangeStoreCorrupt(f"{label} directory contains an unsafe entry")
        return paths

    def _validate_root_layout(self) -> None:
        allowed_directories = {"plans", "indexes", "exports"}
        for entry in self.root.iterdir():
            if entry.is_symlink():
                raise ChangeStoreCorrupt("change-safety store root contains a symlink")
            if entry.is_dir() and entry.name in allowed_directories:
                continue
            raise ChangeStoreCorrupt(
                "change-safety store root contains an unrecognized entry"
            )

    def _validate_plan_layout(self, plan_id: str) -> None:
        directory = self._path("plans", plan_id)
        allowed_directories = {
            "revisions",
            "lifecycle",
            "decisions",
            "evidence",
            "evidence-material",
            "evidence-purges",
            "verifications",
            "archive",
            "audit",
        }
        allowed_files = {"current.json", "decision-current.json"}
        for entry in directory.iterdir():
            if entry.is_symlink():
                raise ChangeStoreCorrupt("plan state directory contains a symlink")
            if entry.is_dir() and entry.name in allowed_directories:
                continue
            if entry.is_file() and entry.name in allowed_files:
                continue
            raise ChangeStoreCorrupt(
                "plan state directory contains an unrecognized entry"
            )
        audit = directory / "audit"
        if audit.exists():
            if audit.is_symlink() or not audit.is_dir():
                raise ChangeStoreCorrupt("plan audit directory is unsafe")
            for entry in audit.iterdir():
                if entry.is_symlink() or not entry.is_dir() or entry.name != "events":
                    raise ChangeStoreCorrupt(
                        "plan audit directory contains an unrecognized entry"
                    )

    def _evidence_material_path(self, evidence: VerificationEvidence) -> Path:
        return self._path(
            "plans",
            evidence.plan_id,
            "evidence-material",
            evidence.evidence_id,
            f"{evidence.artifact_digest}.json",
        )

    def _read_evidence_material(self, evidence: VerificationEvidence) -> dict[str, Any]:
        payload = self._read_json(self._evidence_material_path(evidence))
        availability = payload.get("availability")
        if (
            payload.get("plan_id") != evidence.plan_id
            or payload.get("plan_revision") != evidence.plan_revision
            or payload.get("evidence_id") != evidence.evidence_id
            or payload.get("artifact_digest") != evidence.artifact_digest
            or availability not in {"available", "purged", "redacted"}
            or not isinstance(payload.get("material"), dict)
        ):
            raise ChangeStoreCorrupt("evidence material identity is uncertain")
        if availability == "redacted" and payload.get(
            "redacted_material_digest"
        ) != canonical_digest({"redacted": True}):
            raise ChangeStoreCorrupt(
                "redacted evidence material does not have the expected digest"
            )
        if availability != "redacted" and payload.get("redacted_material_digest"):
            raise ChangeStoreCorrupt(
                "non-redacted evidence material has an unexpected redacted digest"
            )
        return payload

    def _validate_evidence_material_tree(
        self,
        plan_id: str,
        evidence: list[VerificationEvidence],
    ) -> None:
        root = self._path("plans", plan_id, "evidence-material")
        if not root.exists():
            if evidence:
                raise ChangeStoreCorrupt("evidence material directory is missing")
            return
        if root.is_symlink() or not root.is_dir():
            raise ChangeStoreCorrupt("evidence material directory is unsafe")
        expected = {record.evidence_id for record in evidence}
        entries = sorted(root.iterdir(), key=lambda item: item.name)
        if {entry.name for entry in entries} != expected or any(
            entry.is_symlink() or not entry.is_dir() for entry in entries
        ):
            raise ChangeStoreCorrupt(
                "evidence material directory contains an unsafe or orphan entry"
            )
        for record in evidence:
            directory = self._path(
                "plans",
                plan_id,
                "evidence-material",
                record.evidence_id,
            )
            paths = self._json_record_paths(directory, label="evidence material")
            expected_name = f"{record.artifact_digest}.json"
            if len(paths) != 1 or paths[0].name != expected_name:
                raise ChangeStoreCorrupt(
                    "evidence material does not match its immutable evidence record"
                )
            self._read_evidence_material(record)

    def _remove_unpublished_material(self, material_path: Path) -> None:
        """Best-effort rollback for material that never acquired a record."""

        try:
            material_path.unlink()
        except FileNotFoundError:
            return
        material_dir = material_path.parent
        try:
            material_dir.rmdir()
        except OSError:
            return
        try:
            material_dir.parent.rmdir()
        except OSError:
            return

    def _write_model(self, path: Path, model: BaseModel) -> None:
        self._write_json(path, model.model_dump(mode="json"))

    def _write_immutable_model(self, path: Path, model: BaseModel) -> None:
        if path.exists():
            existing = self._read_json(path)
            candidate = model.model_dump(mode="json")
            if existing != candidate:
                raise ChangeStoreCorrupt(
                    "immutable record path already has different data"
                )
            return
        self._write_model(path, model)

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self._validate_header(payload)
        atomic_write_json(path, payload, root=self.root)

    def _read_json(self, path: Path) -> dict[str, Any]:
        payload = read_json_object(path, root=self.root)
        self._validate_header(payload)
        return payload

    def _read_model(self, path: Path, model_type: type[ModelT]) -> ModelT:
        try:
            return model_type.model_validate(self._read_json(path))
        except ChangeStoreNotFound:
            raise
        except (ValidationError, ChangeSafetyError) as exc:
            raise ChangeStoreCorrupt(
                f"state record {path.name} does not match {model_type.__name__}"
            ) from exc

    def _validate_header(self, payload: dict[str, Any]) -> None:
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ChangeStoreCorrupt("state record schema_version is unsupported")
        if payload.get("change_contract_version") != CHANGE_CONTRACT_VERSION:
            raise ChangeStoreCorrupt(
                "state record change contract version is unsupported"
            )
        self._require_repo(payload.get("repo_id"))

    def _require_repo(self, repo_id: Any) -> None:
        if repo_id != self.repo_id:
            raise ChangeStoreCorrupt("state record repo_id does not match this store")


def immutable_export_payload(
    *,
    repo_id: str,
    export_id: str,
    records: Iterable[BaseModel],
) -> dict[str, Any]:
    """Construct a default-redacted audit export from immutable record objects."""

    redacted_records = [
        _redact_export_record(record.model_dump(mode="json")) for record in records
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "change_contract_version": CHANGE_CONTRACT_VERSION,
        "repo_id": repo_id,
        "export_id": export_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "records": redacted_records,
        "sensitive_material_included": _contains_detectable_sensitive_material(
            redacted_records
        ),
        "redaction_assurance": "best_effort_pattern_based",
    }


_EXPORT_SENSITIVE_KEYS = frozenset(
    {
        "artifact_digest",
        "material",
        "redacted_material_digest",
        "runner_metadata",
        "stderr_digest",
        "stdout_digest",
    }
)


def _redact_export_record(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_export_record(child)
            for key, child in value.items()
            if key not in _EXPORT_SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_redact_export_record(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value, redact_paths=True)[0]
    return value


def _contains_detectable_sensitive_material(value: Any) -> bool:
    """Report detectable residue without claiming exhaustive secret discovery."""

    if isinstance(value, dict):
        return any(
            key in _EXPORT_SENSITIVE_KEYS
            or _contains_detectable_sensitive_material(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_detectable_sensitive_material(item) for item in value)
    if isinstance(value, str):
        return redact_sensitive_text(value, redact_paths=True)[1]
    return False


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync; Windows does not permit opening directories."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
