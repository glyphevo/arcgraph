"""Versioned, immutable-record contracts for Surgical Change Safety.

These models deliberately use ``extra=forbid``.  Persisted data is a safety
boundary: accepting unknown fields would make a future producer look valid to
an older verifier while silently dropping the part it does not understand.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arcgraph.change.errors import (
    ChangeContractDigestMismatch,
    ChangeContractSchemaUnsupported,
    ChangeContractVersionUnsupported,
)
from arcgraph.core.schemas import SCHEMA_VERSION

CHANGE_CONTRACT_VERSION = "1.1.0"
CODE_IDENTITY_PROJECTION_VERSION = "1.0"
COMPARISON_PROJECTION_VERSION = "1.2"
SOURCE_NORMALIZATION_PROJECTION_VERSION = "1.1"
SOURCE_PROVIDER_VERSION = "git-object-v1"

PlanStatus = Literal[
    "draft",
    "blocked",
    "ready",
    "verified",
    "failed",
    "superseded",
    "abandoned",
    "archived",
]
PlanDecisionStatus = Literal["pending", "approved", "rejected"]
PlanningVerdict = Literal[
    "PLAN_READY",
    "PLAN_READY_WITH_KNOWN_RISKS",
    "PLAN_BLOCKED_INSUFFICIENT_EVIDENCE",
    "PLAN_BLOCKED_STALE_INDEX",
    "PLAN_BLOCKED_UNRESOLVED_TARGET",
    "PLAN_BLOCKED_TRUNCATED_IMPACT",
    "PLAN_BLOCKED_CAPABILITY_GAP",
    "PLAN_INVALID_INTENT",
]
VerificationVerdict = Literal[
    "SAFE_TO_PROCEED",
    "SAFE_WITH_KNOWN_RISKS",
    "INSUFFICIENT_EVIDENCE",
    "BASELINE_UNAVAILABLE",
    "INDEX_STALE",
    "CHANGE_SCOPE_EXCEEDED",
    "PROTECTED_SURFACE_CHANGED",
    "UNRESOLVED_HIGH_RISK_DEPENDENCY",
    "VERIFICATION_FAILED",
    "PLAN_INVALID_OR_SUPERSEDED",
]
AttestationLevel = Literal["self_reported", "artifact_backed", "trusted_runner"]

_SET_LIKE_FIELD_NAMES = frozenset(
    {
        "targets",
        "allowed_surface_changes",
        "forbidden_surface_changes",
        "user_declared_protected_surfaces",
        "primary_edit_candidates",
        "allowed_edit_scope",
        "review_only_scope",
        "protected_scope",
        "protected_surfaces",
        "affected_entrypoints",
        "affected_resources",
        "affected_contracts",
        "requirements",
        "unknowns",
        "stop_conditions",
        "residual_risk",
        "source_scope_item_ids",
        "matched_scope_item_ids",
        "changed_paths",
        "changed_regions",
    }
)
_PLAN_DIGEST_EXCLUDED_PATHS = frozenset(
    {
        ("plan_content_digest",),
        ("revision_created_at",),
        ("intent", "created_at"),
        ("baseline", "baseline_created_at"),
        ("baseline", "build_identity", "created_at"),
        ("baseline", "working_tree_identity", "captured_at"),
        ("verification_plan", "generated_at"),
    }
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(value: Any) -> str:
    """Return the canonical UTF-8 JSON representation used for all digests."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_projection(value: Any, *, for_plan_digest: bool = False) -> Any:
    """Normalize nested models, dictionaries, and set-like lists.

    A plan digest intentionally excludes wall-clock fields and dynamic read
    projections.  Ordered fields (for example ``acceptance_criteria`` and
    ``constraints``) keep their order; all declared set-like fields are
    canonicalized, deduplicated, and sorted by their full business projection.
    """

    if for_plan_digest:
        return _stable_projection_with_field_names(value)
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            normalized[key] = stable_projection(value[key])
        return normalized
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [
            stable_projection(item, for_plan_digest=for_plan_digest) for item in value
        ]
        return items
    return value


