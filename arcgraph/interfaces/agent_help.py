"""Bounded, structured help for ArcGraph Agent surfaces."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from arcgraph import __version__
from arcgraph.interfaces.agent_capabilities import (
    AgentCapabilitySpec,
    capability_by_name,
    mcp_capability_specs,
)

AGENT_HELP_SCHEMA_VERSION = "1.0.0"
HelpTopic = Literal[
    "overview",
    "workflow",
    "tool_selection",
    "result_contract",
    "recovery",
    "feedback",
]
HelpSurface = Literal["cli", "mcp", "all"]
HELP_TOPICS: tuple[HelpTopic, ...] = (
    "overview",
    "workflow",
    "tool_selection",
    "result_contract",
    "recovery",
    "feedback",
)
HELP_SURFACES: tuple[HelpSurface, ...] = ("cli", "mcp", "all")


class _StrictHelpModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentHelpBehavior(_StrictHelpModel):
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool


class AgentHelpCapability(_StrictHelpModel):
    name: str
    title: str
    description: str
    cost: Literal["low", "medium", "high", "variable"]
    when_to_use: str
    avoid_when: str
    prerequisites: list[str]
    result_signals: list[str]
    recovery: list[str]
    cli_fallback: str | None = None
    behavior: AgentHelpBehavior


class AgentHelpSelection(_StrictHelpModel):
    name: str
    cost: Literal["low", "medium", "high", "variable"]
    when_to_use: str


class AgentHelpInventory(_StrictHelpModel):
    mcp_tool_count: int
    mcp_tools: list[str]
    cli_command_count: int
    cli_commands: list[str]


class AgentHelpFeedback(_StrictHelpModel):
    enabled: bool
    cli_available: bool
    mcp_enabled: bool
    tool_registered: bool
    tool_name: str
    persistence: Literal["disabled", "local_append_only"]
    guidance: str


class AgentHelpResponse(_StrictHelpModel):
    schema_version: Literal["1.0.0"] = AGENT_HELP_SCHEMA_VERSION
    product_version: str
    status: Literal["available", "unavailable"]
    topic: HelpTopic
    surface: HelpSurface
    read_only: Literal[True] = True
    source_snippets: dict[str, bool]
    inventory: AgentHelpInventory
    feedback: AgentHelpFeedback
    guidance: list[str]
    tool: AgentHelpCapability | None = None
    tool_selection: list[AgentHelpSelection]
    warnings: list[str]


_TOPIC_GUIDANCE: dict[HelpTopic, tuple[str, ...]] = {
    "overview": (
        "ArcGraph provides local structural code intelligence through a full CLI and a bounded MCP query surface.",
        "MCP is the primary Agent query surface; CLI remains the indexing, recovery, operations, and complete command surface.",
        "Check index status before relying on an ArcGraph answer.",
        "Use the narrowest registered tool that answers the question.",
        "Inspect result status, freshness, warnings, truncation, resolution, and capabilities before acting.",
    ),
    "workflow": (
        "Call arcgraph_index_status before the first substantive query and after source changes.",
        "If freshness is stale, use arcgraph sync --if-stale; if it is unknown or unavailable, inspect arcgraph doctor and use arcgraph build when no compatible index exists.",
        "Choose the narrowest suitable registered MCP tool and provide concrete targets.",
        "Inspect status, freshness, warnings, truncation, resolved targets, and capabilities in the result.",
        "Keep MCP-only evidence distinct from any shell CLI, grep, or direct-file fallback.",
        "When feedback is enabled, record inaccurate, unavailable, undiscoverable, truncated, slow, missing-capability, or fallback-requiring behavior.",
    ),
    "tool_selection": (
        "Use arcgraph_index_status for the cheapest freshness and capability check.",
        "Prefer one focused target over a broad multi-target context request.",
        "Use explain for resolution provenance, get_risk for blast radius, and get_context for a broader task briefing.",
        "Relative cost is guidance rather than a fixed token promise; repository fan-out and requested detail change payload size.",
        "CLI-only commands are fallbacks, not evidence that the MCP surface exposed the same capability.",
    ),
    "result_contract": (
        "Treat status as the operation outcome and freshness as the index currency signal.",
        "Warnings may contain strings or structured objects and must not be joined as strings without type checks.",
        "Truncation means the payload is incomplete even when status is available.",
        "Unresolved targets, degraded capabilities, and warning_scope_unavailable reduce confidence independently of freshness.",
        "Source snippets remain disabled unless the server operator explicitly enables them.",
        "explain keeps callable neighbors separate from relations (reads/writes/enqueues/consumes). Inspect call_scope and relations.scope; empty callers are not an empty resource adjacency list. Each relations section has a 32 KiB budget, per-direction item limits and two evidence locations per edge.",
        "get_risk prioritizes non-test entrypoints and reports found/returned/omitted counts. Non-test does not prove production use; traversal limits still apply.",
        "Unresolved risk items include inclusion_scope and source_scope. The selection expands affected symbols/modules to their files; same_file is not proof of a dependency. release_blocking classifies an indexed diagnostic, not the proposed change.",
    ),
    "recovery": (
        "Use arcgraph doctor when the environment, project roots, or index state is unclear.",
        "Use arcgraph build for a missing or incompatible index.",
        "Use arcgraph sync --if-stale for the normal explicit stale-index recovery path.",
        "Refine targets when resolution is partial or empty. For entrypoint_flow use METHOD /path, mcp_tool:name, worker:queue:name, or select a returned suggestion. Function names need not be entrypoint IDs.",
        "Use a narrower tool or request when truncation prevents a safe conclusion.",
        "detail_level accepts summary, standard, or detailed only. For explain/get_context, detailed follows max_results up to 100 and the server cap; summary stays at 6 and standard at 8.",
        "entrypoint_flow returns compact records within 64 KiB. Inspect analysis_limits and truncation separately; raise traversal limits or narrow the entrypoint, then explain returned node ids for detail.",
        "Record a trial feedback event when recovery is unclear, fails, or requires an undocumented fallback.",
    ),
    "feedback": (
        "Trial feedback is an explicit local append operation and is disabled unless the operator selects a feedback log.",
        "Use feedback for ArcGraph product experience, not arcgraph_record_learning, which only creates a non-persistent project-memory candidate.",
        "Feedback accepts bounded enums and identifiers, not source, paths, targets, prompts, repository ids, raw exceptions, or free-form narrative.",
        "A human must review privacy-bounded summaries before anything is shared.",
    ),
}


def build_agent_help(
    *,
    topic: HelpTopic = "overview",
    tool_name: str | None = None,
    surface: HelpSurface = "mcp",
    feedback_enabled: bool = False,
    cli_commands: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build one bounded Agent help result without repository state."""

    specs = mcp_capability_specs(feedback_enabled=feedback_enabled)
    registered_names = {spec.name for spec in specs}
    mcp_tools = [spec.name for spec in specs] if surface in {"mcp", "all"} else []
    all_commands = list(dict.fromkeys(cli_commands))
    commands = all_commands if surface in {"cli", "all"} else []
    cli_feedback_available = "feedback" in all_commands
    warnings: list[str] = []
    status: Literal["available", "unavailable"] = "available"
    guidance = list(_TOPIC_GUIDANCE[topic])

    tool: AgentHelpCapability | None = None
    if tool_name:
        spec = capability_by_name(tool_name, feedback_enabled=True)
        if spec is None:
            status = "unavailable"
            warnings.append("The requested ArcGraph MCP tool is unknown.")
        else:
            tool = _capability_projection(spec)
            if spec.name not in registered_names:
                warnings.append(
                    "The requested ArcGraph MCP tool is known but is not registered "
                    "in this server mode."
                )

    selection = (
        [
            AgentHelpSelection(
                name=spec.name,
                cost=spec.cost,
                when_to_use=spec.when_to_use,
            )
            for spec in specs
        ]
        if topic == "tool_selection" and surface in {"mcp", "all"}
        else []
    )
    feedback_registered = any(
        spec.name == "arcgraph_record_trial_feedback" for spec in specs
    )
    feedback_available_on_surface = {
        "cli": cli_feedback_available,
        "mcp": feedback_enabled,
        "all": feedback_enabled or cli_feedback_available,
    }[surface]
    response = AgentHelpResponse(
        product_version=__version__,
        status=status,
        topic=topic,
        surface=surface,
        source_snippets={"requested": False, "enabled": False},
        inventory=AgentHelpInventory(
            mcp_tool_count=len(mcp_tools),
            mcp_tools=mcp_tools,
            cli_command_count=len(commands),
            cli_commands=commands,
        ),
        feedback=AgentHelpFeedback(
            enabled=feedback_available_on_surface,
            cli_available=cli_feedback_available,
            mcp_enabled=feedback_enabled,
            tool_registered=feedback_registered,
            tool_name="arcgraph_record_trial_feedback",
            persistence=(
                "local_append_only" if feedback_available_on_surface else "disabled"
            ),
            guidance=(
                "Call arcgraph_record_trial_feedback when ArcGraph is inaccurate, "
                "unavailable, hard to discover, truncated, slow, missing a capability, "
                "or requires fallback."
                if feedback_enabled
                else (
                    "Use `arcgraph feedback record --feedback-log ABSOLUTE_PATH` "
                    "for strict local feedback when the Agent host permits CLI "
                    "execution; otherwise ask the operator to enable the MCP "
                    "feedback tool with an explicit private log."
                    if cli_feedback_available
                    else "Ask the operator to restart the MCP server with an explicit "
                    "private feedback log when local trial feedback is required."
                )
            ),
        ),
        guidance=guidance,
        tool=tool,
        tool_selection=selection,
        warnings=warnings,
    )
    return response.model_dump(mode="json", exclude_none=False)


