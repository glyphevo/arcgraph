"""Shared contracts for ArcGraph build pipeline components."""

from __future__ import annotations

import ast
import json
from numbers import Real
from dataclasses import dataclass, field
from typing import Any

from arcgraph.core.schemas import BuildWarning, Edge, FrontendCapabilities, Node

__all__ = [
    "FrontendContractError",
    "FrontendGraphFragment",
    "validate_frontend_graph_fragment",
]


class FrontendContractError(ValueError):
    """Raised when a language frontend violates the build-time contract."""


@dataclass
class FrontendGraphFragment:
    """Raw graph fragment produced by a language frontend.

    This is the language-neutral handoff between frontend-specific analysis and
    indexer post-processing such as precision references, coverage, and runtime
    trace import.
    """

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    warnings: list[BuildWarning] = field(default_factory=list)
    adapter_metrics: dict[str, Any] = field(default_factory=dict)
    external_packages: set[str] = field(default_factory=set)
    parsed_files: dict[str, ast.Module] = field(default_factory=dict)
    frontend: FrontendCapabilities | None = None
    extractor_metadata: dict[str, Any] = field(default_factory=dict)
    toolchain_status: dict[str, Any] = field(default_factory=dict)
    phase_timings: dict[str, float] = field(default_factory=dict)


def validate_frontend_graph_fragment(
    fragment: object,
    expected_frontend: FrontendCapabilities,
) -> FrontendGraphFragment:
    """Validate and normalize a frontend graph fragment before indexing."""

    frontend_name = expected_frontend.name
    if not isinstance(fragment, FrontendGraphFragment):
        raise FrontendContractError(
            f"{frontend_name}: analyze_to_graph() must return FrontendGraphFragment"
        )

    if fragment.frontend is None:
        fragment.frontend = expected_frontend
    elif (
        fragment.frontend.name != expected_frontend.name
        or fragment.frontend.version != expected_frontend.version
    ):
        raise FrontendContractError(
            f"{frontend_name}: fragment.frontend must match registered "
            f"frontend name/version ({expected_frontend.name} "
            f"{expected_frontend.version})"
        )

    _validate_list(frontend_name, "nodes", fragment.nodes, Node)
    _validate_list(frontend_name, "edges", fragment.edges, Edge)
    _validate_list(frontend_name, "warnings", fragment.warnings, BuildWarning)
    _validate_json_object(frontend_name, "adapter_metrics", fragment.adapter_metrics)
    _validate_json_object(
        frontend_name, "extractor_metadata", fragment.extractor_metadata
    )
    _validate_json_object(frontend_name, "toolchain_status", fragment.toolchain_status)
    _validate_phase_timings(frontend_name, fragment.phase_timings)
    return fragment


def _validate_list(
    frontend_name: str,
    field_name: str,
    value: object,
    expected_type: type,
) -> None:
    if not isinstance(value, list):
        raise FrontendContractError(f"{frontend_name}: {field_name} must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, expected_type):
            raise FrontendContractError(
                f"{frontend_name}: {field_name}[{index}] must be "
                f"{expected_type.__name__}"
            )


def _validate_json_object(
    frontend_name: str,
    field_name: str,
    value: object,
) -> None:
    if not isinstance(value, dict):
        raise FrontendContractError(f"{frontend_name}: {field_name} must be a dict")
    try:
        json.dumps(value)
    except TypeError as exc:
        raise FrontendContractError(
            f"{frontend_name}: {field_name} must be JSON serializable"
        ) from exc


def _validate_phase_timings(
    frontend_name: str,
    value: object,
) -> None:
    if not isinstance(value, dict):
        raise FrontendContractError(f"{frontend_name}: phase_timings must be a dict")
    for key, timing in value.items():
        if not isinstance(timing, Real) or isinstance(timing, bool):
            raise FrontendContractError(
                f"{frontend_name}: phase_timings[{key!r}] must be numeric seconds"
            )