def _stable_projection_with_field_names(
    value: Any,
    field_name: str | None = None,
    field_path: tuple[str, ...] = (),
) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            child_path = (*field_path, key)
            if child_path in _PLAN_DIGEST_EXCLUDED_PATHS:
                continue
            normalized[key] = _stable_projection_with_field_names(
                value[key],
                key,
                child_path,
            )
        return normalized
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [
            _stable_projection_with_field_names(item, field_path=field_path)
            for item in value
        ]
        if field_name in _SET_LIKE_FIELD_NAMES:
            unique = {canonical_json(item): item for item in items}
            return [unique[key] for key in sorted(unique)]
        return items
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def plan_content_projection(
    value: "ChangePlanRevision | dict[str, Any]",
) -> dict[str, Any]:
    """Return the immutable canonical projection of a plan revision."""

    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    projected = _stable_projection_with_field_names(raw)
    if not isinstance(projected, dict):  # pragma: no cover - static boundary
        raise TypeError("ChangePlanRevision projection must be an object")
    return projected


def plan_content_digest(value: "ChangePlanRevision | dict[str, Any]") -> str:
    return canonical_digest(plan_content_projection(value))


def code_identity_projection(value: "CodeIdentity | dict[str, Any]") -> dict[str, Any]:
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    return {
        key: stable_projection(raw[key])
        for key in sorted(raw)
        if key not in {"captured_at", "code_identity_digest"}
    }


def code_identity_digest(value: "CodeIdentity | dict[str, Any]") -> str:
    return canonical_digest(code_identity_projection(value))


def build_identity_projection(
    value: "BuildIdentity | dict[str, Any]",
) -> dict[str, Any]:
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    return {
        key: stable_projection(raw[key])
        for key in sorted(raw)
        if key not in {"created_at"}
    }


def build_identity_digest(value: "BuildIdentity | dict[str, Any]") -> str:
    return canonical_digest(build_identity_projection(value))


