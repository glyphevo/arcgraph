"""Authoritative metadata for Agent-visible ArcGraph capabilities.

The MCP protocol description, safety annotations, Agent help, and metrics
allowlist all derive from these specifications. Exact CLI syntax remains owned
by argparse rather than being copied here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from arcgraph.core.recovery import STALE_RECOVERY_COMMAND

CapabilityAvailability = Literal["always", "feedback_enabled"]
CapabilityCost = Literal["low", "medium", "high", "variable"]


@dataclass(frozen=True, slots=True)
class ToolBehaviorHints:
    """Local representation of MCP ToolAnnotations without an MCP import."""

    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool


@dataclass(frozen=True, slots=True)
class AgentCapabilitySpec:
    """One authoritative Agent-facing MCP capability specification."""

    name: str
    title: str
    description: str
    availability: CapabilityAvailability
    behavior: ToolBehaviorHints
    when_to_use: str
    avoid_when: str
    prerequisites: tuple[str, ...]
    cost: CapabilityCost
    result_signals: tuple[str, ...]
    recovery: tuple[str, ...]
    cli_fallback: str | None = None


_READ_ONLY_LOCAL = ToolBehaviorHints(
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
_READ_ONLY_OPTIONAL_EXTERNAL_CONTEXT = ToolBehaviorHints(
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=True,
)
_LOCAL_IDEMPOTENT_APPEND = ToolBehaviorHints(
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=False,
)


MCP_CAPABILITY_SPECS: tuple[AgentCapabilitySpec, ...] = (
    AgentCapabilitySpec(
        name="arcgraph_index_status",
        title="ArcGraph Index Status",
        description=(
            "Return ArcGraph index freshness, counts, and capabilities for a "
            "registered repository."
        ),
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use="Use before relying on ArcGraph answers and after source changes.",
        avoid_when="Do not treat an unknown or stale result as a current code answer.",
        prerequisites=("The MCP server must be registered for the repository.",),
        cost="low",
        result_signals=("status", "freshness", "warnings", "capabilities"),
        recovery=(
            "Use the CLI to build an unavailable index.",
            f"Use `{STALE_RECOVERY_COMMAND}` when the index is stale.",
        ),
        cli_fallback="arcgraph current",
    ),
    AgentCapabilitySpec(
        name="arcgraph_get_context",
        title="ArcGraph Get Context",
        description=(
            "Return compact structural context for code targets before editing or "
            "review."
        ),
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use=(
            "Use for a bounded structural briefing around one or more concrete targets."
        ),
        avoid_when="Avoid as a substitute for exact source text or a narrow symbol lookup.",
        prerequisites=(
            "Check index freshness first.",
            "Prefer precise symbol or repository-relative path targets.",
        ),
        cost="high",
        result_signals=(
            "status",
            "freshness",
            "impact.target_resolution",
            "assurance",
            "warnings",
            "truncation",
        ),
        recovery=(
            "Refine unresolved targets.",
            "Request a narrower target or lower detail when the payload is truncated.",
        ),
        cli_fallback="arcgraph context",
    ),
    AgentCapabilitySpec(
        name="arcgraph_explain",
        title="ArcGraph Explain",
        description=(
            "Explain target resolution, direct edge evidence, resolution fallbacks, "
            "and confidence sources. Resource/queue reads, writes, enqueues and "
            "consumes appear separately in explanations.relations."
        ),
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use="Use to understand why ArcGraph resolved a target or inferred an edge.",
        avoid_when="Avoid when only a compact status or definition lookup is needed.",
        prerequisites=(
            "Check index freshness first.",
            "Provide concrete targets rather than an unconstrained task.",
        ),
        cost="high",
        result_signals=(
            "status",
            "freshness",
            "explanations.resolved_targets",
            "warnings",
            "truncation",
        ),
        recovery=(
            "Refine unresolved targets.",
            "Inspect warnings before trusting fallback resolution.",
        ),
        cli_fallback="arcgraph explain",
    ),
    AgentCapabilitySpec(
        name="arcgraph_get_risk",
        title="ArcGraph Get Risk",
        description=(
            "Run a bounded Change Preflight with resolution, impact, tests, similar "
            "implementations, entrypoints, assurance, and next reads."
        ),
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use="Use before changing resolved targets or a bounded changed-file set.",
        avoid_when="Avoid as proof that an unindexed runtime behavior is safe.",
        prerequisites=(
            "Check index freshness first.",
            "Provide targets, changed files, or both.",
        ),
        cost="high",
        result_signals=(
            "status",
            "freshness",
            "target_reports.target_resolution",
            "target_reports.analysis_limits",
            "assurance",
            "warnings",
            "truncation",
        ),
        recovery=(
            "Refine unresolved paths or symbols.",
            "Use narrower targets when the report is truncated.",
        ),
        cli_fallback="arcgraph impact",
    ),
    AgentCapabilitySpec(
        name="arcgraph_entrypoint_flow",
        title="ArcGraph Entrypoint Flow",
        description=(
            "Trace a route, MCP tool, worker, or queue entrypoint through indexed "
            "flow edges."
        ),
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use="Use to follow a known entrypoint into indexed dependencies.",
        avoid_when="Avoid for arbitrary symbols that are not entrypoints.",
        prerequisites=(
            "Check index freshness first.",
            "Provide an entrypoint or an HTTP method and path.",
        ),
        cost="medium",
        result_signals=(
            "status",
            "freshness",
            "target_resolution",
            "analysis_limits",
            "warnings",
            "truncation",
        ),
        recovery=(
            "Use METHOD /path, mcp_tool:name, or worker:queue:name; a handler qualname may return registered entrypoint suggestions.",
            "A wrong HTTP method may suggest the same path under another method; select it explicitly.",
            "Use explain when the entrypoint does not resolve.",
            "Inspect traversal limits separately from response omissions; the compact JSON budget is 64 KiB.",
            "Narrow the entrypoint or explain returned node ids for details when the response budget is reached.",
        ),
        cli_fallback="arcgraph route or arcgraph worker",
    ),
    AgentCapabilitySpec(
        name="arcgraph_get_why",
        title="ArcGraph Get Why",
        description=(
            "Explain structural reasons and, when configured, external historical "
            "memory around a target."
        ),
        availability="always",
        behavior=_READ_ONLY_OPTIONAL_EXTERNAL_CONTEXT,
        when_to_use="Use to combine structural reasons with configured historical context.",
        avoid_when="Avoid assuming optional external memory is configured or authoritative.",
        prerequisites=(
            "Check index freshness first.",
            "Provide one concrete target.",
        ),
        cost="medium",
        result_signals=(
            "status",
            "freshness",
            "resolved_targets",
            "external_memory",
            "warnings",
        ),
        recovery=(
            "Treat unavailable external memory as a documented degradation.",
            "Use explain for purely structural evidence.",
        ),
        cli_fallback=None,
    ),
    AgentCapabilitySpec(
        name="arcgraph_find_similar",
        title="ArcGraph Find Similar",
        description=(
            "Find indexed implementations with similar normalized AST structure and "
            "supporting evidence."
        ),
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use="Use to find structurally similar indexed implementations.",
        avoid_when="Avoid treating structural similarity as semantic equivalence.",
        prerequisites=(
            "Check index freshness first.",
            "Provide one resolvable symbol target.",
        ),
        cost="medium",
        result_signals=(
            "status",
            "freshness",
            "resolved_targets",
            "warnings",
            "truncation",
        ),
        recovery=(
            "Resolve the target with explain when no implementation is found.",
            "Inspect evidence before copying a similar implementation.",
        ),
        cli_fallback="arcgraph similar",
    ),
    AgentCapabilitySpec(
        name="arcgraph_record_learning",
        title="ArcGraph Record Learning Candidate",
        description=(
            "Create a read-only external memory candidate; does not persist memory."
        ),
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use="Use to draft a reviewable learning candidate from indexed context.",
        avoid_when="Do not use as product feedback or assume the candidate was persisted.",
        prerequisites=(
            "Check index freshness first.",
            "Provide one resolvable target.",
        ),
        cost="medium",
        result_signals=("status", "freshness", "memory_candidate", "warnings"),
        recovery=(
            "Review and persist the candidate through an external approved workflow.",
            "Use the trial feedback tool, when enabled, for ArcGraph product feedback.",
        ),
        cli_fallback=None,
    ),
    AgentCapabilitySpec(
        name="arcgraph_preview_change_plan",
        title="ArcGraph Preview Change Plan",
        description=(
            "Compute a fail-closed surgical-change plan without persisting a plan or "
            "pin."
        ),
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use="Use to preview the allowed scope and verification requirements.",
        avoid_when="Do not treat a preview as an approved or persisted plan.",
        prerequisites=(
            "Use a clean, current index.",
            "Provide a task and explicit structured targets.",
        ),
        cost="high",
        result_signals=("status", "verdict", "error_code", "warnings"),
        recovery=(
            "Resolve blocked or invalid targets before proceeding.",
            "Use the CLI for persistent plan lifecycle operations.",
        ),
        cli_fallback="arcgraph change plan",
    ),
    AgentCapabilitySpec(
        name="arcgraph_get_change_plan",
        title="ArcGraph Get Change Plan",
        description=(
            "Read a current surgical-change plan view and redacted evidence summary."
        ),
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use="Use to inspect one already-persisted change plan.",
        avoid_when="Do not use to approve, reject, archive, or mutate a plan.",
        prerequisites=("Provide an exact persisted plan id.",),
        cost="low",
        result_signals=("status", "verdict", "error_code", "data"),
        recovery=("Use the CLI to inspect or change persistent lifecycle state.",),
        cli_fallback="arcgraph change show",
    ),
    AgentCapabilitySpec(
        name="arcgraph_list_change_plans",
        title="ArcGraph List Change Plans",
        description="List current surgical-change plan views without mutation.",
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use="Use to discover persisted change-plan ids and current views.",
        avoid_when="Do not infer archival or approval actions from this read-only list.",
        prerequisites=("The repository must have initialized Change Safety storage.",),
        cost="low",
        result_signals=("status", "error_code", "data"),
        recovery=("Use the CLI for lifecycle actions or storage diagnostics.",),
        cli_fallback="arcgraph change list",
    ),
    AgentCapabilitySpec(
        name="arcgraph_get_graph_delta",
        title="ArcGraph Get Graph Delta",
        description=(
            "Compute a current graph delta for one exact surgical-change revision "
            "without persisting a report."
        ),
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use="Use to compare an exact persisted plan revision with current state.",
        avoid_when="Do not use a delta from a different revision or content digest.",
        prerequisites=(
            "Provide the exact plan id, revision, and plan content digest.",
            "Refresh the index after code changes.",
        ),
        cost="high",
        result_signals=("status", "verdict", "error_code", "data"),
        recovery=(
            "Reload the plan view when revision or digest validation fails.",
            "Use the CLI for a persisted lifecycle workflow.",
        ),
        cli_fallback="arcgraph change diff",
    ),
    AgentCapabilitySpec(
        name="arcgraph_verify_change",
        title="ArcGraph Verify Change",
        description=(
            "Compute a surgical-change verification result without writing a report "
            "or lifecycle event."
        ),
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use="Use to compute a non-persistent verdict for one exact revision.",
        avoid_when="Do not treat the MCP result as a persisted verification transition.",
        prerequisites=(
            "Provide the exact plan id, revision, and plan content digest.",
            "Refresh the index and record required evidence through approved CLI flows.",
        ),
        cost="high",
        result_signals=("status", "verdict", "error_code", "data"),
        recovery=(
            "Address the highest-priority blocking verdict.",
            "Use the CLI to persist an authoritative verification report.",
        ),
        cli_fallback="arcgraph change verify",
    ),
    AgentCapabilitySpec(
        name="arcgraph_help",
        title="ArcGraph Help",
        description=(
            "Return bounded Agent guidance for tool selection, result "
            "interpretation, recovery, and trial feedback."
        ),
        availability="always",
        behavior=_READ_ONLY_LOCAL,
        when_to_use=(
            "Use to discover the ArcGraph workflow or inspect one registered tool."
        ),
        avoid_when="Do not use as a substitute for current index status.",
        prerequisites=("No repository index is required.",),
        cost="low",
        result_signals=("status", "inventory", "feedback", "warnings"),
        recovery=("Choose a listed topic or an exactly registered MCP tool name.",),
        cli_fallback="arcgraph help",
    ),
    AgentCapabilitySpec(
        name="arcgraph_record_trial_feedback",
        title="ArcGraph Record Trial Feedback",
        description=(
            "Record one privacy-bounded Agent trial feedback event in an "
            "explicitly enabled local append-only log."
        ),
        availability="feedback_enabled",
        behavior=_LOCAL_IDEMPOTENT_APPEND,
        when_to_use=(
            "Use when ArcGraph is inaccurate, unavailable, hard to discover, "
            "truncated, slow, missing a capability, or requires fallback."
        ),
        avoid_when=(
            "Do not include source, paths, targets, prompts, repository ids, "
            "raw exceptions, or free-form narrative."
        ),
        prerequisites=(
            "The server operator must explicitly select an absolute feedback log.",
            "Use a new client event UUID or repeat an identical event idempotently.",
        ),
        cost="low",
        result_signals=(
            "status",
            "feedback_id",
            "duplicate",
            "error_code",
            "warnings",
        ),
        recovery=(
            "Use only documented enum values and registered operation names.",
            "Ask the operator to inspect local storage permissions or size limits.",
        ),
        cli_fallback="arcgraph feedback record",
    ),
)


def mcp_capability_specs(
    *, feedback_enabled: bool = False
) -> tuple[AgentCapabilitySpec, ...]:
    """Return capabilities registered for one MCP server mode."""

    return tuple(
        spec
        for spec in MCP_CAPABILITY_SPECS
        if spec.availability == "always"
        or (feedback_enabled and spec.availability == "feedback_enabled")
    )


def mcp_capability_names(*, feedback_enabled: bool = False) -> tuple[str, ...]:
    return tuple(
        spec.name for spec in mcp_capability_specs(feedback_enabled=feedback_enabled)
    )


def mcp_tool_descriptions(*, feedback_enabled: bool = False) -> dict[str, str]:
    return {
        spec.name: spec.description
        for spec in mcp_capability_specs(feedback_enabled=feedback_enabled)
    }


def capability_by_name(
    name: str, *, feedback_enabled: bool = False
) -> AgentCapabilitySpec | None:
    return next(
        (
            spec
            for spec in mcp_capability_specs(feedback_enabled=feedback_enabled)
            if spec.name == name
        ),
        None,
    )


_CAPABILITY_NAMES = tuple(spec.name for spec in MCP_CAPABILITY_SPECS)
if len(_CAPABILITY_NAMES) != len(set(_CAPABILITY_NAMES)):
    raise RuntimeError("ArcGraph MCP capability names must be unique.")

ALL_MCP_CAPABILITY_NAMES = frozenset(_CAPABILITY_NAMES)
