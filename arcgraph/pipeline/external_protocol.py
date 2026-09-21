"""External semantic extractor protocol models and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arcgraph.core.schemas import (
    BuildWarning,
    Edge,
    FileRecord,
    FrontendCapabilities,
    Node,
)
from arcgraph.pipeline.contracts import FrontendGraphFragment

EXTERNAL_EXTRACTOR_SCHEMA_VERSION = "1"

ToolchainState = Literal[
    "available",
    "missing",
    "degraded",
    "timeout",
    "invalid_output",
    "unavailable",
]

_SEMANTIC_EDGE_KINDS = {
    "calls",
    "conforms",
    "declares",
    "defines",
    "extends",
    "has_annotation",
    "has_attribute",
    "has_field",
    "imports",
    "inherits",
    "implements",
    "invokes",
    "links_to",
    "overrides",
    "reads",
    "references",
    "renders",
    "type_ref",
    "uses",
    "writes",
}


class ExternalExtractorValidationError(ValueError):
    """Raised when an extractor payload violates the external protocol."""


class ToolchainRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    command: str | None = None
    required: bool = False
    version_args: list[str] = Field(default_factory=list)


class ToolchainStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: ToolchainState
    required: bool = False
    command: str | None = None
    version: str | None = None
    detail: str | None = None


class ExternalExtractorToolchain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolchainState = "available"
    tools: list[ToolchainStatus] = Field(default_factory=list)
    requirements: list[ToolchainRequirement] = Field(default_factory=list)


class ExternalExtractorIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str


class ExternalExtractorDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    message: str
    path: str | None = None


class ExternalExtractorCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: str
    supported_node_kinds: list[str] = Field(default_factory=list)
    supported_edge_kinds: list[str] = Field(default_factory=list)


class ExternalExtractorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXTERNAL_EXTRACTOR_SCHEMA_VERSION
    repo_root: str
    frontend_name: str
    frontend_version: str
    language: str
    files: list[dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def from_files(
        cls,
        *,
        repo_root: Path,
        frontend: FrontendCapabilities,
        language: str,
        files: list[FileRecord],
    ) -> "ExternalExtractorRequest":
        return cls(
            repo_root=str(repo_root),
            frontend_name=frontend.name,
            frontend_version=frontend.version,
            language=language,
            files=[
                {
                    "path": file.path,
                    "abs_path": file.abs_path,
                    "source_root": file.source_root,
                    "module": file.module,
                    "line_count": file.line_count,
                    "is_package": file.is_package,
                }
                for file in files
            ],
        )


class ExternalExtractorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    extractor: ExternalExtractorIdentity
    language: str
    capabilities: ExternalExtractorCapabilities
    toolchain: ExternalExtractorToolchain = Field(
        default_factory=ExternalExtractorToolchain
    )
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    warnings: list[ExternalExtractorDiagnostic] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


def validate_external_extractor_payload(
    raw_payload: Any,
    *,
    repo_root: Path,
    expected_frontend: FrontendCapabilities,
    supported_node_kinds: set[str],
    supported_edge_kinds: set[str],
) -> ExternalExtractorPayload:
    """Validate a raw external extractor payload against ArcGraph boundaries."""

    try:
        payload = ExternalExtractorPayload.model_validate(raw_payload)
    except ValidationError as exc:
        raise ExternalExtractorValidationError(str(exc)) from exc

    if payload.schema_version != EXTERNAL_EXTRACTOR_SCHEMA_VERSION:
        raise ExternalExtractorValidationError(
            f"Unsupported external extractor schema_version: {payload.schema_version}"
        )
    if payload.extractor.name != expected_frontend.name:
        raise ExternalExtractorValidationError(
            "External extractor name does not match registered frontend: "
            f"{payload.extractor.name} != {expected_frontend.name}"
        )
    if payload.extractor.version != expected_frontend.version:
        raise ExternalExtractorValidationError(
            "External extractor version does not match registered frontend: "
            f"{payload.extractor.version} != {expected_frontend.version}"
        )

    expected_tier = expected_frontend.capabilities.get("language_tier")
    if expected_tier and payload.capabilities.tier != expected_tier:
        raise ExternalExtractorValidationError(
            "External extractor tier does not match frontend capability: "
            f"{payload.capabilities.tier} != {expected_tier}"
        )

    declared_node_kinds = set(payload.capabilities.supported_node_kinds)
    declared_edge_kinds = set(payload.capabilities.supported_edge_kinds)
    if declared_node_kinds and not declared_node_kinds.issubset(supported_node_kinds):
        raise ExternalExtractorValidationError(
            "External extractor declared unsupported node kind(s): "
            f"{sorted(declared_node_kinds - supported_node_kinds)}"
        )
    if declared_edge_kinds and not declared_edge_kinds.issubset(supported_edge_kinds):
        raise ExternalExtractorValidationError(
            "External extractor declared unsupported edge kind(s): "
            f"{sorted(declared_edge_kinds - supported_edge_kinds)}"
        )

    for node in payload.nodes:
        if node.kind not in supported_node_kinds:
            raise ExternalExtractorValidationError(
                f"Unsupported external extractor node kind: {node.kind}"
            )
        _validate_repo_relative_path(repo_root, node.path, "node path")

    for edge in payload.edges:
        if edge.kind not in supported_edge_kinds:
            raise ExternalExtractorValidationError(
                f"Unsupported external extractor edge kind: {edge.kind}"
            )
        for evidence in edge.evidence:
            _validate_repo_relative_path(repo_root, evidence.path, "edge evidence path")
        if _requires_semantic_evidence(edge):
            if not edge.evidence:
                raise ExternalExtractorValidationError(
                    "External extractor semantic edge is missing evidence: "
                    f"{edge.source} {edge.kind} {edge.target}"
                )
            if not edge.resolution.strategy:
                raise ExternalExtractorValidationError(
                    "External extractor semantic edge is missing resolution.strategy: "
                    f"{edge.source} {edge.kind} {edge.target}"
                )

    for warning in payload.warnings:
        _validate_repo_relative_path(repo_root, warning.path, "warning path")

    return payload


def external_payload_to_fragment(
    raw_payload: Any,
    *,
    repo_root: Path,
    expected_frontend: FrontendCapabilities,
    supported_node_kinds: set[str],
    supported_edge_kinds: set[str],
) -> FrontendGraphFragment:
    """Convert a validated external extractor payload into a graph fragment."""

    payload = validate_external_extractor_payload(
        raw_payload,
        repo_root=repo_root,
        expected_frontend=expected_frontend,
        supported_node_kinds=supported_node_kinds,
        supported_edge_kinds=supported_edge_kinds,
    )
    return FrontendGraphFragment(
        nodes=payload.nodes,
        edges=payload.edges,
        warnings=[
            BuildWarning(kind=warning.kind, message=warning.message, path=warning.path)
            for warning in payload.warnings
        ],
        adapter_metrics={
            **payload.metrics,
            "status": "available",
            "schema_version": payload.schema_version,
            "language": payload.language,
        },
        frontend=expected_frontend,
        extractor_metadata={
            "name": payload.extractor.name,
            "version": payload.extractor.version,
            "language": payload.language,
            "tier": payload.capabilities.tier,
        },
        toolchain_status=payload.toolchain.model_dump(mode="json"),
    )


def summarize_toolchain_status(
    toolchains: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return a CI/current friendly summary for external extractor toolchains."""

    unavailable_states = {
        "missing",
        "degraded",
        "timeout",
        "invalid_output",
        "unavailable",
    }
    by_status: dict[str, int] = {}
    required_unavailable: list[str] = []
    optional_unavailable: list[str] = []
    for frontend_name, status in sorted(toolchains.items()):
        state = str(status.get("status", "unknown"))
        by_status[state] = by_status.get(state, 0) + 1
        if state in unavailable_states:
            tool_name = str(status.get("name") or frontend_name)
            label = (
                frontend_name
                if tool_name == frontend_name
                else f"{frontend_name}:{tool_name}"
            )
            if status.get("required"):
                required_unavailable.append(label)
            else:
                optional_unavailable.append(label)
        tools = status.get("tools", [])
        if not isinstance(tools, list):
            continue
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_state = str(tool.get("status", "unknown"))
            if tool_state not in unavailable_states:
                continue
            label = f"{frontend_name}:{tool.get('name', 'tool')}"
            if tool.get("required"):
                required_unavailable.append(label)
            else:
                optional_unavailable.append(label)
    return {
        "available": bool(toolchains),
        "by_status": dict(sorted(by_status.items())),
        "frontends": toolchains,
        "required_unavailable": sorted(required_unavailable),
        "optional_unavailable": sorted(optional_unavailable),
    }


def _validate_repo_relative_path(
    repo_root: Path, raw_path: str | None, label: str
) -> None:
    if not raw_path:
        return
    path = Path(raw_path)
    candidate = path if path.is_absolute() else repo_root / path
    try:
        candidate.resolve().relative_to(repo_root.resolve())
    except (OSError, ValueError) as exc:
        raise ExternalExtractorValidationError(
            f"External extractor {label} is outside the repository: {raw_path}"
        ) from exc


def _requires_semantic_evidence(edge: Edge) -> bool:
    return edge.kind in _SEMANTIC_EDGE_KINDS and edge.confidence in {
        "confirmed",
        "inferred",
        "heuristic",
    }