class ChangeContractModel(BaseModel):
    """Base class for all persisted and externally-visible change payloads."""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    schema_version: str = SCHEMA_VERSION
    change_contract_version: str = CHANGE_CONTRACT_VERSION
    repo_id: str

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ChangeContractSchemaUnsupported(
                f"expected {SCHEMA_VERSION}, received {value!r}"
            )
        return value

    @field_validator("change_contract_version")
    @classmethod
    def _validate_change_contract_version(cls, value: str) -> str:
        if value != CHANGE_CONTRACT_VERSION:
            raise ChangeContractVersionUnsupported(
                f"expected {CHANGE_CONTRACT_VERSION}, received {value!r}"
            )
        return value

    @field_validator("repo_id")
    @classmethod
    def _validate_repo_id(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("repo_id must be a non-empty logical repository id")
        return value.strip()


class ChangeTarget(ChangeContractModel):
    kind: Literal["symbol", "path", "route", "resource", "contract"]
    value: str
    resolution_policy: Literal[
        "must_resolve", "allow_manual_review", "informational"
    ] = "must_resolve"
    reason: str | None = None
    requested_by: str | None = None

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("target value must not be empty")
        return value


class ChangeIntent(ChangeContractModel):
    intent_id: str
    revision: int = 1
    task: str
    targets: list[ChangeTarget] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    allowed_surface_changes: list[str] = Field(default_factory=list)
    forbidden_surface_changes: list[str] = Field(default_factory=list)
    user_declared_protected_surfaces: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    source_digest: str | None = None

    @model_validator(mode="after")
    def _validate_target_repos(self) -> "ChangeIntent":
        for target in self.targets:
            if target.repo_id != self.repo_id:
                raise ValueError("intent targets must use the intent repo_id")
        return self


class ScopeItem(ChangeContractModel):
    scope_item_id: str
    path: str
    path_comparison_key: str
    symbol_id: str | None = None
    baseline_symbol_id: str | None = None
    baseline_start_line: int | None = None
    baseline_end_line: int | None = None
    baseline_raw_source_digest: str | None = None
    baseline_normalized_implementation_digest: str | None = None
    source_normalization_projection_version: str = (
        SOURCE_NORMALIZATION_PROJECTION_VERSION
    )
    scope_kind: Literal["path", "symbol", "region", "surface"]
    policy: Literal["candidate", "allowed", "review_only", "protected"]
    reason: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confidence: Literal[
        "confirmed", "inferred", "heuristic", "runtime-only", "unresolved"
    ] = "unresolved"

    @model_validator(mode="after")
    def _validate_lines(self) -> "ScopeItem":
        if (
            self.baseline_start_line is not None
            and self.baseline_end_line is not None
            and self.baseline_start_line > self.baseline_end_line
        ):
            raise ValueError("baseline start line must not exceed end line")
        return self

    @field_validator("source_normalization_projection_version")
    @classmethod
    def _validate_normalization_version(cls, value: str) -> str:
        if value != SOURCE_NORMALIZATION_PROJECTION_VERSION:
            raise ChangeContractVersionUnsupported(
                "unsupported source normalization projection version"
            )
        return value


class ChangedPath(ChangeContractModel):
    change_kind: Literal[
        "added", "modified", "deleted", "renamed", "copied", "untracked"
    ]
    old_path: str | None = None
    old_path_comparison_key: str | None = None
    new_path: str | None = None
    new_path_comparison_key: str | None = None
    status: Literal["observed", "mapped", "unknown", "manual_review", "blocked"] = (
        "observed"
    )
    evidence: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_paths(self) -> "ChangedPath":
        if not self.old_path and not self.new_path:
            raise ValueError("a changed path needs an old or new repository path")
        return self


class ChangedRegion(ChangeContractModel):
    path: str
    path_comparison_key: str
    old_path: str | None = None
    old_path_comparison_key: str | None = None
    new_path: str | None = None
    new_path_comparison_key: str | None = None
    old_start_line: int | None = None
    old_end_line: int | None = None
    new_start_line: int | None = None
    new_end_line: int | None = None
    baseline_symbol_id: str | None = None
    current_symbol_id: str | None = None
    classification: str
    mapping_status: Literal["mapped", "ambiguous", "unmapped", "not_applicable"]
    baseline_raw_source_digest: str | None = None
    current_raw_source_digest: str | None = None
    baseline_normalized_implementation_digest: str | None = None
    current_normalized_implementation_digest: str | None = None
    source_normalization_projection_version: str = (
        SOURCE_NORMALIZATION_PROJECTION_VERSION
    )
    baseline_source_identity: dict[str, Any] | None = None
    current_source_identity: dict[str, Any] | None = None
    matched_scope_item_ids: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("source_normalization_projection_version")
    @classmethod
    def _validate_normalization_version(cls, value: str) -> str:
        if value != SOURCE_NORMALIZATION_PROJECTION_VERSION:
            raise ChangeContractVersionUnsupported(
                "unsupported source normalization projection version"
            )
        return value


class BuildIdentity(ChangeContractModel):
    index_version: str
    build_relative_path: str
    commit_sha: str | None = None
    git_tree_sha: str | None = None
    file_manifest_digest: str
    summary_digest: str
    evidence_manifest_digest: str | None = None
    source_provider_version: str = SOURCE_PROVIDER_VERSION
    index_sqlite_sha256: str
    index_sqlite_size: int
    created_at: datetime = Field(default_factory=utc_now)
    freshness: dict[str, Any] = Field(default_factory=dict)

    @field_validator("index_sqlite_size")
    @classmethod
    def _validate_sqlite_size(cls, value: int) -> int:
        if value < 0:
            raise ValueError("index sqlite size must not be negative")
        return value

    @field_validator("source_provider_version")
    @classmethod
    def _validate_source_provider_version(cls, value: str) -> str:
        if value != SOURCE_PROVIDER_VERSION:
            raise ChangeContractVersionUnsupported(
                "unsupported source provider version"
            )
        return value


class CodeIdentity(ChangeContractModel):
    head_sha: str | None = None
    git_tree_sha: str | None = None
    working_tree_clean: bool
    working_tree_status_digest: str
    file_manifest_digest: str
    index_version: str
    build_identity_digest: str
    code_identity_projection_version: str = CODE_IDENTITY_PROJECTION_VERSION
    code_identity_digest: str = ""
    captured_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_digest(self) -> "CodeIdentity":
        expected = code_identity_digest(self)
        if self.code_identity_digest and self.code_identity_digest != expected:
            raise ChangeContractDigestMismatch(
                "CodeIdentity digest does not match its canonical projection"
            )
        self.code_identity_digest = expected
        return self

    @field_validator("code_identity_projection_version")
    @classmethod
    def _validate_code_identity_projection_version(cls, value: str) -> str:
        if value != CODE_IDENTITY_PROJECTION_VERSION:
            raise ChangeContractVersionUnsupported(
                "unsupported CodeIdentity projection version"
            )
        return value


class WorkingTreeIdentity(ChangeContractModel):
    head_sha: str | None = None
    branch: str | None = None
    clean: bool
    status_digest: str
    changed_paths: list[str] = Field(default_factory=list)
    untracked_paths: list[str] = Field(default_factory=list)
    captured_at: datetime = Field(default_factory=utc_now)


class BaselineReference(ChangeContractModel):
    baseline_id: str
    build_identity: BuildIdentity
    working_tree_identity: WorkingTreeIdentity
    pin_id: str
    baseline_created_at: datetime = Field(default_factory=utc_now)
    baseline_source_identity: dict[str, Any]

    @model_validator(mode="after")
    def _validate_nested_repo(self) -> "BaselineReference":
        if self.build_identity.repo_id != self.repo_id:
            raise ValueError("baseline build identity repo_id does not match")
        if self.working_tree_identity.repo_id != self.repo_id:
            raise ValueError("baseline working tree repo_id does not match")
        if self.baseline_source_identity.get("repo_id") != self.repo_id:
            raise ValueError("baseline source identity repo_id does not match")
        return self


class VerificationRequirement(ChangeContractModel):
    requirement_id: str
    category: str
    level: Literal["required", "recommended", "optional"]
    subject: str
    reason: str
    acceptance_condition: str
    code_identity_requirement: Literal["exact_current_code_identity"] = (
        "exact_current_code_identity"
    )
    freshness_requirement: Literal["current_build_required"] = "current_build_required"
    minimum_attestation_level: AttestationLevel = "self_reported"
    source_scope_item_ids: list[str] = Field(default_factory=list)


class VerificationPlan(ChangeContractModel):
    plan_id: str
    plan_revision: int
    requirements: list[VerificationRequirement] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)
    input_digest: str

    @model_validator(mode="after")
    def _validate_requirement_repos(self) -> "VerificationPlan":
        for requirement in self.requirements:
            if requirement.repo_id != self.repo_id:
                raise ValueError("verification requirement repo_id does not match")
        return self


