"""Confidence profile definitions for ArcGraph query behavior.

Each profile controls which confidence levels are included in query results,
allowing different trade-offs between precision and recall for different
use cases (code review, debugging, strict static analysis).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConfidenceProfileConfig:
    """Configuration for a named confidence profile."""

    allowed_confidences: frozenset[str]
    include_unresolved: bool
    default_max_depth: int
    description: str


CONFIDENCE_PROFILES: dict[str, ConfidenceProfileConfig] = {
    "review_default": ConfidenceProfileConfig(
        allowed_confidences=frozenset(
            {"confirmed", "inferred", "heuristic", "runtime-only"}
        ),
        include_unresolved=True,  # show unresolved in results but don't traverse
        default_max_depth=3,
        description="Default profile for code review",
    ),
    "debugging_default": ConfidenceProfileConfig(
        allowed_confidences=frozenset(
            {
                "confirmed",
                "inferred",
                "heuristic",
                "runtime-only",
                "unresolved",
            }
        ),
        include_unresolved=True,
        default_max_depth=5,  # deeper exploration for debugging
        description="All evidence levels for debugging",
    ),
    "strict_static": ConfidenceProfileConfig(
        allowed_confidences=frozenset({"confirmed"}),
        include_unresolved=False,
        default_max_depth=2,  # shallower but more precise
        description="Only confirmed edges for strict analysis",
    ),
    "evidence_only": ConfidenceProfileConfig(
        allowed_confidences=frozenset({"confirmed", "inferred", "runtime-only"}),
        include_unresolved=False,
        default_max_depth=3,
        description="Modeled or runtime evidence, excluding name heuristics; not runtime completeness",
    ),
}

# Hard cap on depth regardless of profile or user request.
DEPTH_HARD_CAP: int = 10

# Default profile name when none is specified.
DEFAULT_PROFILE_NAME: str = "review_default"


def callsite_facts(properties: dict[str, Any]) -> list[dict[str, Any]]:
    values = properties.get("callsites")
    if not isinstance(values, list):
        values = [properties.get("callsite")]
    return [value for value in values if isinstance(value, dict)]


def heuristic_callsite(
    fact: dict[str, Any], confidence: str, resolution: dict[str, Any] | None = None
) -> bool:
    """Use per-call confidence; recognize the three old name-guess strategies.

    Older indexes without that metadata otherwise retain edge confidence.
    Rebuilding supplies exact provenance for mixed edges from other strategies.
    """
    guess_strategies = {
        "common_boundary_method",
        "receiver_name_boundary_method",
        "unique_method_fallback",
    }
    if (
        resolution
        and confidence in {"confirmed", "inferred", "runtime-only"}
        and resolution.get("status") == "resolved"
        and resolution.get("callsite_id") == fact.get("callsite_id")
        and resolution.get("callsite_id")
        and resolution.get("strategy") not in guess_strategies
    ):
        # A stronger producer resolved this exact callsite in the merged edge.
        return False
    if "confidence" in fact:
        return fact["confidence"] == "heuristic"
    return (
        confidence == "heuristic" or fact.get("resolution_strategy") in guess_strategies
    )


def get_profile(name: str | None = None) -> ConfidenceProfileConfig:
    """Return the profile config for *name*, falling back to the default.

    Raises ``KeyError`` if *name* is not ``None`` and not a known profile.
    """
    if name is None:
        name = DEFAULT_PROFILE_NAME
    return CONFIDENCE_PROFILES[name]


def effective_max_depth(
    profile: ConfidenceProfileConfig,
    user_max_depth: int | None = None,
) -> tuple[int, list[str]]:
    """Return ``(effective_depth, warnings)`` after applying caps.

    If *user_max_depth* is provided it takes precedence over the profile
    default, but is still capped at ``DEPTH_HARD_CAP``.
    """
    warnings: list[str] = []
    depth = user_max_depth if user_max_depth is not None else profile.default_max_depth
    if depth > DEPTH_HARD_CAP:
        warnings.append(
            f"Requested max_depth={depth} exceeds hard cap={DEPTH_HARD_CAP}; "
            f"capped to {DEPTH_HARD_CAP}."
        )
        depth = DEPTH_HARD_CAP
    return depth, warnings


def confidence_sql_filter(profile: ConfidenceProfileConfig) -> str:
    """Return a SQL ``IN (...)`` clause fragment for the allowed confidences.

    Example: ``"'confirmed', 'inferred', 'heuristic', 'runtime-only'"``
    """
    return ", ".join(f"'{c}'" for c in sorted(profile.allowed_confidences))


def profile_summary(name: str, config: ConfidenceProfileConfig) -> dict[str, Any]:
    """Return a JSON-serializable summary of a profile for API responses."""
    return {
        "name": name,
        "allowed_confidences": sorted(config.allowed_confidences),
        "include_unresolved": config.include_unresolved,
        "default_max_depth": config.default_max_depth,
        "description": config.description,
    }