def format_agent_help_human(payload: dict[str, Any]) -> str:
    """Render a compact human view from the validated machine payload."""

    response = AgentHelpResponse.model_validate(payload)
    lines = [
        f"ArcGraph Agent Help — {response.topic}",
        f"Status: {response.status}",
    ]
    if response.inventory.mcp_tools:
        lines.append(
            f"MCP tools: {response.inventory.mcp_tool_count} registered in this mode"
        )
    if response.inventory.cli_commands:
        lines.append(f"CLI commands: {response.inventory.cli_command_count} available")
    lines.extend(f"- {item}" for item in response.guidance)
    if response.tool is not None:
        lines.extend(
            [
                "",
                f"{response.tool.title} ({response.tool.name})",
                response.tool.description,
                f"Relative cost: {response.tool.cost}",
                f"Use when: {response.tool.when_to_use}",
                f"Avoid when: {response.tool.avoid_when}",
            ]
        )
        lines.extend(f"Prerequisite: {item}" for item in response.tool.prerequisites)
        lines.extend(f"Recovery: {item}" for item in response.tool.recovery)
    if response.tool_selection:
        lines.extend(
            f"- {item.name} [{item.cost}]: {item.when_to_use}"
            for item in response.tool_selection
        )
    if response.feedback.mcp_enabled:
        lines.append("Feedback: MCP enabled; local append only")
    elif response.feedback.cli_available:
        lines.append("Feedback: CLI available with an explicit local log; MCP disabled")
    else:
        lines.append("Feedback: disabled")
    lines.extend(f"Warning: {warning}" for warning in response.warnings)
    return "\n".join(lines) + "\n"


def _capability_projection(spec: AgentCapabilitySpec) -> AgentHelpCapability:
    return AgentHelpCapability(
        name=spec.name,
        title=spec.title,
        description=spec.description,
        cost=spec.cost,
        when_to_use=spec.when_to_use,
        avoid_when=spec.avoid_when,
        prerequisites=list(spec.prerequisites),
        result_signals=list(spec.result_signals),
        recovery=list(spec.recovery),
        cli_fallback=spec.cli_fallback,
        behavior=AgentHelpBehavior(
            read_only=spec.behavior.read_only,
            destructive=spec.behavior.destructive,
            idempotent=spec.behavior.idempotent,
            open_world=spec.behavior.open_world,
        ),
    )