class ProtectedSurface(ChangeContractModel):
    surface_id: str
    surface_kind: Literal[
        "route",
        "openapi",
        "table",
        "queue",
        "cli",
        "mcp",
        "typescript_export",
        "python_public_api",
        "configuration",
        "framework_registration",
        "database_migration",
        "security_boundary",
        "event_contract",
        "external_integration",
    ]
    path: str | None = None
    path_comparison_key: str | None = None
    symbol_id: str | None = None
    detection_source: str
    capability: Literal[
        "reliable_graph_fact",
        "explicit_protocol_input",
        "partial_or_heuristic",
        "user_declaration_required",
        "unavailable",
    ]
    confidence: Literal[
        "confirmed", "inferred", "heuristic", "runtime-only", "unresolved"
    ]
    detection_status: Literal["confirmed", "candidate", "declared", "unknown"]
    policy: Literal["protected", "review_only"] = "protected"
    evidence: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_detection_capability(self) -> "ProtectedSurface":
        if self.detection_status == "confirmed" and self.capability not in {
            "reliable_graph_fact",
            "explicit_protocol_input",
        }:
            raise ValueError(
                "a partial, declared, or unavailable capability cannot be confirmed"
            )
        if (
            self.detection_status == "declared"
            and self.capability != "user_declaration_required"
        ):
            raise ValueError(
                "a declared protected surface must use user declaration capability"
            )
        if self.capability == "unavailable" and self.detection_status != "unknown":
            raise ValueError("an unavailable capability must remain unknown")
        return self


