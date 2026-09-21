"""Fail-closed verifier for one approved immutable ChangePlanRevision."""

from __future__ import annotations

from dataclasses import dataclass
import uuid
from pathlib import Path
from typing import Any, Iterable

from arcgraph.change.baseline import validate_baseline_reference
from arcgraph.change.contracts import (
    ChangePlanRevision,
    ChangeVerificationReport,
    ChangedPath,
    ChangedRegion,
    CodeIdentity,
    BuildIdentity,
    GraphDelta,
    PlanDecision,
    ProtectedSurface,
    VerificationEvidence,
)
from arcgraph.change.evidence import evidence_summary
from arcgraph.change.errors import (
    ChangePlanStateError,
    CurrentCodeIdentityMismatch,
    TrustedRunnerError,
)
from arcgraph.change.graph_delta import GraphDeltaEngine
from arcgraph.change.identities import (
    assert_code_identity_matches,
    assert_current_build_matches_code,
    capture_build_identity,
    capture_code_identity,
)
from arcgraph.change.source_provider import (
    BaselineSourceProvider,
    CurrentSourceProvider,
)
from arcgraph.change.paths import repository_path_comparison_key
from arcgraph.change.changed_regions import ChangedRegionMapper
from arcgraph.change.trusted_runners import (
    TrustedRunnerRegistry,
    attestation_meets_requirement,
)
from arcgraph.change.verdicts import VerdictFinding, decide_verification_verdict
from arcgraph.core.graph_store import GraphStoreReader


@dataclass(frozen=True, slots=True)
class _VerificationComparison:
    """One read-only baseline/current comparison shared by diff and verify."""

    current_build: BuildIdentity
    current_code: CodeIdentity
    changed_paths: list[ChangedPath]
    changed_regions: list[ChangedRegion]
    delta: GraphDelta
    protected_surface_changes: list[dict[str, Any]]


