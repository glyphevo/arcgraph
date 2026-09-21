"""Pydantic schemas for ArcGraph core and agent-facing boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Persisted graph/index and raw QueryEngine contract.
SCHEMA_VERSION = "1.0.0"
# Bounded target-scoped CLI/native/MCP response contract.
READ_SCHEMA_VERSION = "1.4.0"
SCHEMA_STABLE_TARGET_VERSION = "1.0.0"
V2_RECEIVER_RESOLUTION_DEFAULT_SCHEMA_VERSION = "1.0.0"
SCHEMA_ROADMAP = ("0.6.0", "0.7.0", "0.8.0", "0.9.0", "1.0.0")
# 0.7.0, 0.8.0, and 0.9.0 are rollout readiness checkpoints from the
# implementation plan. They were not published as separate stable runtime
# schemas, so existing 0.6.0 indexes should be rebuilt directly to 1.0.0.
SCHEMA_COMPATIBILITY_STRATEGIES = {
    ("0.6.0", "0.7.0"): (
        "full-rebuild",
        True,
        "bindings, callsites, type_refs, and semantic metrics require rebuild.",
    ),
    ("0.7.0", "0.8.0"): (
        "full-rebuild",
        True,
        "precision facts can be imported incrementally, but source roots should refresh.",
    ),
    ("0.8.0", "0.9.0"): (
        "incremental-import",
        False,
        "runtime trace facts are additive and can be imported into an existing graph.",
    ),
    ("0.9.0", "1.0.0"): (
        "compatibility-check",
        False,
        "stable frontend contract is accepted when compatibility checks pass.",
    ),
}
SCHEMA_DIRECT_COMPATIBILITY_STRATEGIES = {
    ("0.6.0", "1.0.0"): (
        "full-rebuild",
        True,
        "0.7.0, 0.8.0, and 0.9.0 were readiness checkpoints, not separately "
        "published stable runtime schemas; rebuild 0.6.0 indexes directly to 1.0.0.",
    ),
}

Confidence = Literal["confirmed", "inferred", "heuristic", "runtime-only", "unresolved"]
FactKind = Literal["node", "edge", "diagnostic", "metric"]
FactResolutionStatus = Literal[
    "resolved", "unresolved", "runtime-only", "partial", "conflict"
]
DiagnosticSeverity = Literal["info", "warning", "error"]
DetailLevel = Literal["summary", "standard", "detailed"]
AgentContextPolicy = Literal[
    "coding_change",
    "review",
    "debugging",
    "architecture_investigation",
]

CONFIDENCE_RANK: dict[Confidence, int] = {
    "unresolved": 0,
    "heuristic": 1,
    "runtime-only": 2,
    "inferred": 3,
    "confirmed": 4,
}


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    column: int | None = None
    detail: str | None = None
    snippet: str | None = None


class FactEvidence(Evidence):
    """Evidence attached to a persisted semantic fact."""


EvidenceKey = tuple[
    str, str | None, int | None, int | None, int | None, str | None, str | None
]


def evidence_key(evidence: Evidence) -> EvidenceKey:
    return (
        evidence.kind,
        evidence.path,
        evidence.start_line,
        evidence.end_line,
        evidence.column,
        evidence.detail,
        evidence.snippet,
    )


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    name: str
    qualname: str | None = None
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    canonical_identity: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class FactResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FactResolutionStatus = "resolved"
    strategy: str | None = None
    candidate_count: int | None = None
    callsite_id: str | None = None
    supersedes: str | None = None
    detail: str | None = None
    fallbacks: list[str] = Field(default_factory=list)


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    kind: str
    confidence: Confidence = "confirmed"
    semantic_role: str | None = None
    resolution: FactResolution = Field(default_factory=FactResolution)
    confidence_sources: dict[str, list[str]] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


class FileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    abs_path: str
    source_root: str
    module: str
    file_hash: str
    line_count: int
    file_size: int = 0
    is_package: bool = False


class BuildWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    message: str
    path: str | None = None
    # Set by the producing frontend. Warning-kind prefixes are conventions, not
    # a provenance contract, so consumers that replace frontend-derived state
    # must use this field rather than infer ownership from ``kind`` alone.
    frontend_name: str | None = None


class FrontendCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    language: str | None = None
    capabilities: dict[str, str] = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)
    file_extensions: list[str] = Field(default_factory=list)


class FrontendAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_root: str
    repo_id: str = "default"
    index_version: str | None = None
    source_roots: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    full_rebuild: bool = True


class SemanticDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnostic_id: str
    repo_id: str = "default"
    index_version: str
    diagnostic_kind: str
    message: str
    severity: DiagnosticSeverity = "warning"
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    frontend_name: str = "platform"
    fact_id: str | None = None
    first_seen_index: str | None = None
    last_seen_index: str | None = None
    seen_count: int = 1
    properties: dict[str, Any] = Field(default_factory=dict)


class SemanticFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    fact_id: str
    repo_id: str = "default"
    index_version: str
    language: str | None = None
    frontend_name: str
    frontend_version: str
    fact_kind: FactKind
    node_id: str | None = None
    source: str | None = None
    target: str | None = None
    edge_kind: str | None = None
    confidence: Confidence = "confirmed"
    semantic_role: str | None = None
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    resolution: FactResolution = Field(default_factory=FactResolution)
    evidence: list[FactEvidence] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FrontendAnalyzeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frontend: FrontendCapabilities
    facts: list[SemanticFact] = Field(default_factory=list)
    diagnostics: list[SemanticDiagnostic] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    status: Literal["available", "partial", "unavailable"] = "available"


class MergeMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str = "default"
    index_version: str
    facts_in: int = 0
    facts_merged: int = 0
    nodes_out: int = 0
    edges_out: int = 0
    diagnostics_count: int = 0
    conflicts_count: int = 0
    unresolved_count: int = 0
    truncated_count: int = 0
    confidence_counts: dict[str, int] = Field(default_factory=dict)
    edge_kind_counts: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SchemaMigrationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    migration_id: str
    from_schema_version: str | None = None
    to_schema_version: str = SCHEMA_VERSION
    strategy: str = "full-rebuild"
    required_rebuild: bool = True
    applied_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    notes: str | None = None


class SchemaCompatibilityStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_schema_version: str
    to_schema_version: str
    strategy: str
    required_rebuild: bool = True
    notes: str


def schema_compatibility_steps(
    from_schema_version: str = SCHEMA_VERSION,
    to_schema_version: str = SCHEMA_STABLE_TARGET_VERSION,
) -> list[SchemaCompatibilityStep]:
    if (
        from_schema_version not in SCHEMA_ROADMAP
        or to_schema_version not in SCHEMA_ROADMAP
    ):
        return []
    start = SCHEMA_ROADMAP.index(from_schema_version)
    end = SCHEMA_ROADMAP.index(to_schema_version)
    if start >= end:
        return []
    steps: list[SchemaCompatibilityStep] = []
    for index in range(start, end):
        source = SCHEMA_ROADMAP[index]
        target = SCHEMA_ROADMAP[index + 1]
        strategy, required_rebuild, notes = SCHEMA_COMPATIBILITY_STRATEGIES[
            (source, target)
        ]
        steps.append(
            SchemaCompatibilityStep(
                from_schema_version=source,
                to_schema_version=target,
                strategy=strategy,
                required_rebuild=required_rebuild,
                notes=notes,
            )
        )
    return steps


def schema_direct_compatibility_step(
    from_schema_version: str = "0.6.0",
    to_schema_version: str = SCHEMA_STABLE_TARGET_VERSION,
) -> SchemaCompatibilityStep | None:
    strategy = SCHEMA_DIRECT_COMPATIBILITY_STRATEGIES.get(
        (from_schema_version, to_schema_version)
    )
    if strategy is None:
        return None
    strategy_name, required_rebuild, notes = strategy
    return SchemaCompatibilityStep(
        from_schema_version=from_schema_version,
        to_schema_version=to_schema_version,
        strategy=strategy_name,
        required_rebuild=required_rebuild,
        notes=notes,
    )


def schema_version_reached(version: str, minimum: str) -> bool:
    if version in SCHEMA_ROADMAP and minimum in SCHEMA_ROADMAP:
        return SCHEMA_ROADMAP.index(version) >= SCHEMA_ROADMAP.index(minimum)
    return _schema_version_tuple(version) >= _schema_version_tuple(minimum)


def _schema_version_tuple(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    values: list[int] = []
    for part in parts[:3]:
        try:
            values.append(int(part))
        except ValueError:
            values.append(0)
    while len(values) < 3:
        values.append(0)
    return (values[0], values[1], values[2])


class IndexMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    index_version: str
    repo_root: str
    source_roots: list[str]
    source_root_specs: list[dict[str, str]] = Field(default_factory=list)
    source_root_detection: dict[str, Any] | None = None
    project_name: str | None = None
    project_name_source: str | None = None
    project_version: str | None = None
    project_version_source: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    commit_sha: str | None = None
    file_count: int = 0
    node_count: int = 0
    edge_count: int = 0
    warning_count: int = 0
    evidence_manifest: dict[str, Any] | None = None
    language_tiers: dict[str, dict[str, str]] = Field(default_factory=dict)
    extractor_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)
    toolchain_status: dict[str, dict[str, Any]] = Field(default_factory=dict)
    capabilities: dict[str, str] = Field(
        default_factory=lambda: {
            "scanner": "available",
            "imports": "available",
            "symbols": "available",
            "calls": "available",
            "incremental": "available",
            "framework_adapters": "available",
            "entrypoints": "available",
            "resources": "available",
            "architecture": "basic",
            "precise_references": "ast-fallback",
            "test_recommendations": "heuristic",
            "test_gaps": "available",
            "coverage": "unavailable",
            "runtime_trace": "unavailable",
            "precision": "ast_fallback_only",
            "similarity": "available",
            "semantic_facts": "available",
            "diagnostics": "available",
            "merge_metrics": "available",
            "semantic_stats": "available",
            "unresolved": "available",
            "bindings": "available",
            "types": "available",
            "receiver_resolution": "available",
        }
    )


class Freshness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["fresh", "stale", "unknown"] = "fresh"
    stale: bool = False
    stale_files: list[str] = Field(default_factory=list)
    stale_modules: list[str] = Field(default_factory=list)
    reason: str | None = None


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str | None = None
    targets: list[str] = Field(default_factory=list)
    max_results: int = 30
    detail_level: DetailLevel = "summary"
    profile: str = "review_default"
    include_source: bool = False


class CapabilitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reported: int = 0
    total: int = 0
    omitted_value: str = "available"
    detail: str = "Run `arcgraph current` for the full capability table."


ReadWarning = str | dict[str, Any]


class AssuranceIndexedScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index_version: str | None = None
    commit_sha: str | None = None
    source_roots: list[str] = Field(default_factory=list)
    file_count: int = 0
    freshness: Freshness = Field(default_factory=Freshness)


class AssuranceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    impact_edges_by_confidence: dict[str, int] = Field(default_factory=dict)
    precision: str = "unavailable"
    target_reference_verification: str = "not_requested"
    coverage: str = "unavailable"
    runtime_trace: str = "unavailable"


class AssuranceLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    traversal_truncated: bool = False
    analysis_data_truncated: bool = False
    response_truncated: bool = False
    unresolved_risk_count: int = 0
    unresolved_edge_count: int = 0
    dynamic_gap_count: int = 0
    analysis_truncation_count: int = 0
    response_truncation_count: int = 0


class AssuranceCompleteness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["not_established", "bounded_static_only"] = "not_established"
    scope: str = ""


class Assurance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    posture: Literal["do_not_rely", "limited", "bounded_support"] = "do_not_rely"
    indexed_scope: AssuranceIndexedScope = Field(default_factory=AssuranceIndexedScope)
    target_resolutions: list[dict[str, Any]] = Field(default_factory=list)
    target_resolution_summary: dict[str, int] = Field(default_factory=dict)
    evidence: AssuranceEvidence = Field(default_factory=AssuranceEvidence)
    limits: AssuranceLimits = Field(default_factory=AssuranceLimits)
    completeness: AssuranceCompleteness = Field(default_factory=AssuranceCompleteness)
    non_claims: list[str] = Field(default_factory=list)
    verification_required: list[str] = Field(default_factory=list)


class ContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.4.0"] = READ_SCHEMA_VERSION
    index_schema_version: str = SCHEMA_VERSION
    index_version: str | None = None
    status: Literal["available", "partial", "unavailable"] = "available"
    task: str | None = None
    targets: list[str] = Field(default_factory=list)
    detail_level: DetailLevel = "summary"
    confidence_profile: str = "review_default"
    max_results: int = 30
    capabilities: dict[str, str] = Field(default_factory=dict)
    capabilities_summary: CapabilitySummary = Field(default_factory=CapabilitySummary)
    freshness: Freshness = Field(default_factory=Freshness)
    symbols: list[dict[str, Any]] = Field(default_factory=list)
    entrypoints: list[dict[str, Any]] = Field(default_factory=list)
    resources: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    grouped_effects: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    unresolved_risks: list[dict[str, Any]] = Field(default_factory=list)
    impact: list[dict[str, Any]] = Field(default_factory=list)
    entrypoint_flows: list[dict[str, Any]] = Field(default_factory=list)
    test_candidates: list[dict[str, Any]] = Field(default_factory=list)
    test_gaps: list[dict[str, Any]] = Field(default_factory=list)
    architecture: dict[str, Any] | None = None
    recommended_next_reads: list[dict[str, Any]] = Field(default_factory=list)
    source_snippets: dict[str, Any] | None = None
    estimated_tokens: int = 0
    truncation: dict[str, Any] = Field(default_factory=dict)
    assurance: Assurance = Field(default_factory=Assurance)
    recovery_action: dict[str, Any] | None = None
    warnings: list[ReadWarning] = Field(default_factory=list)
    repo_id: str | None = None
    read_only: bool | None = None


class ExplainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.4.0"] = READ_SCHEMA_VERSION
    index_schema_version: str = SCHEMA_VERSION
    index_version: str | None = None
    status: Literal["available", "partial", "unavailable"] = "available"
    task: str | None = None
    targets: list[str] = Field(default_factory=list)
    detail_level: DetailLevel = "summary"
    confidence_profile: str = "review_default"
    max_results: int = 30
    capabilities: dict[str, str] = Field(default_factory=dict)
    capabilities_summary: CapabilitySummary = Field(default_factory=CapabilitySummary)
    freshness: Freshness = Field(default_factory=Freshness)
    explanations: list[dict[str, Any]] = Field(default_factory=list)
    recommended_next_reads: list[dict[str, Any]] = Field(default_factory=list)
    source_snippets: dict[str, Any] | None = None
    estimated_tokens: int = 0
    truncation: dict[str, Any] = Field(default_factory=dict)
    recovery_action: dict[str, Any] | None = None
    warnings: list[ReadWarning] = Field(default_factory=list)
    repo_id: str | None = None
    read_only: bool | None = None


class RiskReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.4.0"] = READ_SCHEMA_VERSION
    index_schema_version: str = SCHEMA_VERSION
    index_version: str | None = None
    status: Literal["available", "partial", "unavailable"] = "partial"
    capabilities: dict[str, str] = Field(default_factory=dict)
    capabilities_summary: CapabilitySummary = Field(default_factory=CapabilitySummary)
    freshness: Freshness = Field(default_factory=Freshness)
    targets: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    max_depth: int = 2
    max_results: int = 30
    blast_radius: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    entrypoint_summary: dict[str, Any] = Field(default_factory=dict)
    test_candidates: list[dict[str, Any]] = Field(default_factory=list)
    test_gaps: list[dict[str, Any]] = Field(default_factory=list)
    risk_factors: list[dict[str, Any]] = Field(default_factory=list)
    target_reports: list[dict[str, Any]] = Field(default_factory=list)
    similar_implementations: list[dict[str, Any]] = Field(default_factory=list)
    unknowns: list[dict[str, Any]] = Field(default_factory=list)
    recommended_next_reads: list[dict[str, Any]] = Field(default_factory=list)
    precise_references: list[dict[str, Any]] = Field(default_factory=list)
    estimated_tokens: int = 0
    truncation: dict[str, Any] = Field(default_factory=dict)
    assurance: Assurance = Field(default_factory=Assurance)
    recovery_action: dict[str, Any] | None = None
    warnings: list[ReadWarning] = Field(default_factory=list)
    repo_id: str | None = None
    read_only: bool | None = None
    source_snippets: dict[str, Any] | None = None


class AgentContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    repo_id: str = "default"
    targets: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    policy: AgentContextPolicy = "coding_change"
    max_results: int = 30
    detail_level: DetailLevel = "summary"
    profile: str = "review_default"


class AgentTaskContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.4.0"] = READ_SCHEMA_VERSION
    index_schema_version: str = SCHEMA_VERSION
    status: Literal["available", "partial", "unavailable"] = "available"
    repo_id: str = "default"
    task: str
    policy: AgentContextPolicy
    targets: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    max_results: int = 30
    detail_level: DetailLevel = "summary"
    profile: str = "review_default"
    freshness: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, str] = Field(default_factory=dict)
    capabilities_summary: CapabilitySummary = Field(default_factory=CapabilitySummary)
    index_status: dict[str, Any] = Field(default_factory=dict)
    structural_context: dict[str, Any] = Field(default_factory=dict)
    risk: dict[str, Any] = Field(default_factory=dict)
    why: dict[str, Any] = Field(default_factory=dict)
    similar_implementations: list[dict[str, Any]] = Field(default_factory=list)
    test_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    git_diff: dict[str, Any] = Field(default_factory=dict)
    memories: list[dict[str, Any]] = Field(default_factory=list)
    recommended_next_reads: list[dict[str, Any]] = Field(default_factory=list)
    estimated_tokens: int = 0
    truncation: dict[str, Any] = Field(default_factory=dict)
    warnings: list[ReadWarning] = Field(default_factory=list)


class MemoryRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    title: str
    content: str = ""
    memory_type: str
    score: float | None = None
    source: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WhyMemoryContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "partial", "unavailable"] = "unavailable"
    reason: str | None = None
    query: str | None = None
    memories: list[MemoryRef] = Field(default_factory=list)
    historical_decisions: list[MemoryRef] = Field(default_factory=list)
    lessons_learned: list[MemoryRef] = Field(default_factory=list)
    best_practices: list[MemoryRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WhyReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.4.0"] = READ_SCHEMA_VERSION
    index_schema_version: str = SCHEMA_VERSION
    index_version: str | None = None
    status: Literal["available", "partial", "unavailable"] = "partial"
    target: str = ""
    task: str | None = None
    resolved_targets: list[str] = Field(default_factory=list)
    freshness: Freshness = Field(default_factory=Freshness)
    capabilities: dict[str, str] = Field(default_factory=dict)
    capabilities_summary: CapabilitySummary = Field(default_factory=CapabilitySummary)
    structural_why: list[dict[str, Any]] = Field(default_factory=list)
    historical_decisions: list[dict[str, Any]] = Field(default_factory=list)
    lessons_learned: list[dict[str, Any]] = Field(default_factory=list)
    best_practices: list[dict[str, Any]] = Field(default_factory=list)
    external_memory: dict[str, Any] = Field(default_factory=dict)
    warnings: list[ReadWarning] = Field(default_factory=list)
    repo_id: str | None = None
    read_only: bool | None = None
    source_snippets: dict[str, Any] | None = None
    recovery_action: dict[str, Any] | None = None


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    memory_type: str = "lesson_learned"
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    source: str = "arcgraph_record_learning"