class ChangePlanRevision(ChangeContractModel):
    """Immutable planning authority stored at ``revisions/<revision>.json``."""

    plan_id: str
    revision: int
    intent: ChangeIntent
    baseline: BaselineReference
    planning_verdict: PlanningVerdict
    primary_edit_candidates: list[ScopeItem] = Field(default_factory=list)
    allowed_edit_scope: list[ScopeItem] = Field(default_factory=list)
    review_only_scope: list[ScopeItem] = Field(default_factory=list)
    protected_scope: list[ScopeItem] = Field(default_factory=list)
    protected_surfaces: list[ProtectedSurface] = Field(default_factory=list)
    affected_entrypoints: list[dict[str, Any]] = Field(default_factory=list)
    affected_resources: list[dict[str, Any]] = Field(default_factory=list)
    affected_contracts: list[dict[str, Any]] = Field(default_factory=list)
    verification_plan: VerificationPlan
    unknowns: list[dict[str, Any]] = Field(default_factory=list)
    stop_conditions: list[dict[str, Any]] = Field(default_factory=list)
    residual_risk: list[dict[str, Any]] = Field(default_factory=list)
    input_digest: str
    revision_created_at: datetime = Field(default_factory=utc_now)
    plan_content_digest: str = ""

    @model_validator(mode="after")
    def _validate_immutable_content(self) -> "ChangePlanRevision":
        nested = [self.intent, self.baseline, self.verification_plan]
        nested.extend(self.primary_edit_candidates)
        nested.extend(self.allowed_edit_scope)
        nested.extend(self.review_only_scope)
        nested.extend(self.protected_scope)
        nested.extend(self.protected_surfaces)
        if any(item.repo_id != self.repo_id for item in nested):
            raise ValueError("plan nested record repo_id does not match")
        if self.verification_plan.plan_id != self.plan_id:
            raise ValueError("verification plan must bind the exact plan id")
        if self.verification_plan.plan_revision != self.revision:
            raise ValueError("verification plan must bind the exact revision")
        expected = plan_content_digest(self)
        if self.plan_content_digest and self.plan_content_digest != expected:
            raise ChangeContractDigestMismatch(
                "ChangePlanRevision digest does not match immutable content"
            )
        self.plan_content_digest = expected
        return self


class PlanLifecycleEvent(ChangeContractModel):
    event_id: str
    plan_id: str
    plan_revision: int
    prior_status: PlanStatus | None = None
    next_status: PlanStatus
    reason: str
    recorded_at: datetime = Field(default_factory=utc_now)


class PlanDecision(ChangeContractModel):
    decision_id: str
    plan_id: str
    plan_revision: int
    plan_content_digest: str
    status: PlanDecisionStatus = "pending"
    actor: str
    reason: str
    decided_at: datetime = Field(default_factory=utc_now)


class ChangeCurrentPointer(ChangeContractModel):
    plan_id: str
    plan_revision: int
    plan_content_digest: str
    pin_id: str
    updated_at: datetime = Field(default_factory=utc_now)


class DecisionCurrentPointer(ChangeContractModel):
    plan_id: str
    plan_revision: int
    plan_content_digest: str
    decision_id: str
    updated_at: datetime = Field(default_factory=utc_now)


class ChangePlanView(ChangeContractModel):
    plan_id: str
    revision: int
    plan_revision: ChangePlanRevision
    plan_status: PlanStatus
    plan_decision: PlanDecision | None = None
    verification_summary: dict[str, Any] = Field(default_factory=dict)
    evidence_summary: dict[str, Any] = Field(default_factory=dict)
    archive_projection: dict[str, Any] = Field(default_factory=dict)
    pin_projection: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_view_repo(self) -> "ChangePlanView":
        if self.plan_revision.repo_id != self.repo_id:
            raise ValueError("plan revision repo_id does not match view")
        if (
            self.plan_id != self.plan_revision.plan_id
            or self.revision != self.plan_revision.revision
        ):
            raise ValueError("plan view does not bind the exact plan revision")
        if self.plan_decision and self.plan_decision.repo_id != self.repo_id:
            raise ValueError("plan decision repo_id does not match view")
        if self.plan_decision and (
            self.plan_decision.plan_id != self.plan_id
            or self.plan_decision.plan_revision != self.revision
            or self.plan_decision.plan_content_digest
            != self.plan_revision.plan_content_digest
        ):
            raise ValueError("plan decision does not bind the exact view revision")
        return self


