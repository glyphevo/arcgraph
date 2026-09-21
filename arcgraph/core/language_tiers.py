"""Language capability tiers for frontend and protocol support."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from arcgraph.core.schemas import FrontendCapabilities

LANGUAGE_TIER_DEFINITIONS: dict[str, str] = {
    "L0": "workspace/file discovery",
    "L1": "structural static facts",
    "L2": "protocol evidence facts",
    "L3": "semantic static frontend",
    "L4": "evidence-augmented high availability",
}

SCIP_PROTOCOL_LANGUAGES = ("c", "cpp", "csharp", "go", "java", "rust", "swift")


def language_tier_capabilities(
    *,
    tier: str,
    source: str,
    scope: str,
    languages: Iterable[str] | str,
    status: str = "available",
) -> dict[str, str]:
    """Return standard string capabilities for a frontend tier declaration."""

    if isinstance(languages, str):
        tier_languages = languages
    else:
        tier_languages = ",".join(languages)
    return {
        "language_tier": tier,
        "language_tier_definition": LANGUAGE_TIER_DEFINITIONS[tier],
        "language_tier_source": source,
        "language_tier_scope": scope,
        "language_tier_status": status,
        "language_tier_languages": tier_languages,
    }


def build_language_tiers(
    frontends: Iterable[FrontendCapabilities],
    adapter_metrics: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Build the current index's machine-readable language support summary."""

    tiers: dict[str, dict[str, str]] = {}
    for frontend in frontends:
        capabilities = frontend.capabilities
        tier = capabilities.get("language_tier")
        if not tier:
            continue
        if not _frontend_tier_is_available(frontend, adapter_metrics):
            continue
        languages = _languages_for_frontend(frontend, adapter_metrics)
        for language in languages:
            if not language:
                continue
            existing = tiers.get(language)
            if existing and _tier_rank(existing.get("tier", "")) >= _tier_rank(tier):
                continue
            tiers[language] = {
                "tier": tier,
                "tier_definition": capabilities.get(
                    "language_tier_definition",
                    LANGUAGE_TIER_DEFINITIONS.get(tier, ""),
                ),
                "frontend": frontend.name,
                "frontend_version": frontend.version,
                "source": capabilities.get("language_tier_source", "frontend"),
                "scope": capabilities.get("language_tier_scope", ""),
                "status": capabilities.get("language_tier_status", "available"),
            }

    scip_metrics = adapter_metrics.get("scip-protocol")
    active_scip_languages = set()
    if isinstance(scip_metrics, dict):
        active_scip_languages = {
            str(language)
            for language in scip_metrics.get("languages", [])
            if isinstance(language, str)
        }
    for language in SCIP_PROTOCOL_LANGUAGES:
        if language in tiers:
            continue
        status = (
            "available"
            if language in active_scip_languages
            else "requires_explicit_scip_graph_index"
        )
        tiers[language] = {
            "tier": "L2",
            "tier_definition": LANGUAGE_TIER_DEFINITIONS["L2"],
            "frontend": "scip-protocol",
            "frontend_version": "0.1.0",
            "source": "scip",
            "scope": (
                "SCIP definitions and references only; no call/import "
                "semantics inferred."
            ),
            "status": status,
        }
    return dict(sorted(tiers.items()))


def _tier_rank(tier: str) -> int:
    try:
        return int(tier.lstrip("L"))
    except ValueError:
        return -1


def _frontend_tier_is_available(
    frontend: FrontendCapabilities,
    adapter_metrics: dict[str, Any],
) -> bool:
    mode = frontend.capabilities.get("mode")
    if mode != "external-semantic-extractor":
        return True
    metrics = adapter_metrics.get(frontend.name)
    return isinstance(metrics, dict) and metrics.get("status") == "available"


def _languages_for_frontend(
    frontend: FrontendCapabilities,
    adapter_metrics: dict[str, Any],
) -> list[str]:
    raw_languages = frontend.capabilities.get("language_tier_languages")
    if raw_languages == "artifact-languages":
        metrics = adapter_metrics.get(frontend.name)
        if isinstance(metrics, dict):
            return sorted(
                {
                    str(language)
                    for language in metrics.get("languages", [])
                    if isinstance(language, str)
                }
            )
        return []
    if raw_languages:
        return [
            language.strip()
            for language in raw_languages.split(",")
            if language.strip()
        ]
    return [frontend.language] if frontend.language else []