class ChangeVerifier:
    """Compute a report without mutating plan authority or executing commands."""

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
        self.trusted_runner_registry = trusted_runner_registry or TrustedRunnerRegistry(
            None,
            repo_root=self.repo_root,
        )

    def verify(
        self,
        revision: ChangePlanRevision,
        decision: PlanDecision | None,
        *,
        evidence: Iterable[VerificationEvidence],
        material_availability: dict[str, str] | None = None,
    ) -> ChangeVerificationReport:
        if revision.repo_id != self.repo_id:
            raise ChangePlanStateError("verification revision repo_id does not match")
        if decision is None or decision.status != "approved":
            raise ChangePlanStateError("only an approved exact revision can verify")
        if (
            decision.plan_id != revision.plan_id
            or decision.plan_revision != revision.revision
            or decision.plan_content_digest != revision.plan_content_digest
        ):
            raise ChangePlanStateError(
                "approval does not bind the exact revision digest"
            )
        comparison = self._comparison(revision)
        findings = self._scope_findings(
            revision,
            comparison.changed_paths,
            comparison.changed_regions,
        )
        findings.extend(
            self._delta_findings(
                comparison.delta.unknowns,
                comparison.delta.truncation,
            )
        )
        findings.extend(
            self._protected_surface_findings(
                revision,
                comparison.protected_surface_changes,
            )
        )
        availability = material_availability or {}
        evidence_records = list(evidence)
        findings.extend(
            self._evidence_findings(
                revision,
                comparison.current_code,
                evidence_records,
                availability,
            )
        )
        verdict = decide_verification_verdict(findings)
        return ChangeVerificationReport(
            repo_id=self.repo_id,
            verification_id=f"verification-{uuid.uuid4().hex}",
            plan_id=revision.plan_id,
            plan_revision=revision.revision,
            baseline_build_identity=revision.baseline.build_identity,
            baseline_source_identity=revision.baseline.baseline_source_identity,
            current_build_identity=comparison.current_build,
            current_code_identity=comparison.current_code,
            actual_changed_paths=comparison.changed_paths,
            changed_regions=comparison.changed_regions,
            graph_delta=comparison.delta,
            findings=[finding.to_payload() for finding in findings],
            residual_risk=[
                finding.to_payload()
                for finding in findings
                if finding.severity in {"warning", "manual_review"}
            ],
            verdict=verdict,
            verdict_reason=_verdict_reason(verdict, findings),
            evidence_ids=sorted(item.evidence_id for item in evidence_records),
            evidence_summary=evidence_summary(evidence_records, availability),
        )

    def graph_delta(self, revision: ChangePlanRevision) -> GraphDelta:
        """Compute an exact current graph delta without requiring approval.

        The method is intentionally read-only.  It is used by CLI ``diff`` and
        MCP preview paths, while ``verify`` adds the approved-decision and
        evidence requirements above this shared comparison.
        """

        if revision.repo_id != self.repo_id:
            raise ChangePlanStateError("diff revision repo_id does not match")
        return self._comparison(revision).delta

    def _comparison(self, revision: ChangePlanRevision) -> _VerificationComparison:
        """Capture current state and compare it to one immutable baseline."""

        validate_baseline_reference(self.repo_root, self.output_dir, revision.baseline)
        baseline_reader = GraphStoreReader.from_build(
            self.output_dir,
            revision.baseline.build_identity.index_version,
        )
        current_reader = GraphStoreReader.from_current(self.output_dir)
        current_build = capture_build_identity(
            self.repo_root,
            self.output_dir,
            repo_id=self.repo_id,
        )
        source_paths = [record.path for record in current_reader.iter_files()]
        current_code = capture_code_identity(
            self.repo_root,
            repo_id=self.repo_id,
            build_identity=current_build,
            source_paths=source_paths,
        )
        assert_current_build_matches_code(current_build, current_code)
        mapper = ChangedRegionMapper(
            self.repo_root,
            repo_id=self.repo_id,
            baseline_source=BaselineSourceProvider(self.repo_root, revision.baseline),
            current_source=CurrentSourceProvider(self.repo_root, repo_id=self.repo_id),
            baseline_nodes=baseline_reader.iter_nodes(),
            current_nodes=current_reader.iter_nodes(),
        )
        changed_paths = mapper.changed_paths()
        changed_regions = mapper.changed_regions(changed_paths)
        delta = GraphDeltaEngine().compare(
            repo_id=self.repo_id,
            baseline_reader=baseline_reader,
            current_reader=current_reader,
            baseline_build_identity=revision.baseline.build_identity,
            baseline_source_identity=revision.baseline.baseline_source_identity,
            current_build_identity=current_build,
            current_code_identity=current_code,
            changed_paths=changed_paths,
            changed_regions=changed_regions,
        )
        protected_changes = _protected_surface_changes(
            revision,
            delta,
            self.repo_root,
        )
        delta = delta.model_copy(
            update={"protected_surface_changes": protected_changes}
        )
        return _VerificationComparison(
            current_build=current_build,
            current_code=current_code,
            changed_paths=changed_paths,
            changed_regions=changed_regions,
            delta=delta,
            protected_surface_changes=protected_changes,
        )

    def _scope_findings(
        self,
        revision: ChangePlanRevision,
        changed_paths: list[ChangedPath],
        changed_regions: list[ChangedRegion],
    ) -> list[VerdictFinding]:
        findings: list[VerdictFinding] = []
        if changed_paths and not changed_regions:
            findings.append(
                VerdictFinding(
                    code="GRAPH_DELTA_INCOMPLETE",
                    message="Git reports changed paths but no regions were produced",
                    severity="blocking",
                    category="changed_region",
                )
            )
            return findings
        for region in changed_regions:
            # A normalized implementation digest distinguishes implementation
            # semantics from text-only edits; it is not an authorization bypass.
            # Every real Git region, including comments and formatting, still
            # has to satisfy allowed, review-only, and protected scope policy.
            if region.mapping_status not in {"mapped", "not_applicable"}:
                findings.append(
                    VerdictFinding(
                        code="CHANGED_REGION_UNMAPPED",
                        message=f"changed region is {region.mapping_status}: {region.path}",
                        severity="blocking",
                        category="changed_region",
                    )
                )
                continue
            if not any(
                _scope_matches(region, scope) for scope in revision.allowed_edit_scope
            ):
                findings.append(
                    VerdictFinding(
                        code="CHANGE_SCOPE_EXCEEDED",
                        message=f"change is outside allowed edit scope: {region.path}",
                        severity="blocking",
                        category="scope",
                    )
                )
            if any(
                _scope_matches(region, scope) for scope in revision.review_only_scope
            ):
                findings.append(
                    VerdictFinding(
                        code="REVIEW_ONLY_SCOPE_CHANGED",
                        message=f"review-only scope changed: {region.path}",
                        severity="blocking",
                        category="scope",
                    )
                )
            protected_matches = [
                scope
                for scope in revision.protected_scope
                if _scope_matches(region, scope)
            ]
            if protected_matches:
                findings.append(
                    VerdictFinding(
                        code="PROTECTED_SCOPE_CHANGED",
                        message=f"protected scope changed without authorization: {region.path}",
                        severity="blocking",
                        category="protected_surface",
                    )
                )
        return findings

    def _protected_surface_findings(
        self,
        revision: ChangePlanRevision,
        protected_changes: list[dict[str, Any]],
    ) -> list[VerdictFinding]:
        findings: list[VerdictFinding] = []
        for change in protected_changes:
            surface = _surface_for_id(revision.protected_surfaces, change["surface_id"])
            if surface.detection_status == "unknown":
                findings.append(
                    VerdictFinding(
                        code="CAPABILITY_UNKNOWN_HIGH_RISK",
                        message=f"protected surface is unknown: {surface.surface_id}",
                        severity="blocking",
                        category="protected_surface",
                    )
                )
                continue
            if _surface_is_explicitly_authorized(surface, revision.allowed_edit_scope):
                continue
            findings.append(
                VerdictFinding(
                    code="PROTECTED_SURFACE_CHANGED",
                    message=(
                        "protected-surface graph projection changed without an "
                        f"allowed scope: {surface.surface_id}"
                    ),
                    severity="blocking",
                    category="protected_surface",
                    details={"change_category": change["change_category"]},
                )
            )
        return findings

    def _delta_findings(
        self,
        unknowns: list[dict[str, Any]],
        truncation: dict[str, Any],
    ) -> list[VerdictFinding]:
        findings = [
            VerdictFinding(
                code=str(item.get("code", "UNRESOLVED_HIGH_RISK_DEPENDENCY")),
                message=str(item.get("reason") or item.get("path") or "unknown delta"),
                severity="blocking" if item.get("blocking", True) else "manual_review",
                category="graph_delta",
            )
            for item in unknowns
        ]
        if truncation.get("truncated"):
            findings.append(
                VerdictFinding(
                    code="GRAPH_DELTA_INCOMPLETE",
                    message="graph delta was truncated",
                    severity="blocking",
                    category="graph_delta",
                )
            )
        return findings

    def _evidence_findings(
        self,
        revision: ChangePlanRevision,
        current_code,
        evidence: list[VerificationEvidence],
        availability: dict[str, str],
    ) -> list[VerdictFinding]:
        findings: list[VerdictFinding] = []
        for requirement in revision.verification_plan.requirements:
            matching = [
                item
                for item in evidence
                if item.verification_requirement_id == requirement.requirement_id
                and item.plan_id == revision.plan_id
                and item.plan_revision == revision.revision
                and item.plan_content_digest == revision.plan_content_digest
            ]
            passing = []
            observed_codes: set[str] = set()
            for item in matching:
                try:
                    assert_code_identity_matches(
                        item.current_code_identity, current_code
                    )
                except CurrentCodeIdentityMismatch:
                    observed_codes.add("EVIDENCE_STALE")
                    continue
                if not attestation_meets_requirement(item, requirement):
                    observed_codes.add("EVIDENCE_ATTESTATION_INSUFFICIENT")
                    continue
                try:
                    self.trusted_runner_registry.validate(item, requirement)
                except TrustedRunnerError:
                    observed_codes.add("TRUSTED_RUNNER_UNAVAILABLE")
                    continue
                if item.result == "fail":
                    observed_codes.add("EVIDENCE_FAILED")
                    continue
                if item.result != "pass":
                    observed_codes.add("EVIDENCE_MISSING")
                    continue
                if availability.get(item.evidence_id, "unknown") not in {
                    "available",
                    "redacted",
                }:
                    observed_codes.add("EVIDENCE_STALE")
                    continue
                if item.result == "pass":
                    passing.append(item)
            if passing:
                continue
            code = _evidence_failure_code(observed_codes, bool(matching))
            severity = "blocking" if requirement.level == "required" else "warning"
            findings.append(
                VerdictFinding(
                    code=code,
                    message=f"verification requirement lacks current passing evidence: {requirement.requirement_id}",
                    severity=severity,
                    category="evidence",
                )
            )
        return findings