class VerificationEvidence(ChangeContractModel):
    evidence_id: str
    verification_requirement_id: str
    plan_id: str
    plan_revision: int
    plan_content_digest: str
    result: Literal["pass", "fail", "unknown", "not_run"]
    current_code_identity: CodeIdentity
    attestation_level: AttestationLevel
    producer_identity: str
    stdout_digest: str | None = None
    stderr_digest: str | None = None
    runner_metadata: dict[str, Any] = Field(default_factory=dict)
    redaction_status: Literal["none", "redacted", "purged"] = "none"
    artifact_digest: str
    payload_digest: str = ""
    captured_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_evidence(self) -> "VerificationEvidence":
        if self.current_code_identity.repo_id != self.repo_id:
            raise ValueError("evidence CodeIdentity repo_id does not match")
        payload = self.model_dump(mode="json", exclude={"payload_digest"})
        expected = canonical_digest(stable_projection(payload))
        if self.payload_digest and self.payload_digest != expected:
            raise ChangeContractDigestMismatch("Evidence payload digest does not match")
        self.payload_digest = expected
        return self


class GraphDelta(ChangeContractModel):
    baseline_build_identity: BuildIdentity
    baseline_source_identity: dict[str, Any]
    current_build_identity: BuildIdentity
    current_code_identity: CodeIdentity
    comparison_projection_version: str = COMPARISON_PROJECTION_VERSION
    changed_paths: list[ChangedPath] = Field(default_factory=list)
    changed_regions: list[ChangedRegion] = Field(default_factory=list)
    identity_changes: list[dict[str, Any]] = Field(default_factory=list)
    semantic_changes: list[dict[str, Any]] = Field(default_factory=list)
    implementation_changes: list[dict[str, Any]] = Field(default_factory=list)
    location_changes: list[dict[str, Any]] = Field(default_factory=list)
    evidence_changes: list[dict[str, Any]] = Field(default_factory=list)
    evidence_location_changes: list[dict[str, Any]] = Field(default_factory=list)
    lifecycle_metadata_changes: list[dict[str, Any]] = Field(default_factory=list)
    protected_surface_changes: list[dict[str, Any]] = Field(default_factory=list)
    unknowns: list[dict[str, Any]] = Field(default_factory=list)
    truncation: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("comparison_projection_version")
    @classmethod
    def _validate_comparison_projection_version(cls, value: str) -> str:
        if value != COMPARISON_PROJECTION_VERSION:
            raise ChangeContractVersionUnsupported(
                "unsupported graph comparison projection version"
            )
        return value

    @model_validator(mode="after")
    def _validate_delta_repos(self) -> "GraphDelta":
        nested = [
            self.baseline_build_identity,
            self.current_build_identity,
            self.current_code_identity,
            *self.changed_paths,
            *self.changed_regions,
        ]
        if any(item.repo_id != self.repo_id for item in nested):
            raise ValueError("graph delta nested repo_id does not match")
        if self.baseline_source_identity.get("repo_id") != self.repo_id:
            raise ValueError("graph delta baseline source repo_id does not match")
        return self


class ChangeVerificationReport(ChangeContractModel):
    verification_id: str
    plan_id: str
    plan_revision: int
    baseline_build_identity: BuildIdentity
    baseline_source_identity: dict[str, Any]
    current_build_identity: BuildIdentity
    current_code_identity: CodeIdentity
    actual_changed_paths: list[ChangedPath] = Field(default_factory=list)
    changed_regions: list[ChangedRegion] = Field(default_factory=list)
    graph_delta: GraphDelta | None = None
    findings: list[dict[str, Any]] = Field(default_factory=list)
    residual_risk: list[dict[str, Any]] = Field(default_factory=list)
    verdict: VerificationVerdict
    verdict_reason: str
    # The immutable report retains the exact evidence records considered at
    # verification time.  Current material availability remains deliberately
    # outside this record and is derived in ChangeVerificationReportView.
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_summary: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_report_references(self) -> "ChangeVerificationReport":
        nested = [
            self.baseline_build_identity,
            self.current_build_identity,
            self.current_code_identity,
            *self.actual_changed_paths,
            *self.changed_regions,
        ]
        if any(item.repo_id != self.repo_id for item in nested):
            raise ValueError("verification report nested repo_id does not match")
        if self.baseline_source_identity.get("repo_id") != self.repo_id:
            raise ValueError(
                "verification report baseline source repo_id does not match"
            )
        if self.graph_delta is not None and self.graph_delta.repo_id != self.repo_id:
            raise ValueError("verification report graph delta repo_id does not match")
        if self.evidence_ids != sorted(set(self.evidence_ids)):
            raise ValueError(
                "verification report evidence ids must be stable and unique"
            )
        return self


class ChangeVerificationReportView(ChangeContractModel):
    """Read-only current projection over an immutable verification report.

    Purging evidence never rewrites a historical report.  Consumers that need
    to know whether the exact evidence material remains available use this
    projection, which is rebuilt from immutable Evidence/Purge records and the
    controlled material projection on every read.
    """

    report: ChangeVerificationReport
    evidence_availability: dict[str, str] = Field(default_factory=dict)
    evidence_summary: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_report_repo(self) -> "ChangeVerificationReportView":
        if self.report.repo_id != self.repo_id:
            raise ValueError("verification report repo_id does not match view")
        return self


class BuildPin(ChangeContractModel):
    """Immutable pin creation record.

    Release authority is represented only by a separate ``PinReleaseEvent``.
    The nullable legacy projection fields remain for contract compatibility and
    are intentionally never mutated on the immutable pin record.
    """

    pin_id: str
    plan_id: str
    plan_revision: int
    plan_content_digest: str
    index_version: str
    build_relative_path: str
    created_at: datetime = Field(default_factory=utc_now)
    released_at: datetime | None = None
    release_reason: str | None = None


class PinReleaseEvent(ChangeContractModel):
    release_id: str
    pin_id: str
    plan_id: str
    plan_revision: int
    reason: str
    released_at: datetime = Field(default_factory=utc_now)


class EvidencePurgeEvent(ChangeContractModel):
    purge_id: str
    plan_id: str
    plan_revision: int
    evidence_id: str
    requirement_id: str
    actor: str
    reason: str
    purge_mode: Literal["remove_material", "replace_with_redacted"]
    artifact_digest: str
    redacted_material_digest: str | None = None
    recorded_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_purge_mode(self) -> "EvidencePurgeEvent":
        if (
            self.purge_mode == "replace_with_redacted"
            and not self.redacted_material_digest
        ):
            raise ValueError(
                "replace_with_redacted requires a redacted material digest"
            )
        if (
            self.purge_mode == "remove_material"
            and self.redacted_material_digest is not None
        ):
            raise ValueError(
                "remove_material must not provide a redacted material digest"
            )
        return self


class ChangeAuditEvent(ChangeContractModel):
    audit_event_id: str
    event_type: str
    plan_id: str
    plan_revision: int = Field(ge=1)
    subject_id: str | None = None
    reason: str | None = None
    recorded_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)


class ArchiveRecord(ChangeContractModel):
    archive_id: str
    plan_id: str
    plan_revision: int
    plan_content_digest: str
    status_before_archive: PlanStatus
    archived_at: datetime = Field(default_factory=utc_now)
    actor: str
    reason: str


def verification_requirement_summary(plan: VerificationPlan) -> dict[str, Any]:
    """Derive a read-only summary; never persist it as a second authority."""

    by_level: dict[str, int] = {"required": 0, "recommended": 0, "optional": 0}
    for requirement in plan.requirements:
        by_level[requirement.level] += 1
    return {
        "total": len(plan.requirements),
        "by_level": by_level,
        "requirement_ids": sorted(
            requirement.requirement_id for requirement in plan.requirements
        ),
    }