def _scope_matches(region: ChangedRegion, scope) -> bool:
    if scope.symbol_id is not None:
        # A symbol-scoped plan is bound to the baseline entity, not only to
        # its old file path.  That lets an explicitly planned rename/move
        # remain reviewable through the Baseline/Current symbol mapping while
        # still requiring an exact stable symbol witness.
        return scope.symbol_id in {
            region.baseline_symbol_id,
            region.current_symbol_id,
        }
    region_path_keys = {
        key
        for key in (
            region.old_path_comparison_key,
            region.new_path_comparison_key,
            region.path_comparison_key,
        )
        if key is not None
    }
    return scope.path_comparison_key in region_path_keys


def _protected_surface_changes(
    revision: ChangePlanRevision,
    delta,
    repo_root: Path,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for category, values in (
        ("identity_change", delta.identity_changes),
        ("semantic_change", delta.semantic_changes),
        ("implementation_change", delta.implementation_changes),
    ):
        for value in values:
            for surface in revision.protected_surfaces:
                if _surface_matches_delta(surface, value, repo_root):
                    changes.append(
                        {
                            "surface_id": surface.surface_id,
                            "surface_kind": surface.surface_kind,
                            "change_category": category,
                            "stable_id": value.get("stable_id"),
                            "path": value.get("path"),
                        }
                    )
    unique = {
        (
            item["surface_id"],
            item["change_category"],
            item.get("stable_id"),
            item.get("path"),
        ): item
        for item in changes
    }
    return [unique[key] for key in sorted(unique)]


def _surface_matches_delta(
    surface: ProtectedSurface,
    change: dict[str, Any],
    repo_root: Path,
) -> bool:
    stable_id = change.get("stable_id")
    if (
        surface.symbol_id is not None
        and isinstance(stable_id, str)
        and stable_id.endswith(f":{surface.symbol_id}")
    ):
        return True
    if surface.symbol_id is not None and surface.symbol_id in {
        change.get("baseline_symbol_id"),
        change.get("current_symbol_id"),
    }:
        return True
    path = change.get("path")
    if (
        surface.path_comparison_key is not None
        and isinstance(path, str)
        and repository_path_comparison_key(path, repo_root)
        == surface.path_comparison_key
    ):
        return True
    projection = change.get("projection")
    if isinstance(projection, dict):
        projected_path = projection.get("path")
        if (
            surface.path_comparison_key is not None
            and isinstance(projected_path, str)
            and repository_path_comparison_key(projected_path, repo_root)
            == surface.path_comparison_key
        ):
            return True
        if surface.symbol_id is not None and surface.symbol_id in {
            _without_repo_prefix(projection.get("source")),
            _without_repo_prefix(projection.get("target")),
        }:
            return True
    return False


def _surface_for_id(
    surfaces: list[ProtectedSurface],
    surface_id: str,
) -> ProtectedSurface:
    for surface in surfaces:
        if surface.surface_id == surface_id:
            return surface
    raise ChangePlanStateError("protected-surface delta lost its source surface")


def _surface_is_explicitly_authorized(
    surface: ProtectedSurface,
    allowed_scopes,
) -> bool:
    for scope in allowed_scopes:
        if surface.symbol_id is not None:
            if scope.symbol_id == surface.symbol_id:
                return True
            # An explicit path target is intentionally file-wide.  A
            # symbol-scoped target in the same file is not authorization for a
            # different protected symbol.
            if (
                scope.symbol_id is None
                and surface.path_comparison_key is not None
                and surface.path_comparison_key == scope.path_comparison_key
            ):
                return True
            continue
        if (
            surface.path_comparison_key is not None
            and scope.symbol_id is None
            and surface.path_comparison_key == scope.path_comparison_key
        ):
            return True
    return False


def _evidence_failure_code(observed_codes: set[str], has_matching: bool) -> str:
    for code in (
        "TRUSTED_RUNNER_UNAVAILABLE",
        "EVIDENCE_ATTESTATION_INSUFFICIENT",
        "EVIDENCE_FAILED",
        "EVIDENCE_STALE",
        "EVIDENCE_MISSING",
    ):
        if code in observed_codes:
            return code
    return "EVIDENCE_STALE" if has_matching else "EVIDENCE_MISSING"


def _without_repo_prefix(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.split(":", 1)[1] if ":" in value else value


def _verdict_reason(verdict: str, findings: list[VerdictFinding]) -> str:
    blocking = [finding for finding in findings if finding.severity == "blocking"]
    if blocking:
        return f"{verdict}: {blocking[0].code}"
    return verdict
