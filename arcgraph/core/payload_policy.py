"""Shared policy for bounded, target-scoped read payloads.

This module deliberately sits between the storage/query layer and every
agent-facing transport.  QueryEngine's raw payloads describe the 1.0 index
schema; bounded CLI, native, and MCP responses use the independently versioned
read contract below.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from arcgraph.core.recovery import STALE_RECOVERY_COMMAND
from arcgraph.core.assurance import apply_response_truncation
from arcgraph.core.schemas import READ_SCHEMA_VERSION, SCHEMA_VERSION

FULL_CAPABILITY = "available"
MAX_TARGETS_PER_REQUEST = 100
MAX_TARGET_LENGTH = 4096
MAX_FRESHNESS_SAMPLES_PER_KIND = 5
# A payload that carries no max_results expressed no caller preference. It
# still gets a ceiling: one file target resolves to every symbol the file
# declares, which is unbounded in the size of the file.
DEFAULT_PRESENTATION_LIMIT = MAX_TARGETS_PER_REQUEST

TARGET_SCOPED_CLI_COMMANDS = frozenset(
    {
        "bindings",
        "callees",
        "callers",
        "callsites",
        "context",
        "explain",
        "impact",
        "imports",
        "route",
        "similar",
        "symbol",
        "tests",
        "types",
        "unresolved",
        "worker",
    }
)

TARGET_SCOPED_MCP_TOOLS = frozenset(
    {
        "arcgraph_get_context",
        "arcgraph_explain",
        "arcgraph_get_risk",
        "arcgraph_entrypoint_flow",
        "arcgraph_get_why",
        "arcgraph_find_similar",
        "arcgraph_record_learning",
    }
)

STALE_INDEX_WARNING_KIND = "stale_index"
STALE_INDEX_WARNING_MESSAGE = (
    f"Index is stale; run `{STALE_RECOVERY_COMMAND}` before relying on results."
)

WARNING_SCOPE_UNAVAILABLE = {
    "kind": "warning_scope_unavailable",
    "message": (
        "Could not read definition paths for the resolved targets, so index "
        "warnings are reported unfiltered. The index store may be unavailable."
    ),
}


class TargetRequestLimitError(RuntimeError):
    """Raised when a target-scoped request exceeds bounded read-side limits."""


def normalize_targets(
    values: list[str] | tuple[str, ...] | None,
    *,
    label: str = "targets",
    max_targets: int = MAX_TARGETS_PER_REQUEST,
) -> list[str]:
    """Validate and de-duplicate target strings while preserving input order."""

    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        if not isinstance(value, str):
            raise TargetRequestLimitError(f"{label} must contain only strings")
        if len(value) > MAX_TARGET_LENGTH:
            raise TargetRequestLimitError(
                f"{label} contains a value longer than {MAX_TARGET_LENGTH} characters"
            )
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) > max_targets:
            raise TargetRequestLimitError(
                f"{label} accepts at most {max_targets} unique values"
            )
    return result


def normalize_target_groups(
    targets: list[str] | None,
    changed_files: list[str] | None,
) -> tuple[list[str], list[str], list[str]]:
    """Normalize two named target groups and enforce one combined work bound."""

    normalized_targets = normalize_targets(targets, label="targets")
    normalized_changed_files = normalize_targets(
        changed_files,
        label="changed_files",
    )
    combined = normalize_targets(
        [*normalized_targets, *normalized_changed_files],
        label="targets and changed_files",
    )
    return normalized_targets, normalized_changed_files, combined


def bounded_target_resolution(
    value: dict[str, Any], max_results: int
) -> dict[str, Any]:
    """Bound a resolution's id list for presentation, and say what it dropped.

    One file target resolves to every symbol the file declares — hundreds in
    a large module — and the same resolution is emitted by the report and
    again by assurance, so an unbounded list can dominate a response that
    asked for one result. The bound lives here rather than in
    TargetResolution.to_dict() because the raw engine payload is documented
    as the unbounded debug view.

    Callers must keep using the resolution's own resolved ids for analysis;
    this shape is only what gets serialized.
    """

    if not isinstance(value, dict):
        return {}
    bounded = dict(value)
    ids = [item for item in value.get("resolved_ids", []) if item]
    limit = max(0, max_results)
    bounded["resolved_ids"] = ids[:limit]
    bounded["resolved_id_summary"] = {
        "total": len(ids),
        "returned": min(len(ids), limit),
        "omitted": max(0, len(ids) - limit),
    }
    return bounded


def reportable_capabilities(
    capabilities: dict[str, str],
) -> tuple[dict[str, str], dict[str, Any]]:
    """Return answer-qualifying capability caveats plus an explicit summary."""

    reported = {
        key: value for key, value in capabilities.items() if value != FULL_CAPABILITY
    }
    summary = {
        "reported": len(reported),
        "total": len(capabilities),
        "omitted_value": FULL_CAPABILITY,
        "detail": "Run `arcgraph current` for the full capability table.",
    }
    return reported, summary


def apply_target_payload_contract(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the versioned, bounded read contract without mutating *payload*."""

    result = apply_index_status_contract(payload)
    _bound_freshness_details(result)
    prior_schema = result.get("schema_version", SCHEMA_VERSION)
    result["index_schema_version"] = result.get(
        "index_schema_version",
        SCHEMA_VERSION if prior_schema == READ_SCHEMA_VERSION else prior_schema,
    )
    result["schema_version"] = READ_SCHEMA_VERSION

    capabilities = result.get("capabilities")
    if isinstance(capabilities, dict):
        reported, summary = reportable_capabilities(capabilities)
        existing_summary = result.get("capabilities_summary")
        if isinstance(existing_summary, dict):
            existing_total = existing_summary.get("total")
            if isinstance(existing_total, int):
                summary["total"] = max(existing_total, len(capabilities))
        result["capabilities"] = reported
        result["capabilities_summary"] = summary
    elif "capabilities_summary" not in result:
        _reported, summary = reportable_capabilities({})
        result["capabilities_summary"] = summary

    _bound_payload_resolution(result)
    return result


def _bound_payload_resolution(payload: dict[str, Any]) -> None:
    """Bound every resolved-id list a serialized payload carries.

    Every agent-facing payload passes through this contract, and only the
    raw engine payloads skip it — which is the documented difference. Doing
    the bound here means a new provider surface cannot ship an unbounded
    list by forgetting to call a helper: file targets legitimately resolve
    to every symbol a file declares, so an unbounded list is a large
    response, not an edge case.

    The same ids appear twice — inside ``target_resolution`` and again as
    the top-level ``resolved_targets`` — so both are bounded by the same
    limit, or the payload would still leak the list it just summarized.
    A payload that states no ``max_results`` expressed no caller preference,
    but it still gets a stated ceiling rather than no ceiling at all.
    """

    limit = payload.get("max_results")
    if not isinstance(limit, int) or limit < 0:
        limit = DEFAULT_PRESENTATION_LIMIT
    omitted_counts: dict[str, int] = {}

    def bound(value: Any) -> tuple[Any, int]:
        """Bound one resolution, or leave an already-bounded one alone.

        Re-bounding a bounded list would recompute its summary from the
        truncated ids and report nothing was dropped.
        """

        if not isinstance(value, dict) or "resolved_id_summary" in value:
            return value, 0
        bounded_value = bounded_target_resolution(value, limit)
        return bounded_value, int(
            bounded_value.get("resolved_id_summary", {}).get("omitted", 0) or 0
        )

    resolution, omitted = bound(payload.get("target_resolution"))
    if omitted:
        omitted_counts["target_resolution.resolved_ids"] = omitted
    if resolution is not payload.get("target_resolution"):
        payload["target_resolution"] = resolution

    # The same ids ride along inside assurance and inside every per-target
    # report a payload carries. Bounding only the top-level copy leaves the
    # nested ones to leak the list the payload just summarized, which is how
    # compact_impact shipped five ids beside forty-one.
    assurance = payload.get("assurance")
    if isinstance(assurance, dict):
        resolutions = assurance.get("target_resolutions")
        if isinstance(resolutions, list):
            bounded_resolutions = []
            nested_omitted = 0
            for item in resolutions:
                bounded_item, item_omitted = bound(item)
                nested_omitted += item_omitted
                bounded_resolutions.append(bounded_item)
            if nested_omitted:
                omitted_counts["assurance.target_resolutions.resolved_ids"] = (
                    nested_omitted
                )
            updated_assurance = dict(assurance)
            updated_assurance["target_resolutions"] = bounded_resolutions
            payload["assurance"] = updated_assurance

    for key, value in list(payload.items()):
        if not isinstance(value, list):
            continue
        report_omitted = 0
        sibling_omitted = 0
        bounded_items: list[Any] = []
        changed = False
        for item in value:
            if not isinstance(item, dict) or "target_resolution" not in item:
                bounded_items.append(item)
                continue
            bounded_item, item_omitted = bound(item.get("target_resolution"))
            report_omitted += item_omitted
            # The report's own resolved_targets duplicates these ids, and the
            # bounded resolution's summary describes both. Leaving the sibling
            # alone would put the full list back beside the bounded one — the
            # reason this bound moved into the contract in the first place.
            item_targets = item.get("resolved_targets")
            trim_sibling = (
                isinstance(item_targets, list)
                and len(item_targets) > limit
                and isinstance(bounded_item, dict)
                and "resolved_id_summary" in bounded_item
            )
            if bounded_item is item.get("target_resolution") and not trim_sibling:
                bounded_items.append(item)
                continue
            updated = dict(item)
            updated["target_resolution"] = bounded_item
            if trim_sibling:
                sibling_omitted += len(item_targets) - limit
                updated["resolved_targets"] = item_targets[:limit]
            bounded_items.append(updated)
            changed = True
        if report_omitted:
            omitted_counts[f"{key}.target_resolution.resolved_ids"] = report_omitted
        if sibling_omitted:
            omitted_counts[f"{key}.resolved_targets"] = sibling_omitted
        if changed:
            payload[key] = bounded_items

    resolved_targets = payload.get("resolved_targets")
    summary = payload.get("target_resolution", {})
    has_summary = (
        isinstance(summary, dict) and "resolved_id_summary" in summary
    ) or isinstance(payload.get("truncation"), dict)
    if (
        isinstance(resolved_targets, list)
        and len(resolved_targets) > limit
        # resolved_targets duplicates the resolution's ids, so the
        # resolution's summary describes it too. With neither that summary
        # nor a truncation block there is nowhere to say what was dropped,
        # and dropping without saying so is the defect this guards against.
        and has_summary
    ):
        omitted_counts["resolved_targets"] = len(resolved_targets) - limit
        payload["resolved_targets"] = resolved_targets[:limit]

    if not omitted_counts:
        return
    truncation = payload.get("truncation")
    if not isinstance(truncation, dict):
        return
    # The enclosing contract only shallow-copies the payload, so this nested
    # dict is still the caller's object. Copy it and its counts before
    # writing, or "without mutating payload" stops being true one level down.
    updated = dict(truncation)
    counts = dict(updated.get("truncated_counts", {}))
    for key, value in omitted_counts.items():
        counts.setdefault(key, value)
    updated["truncated_counts"] = counts
    updated["truncated"] = True
    if updated.get("reason") is None:
        updated["reason"] = "max_results"
    payload["truncation"] = updated
    # Assurance was built before this bound existed, so it still says the
    # response was complete. Re-apply the one rule that changed rather than
    # restating it here.
    assurance_block = payload.get("assurance")
    if isinstance(assurance_block, dict):
        payload["assurance"] = apply_response_truncation(assurance_block, updated)


_STALE_LIST_KEYS = ("stale_files", "stale_modules")
_STALE_DETAIL_ROOT_KEYS = frozenset(
    {
        "freshness",
        "assurance",
        "warnings",
        "risk_factors",
        "index_status",
        "reasons",
        "risk",
        "structural_context",
        "structural_why",
        "why",
    }
)
_STALE_DETAIL_PAYLOAD_KEYS = frozenset(
    {
        "index_status",
        "risk",
        "structural_context",
        "why",
    }
)


def _stale_detail_identity(value: Any) -> tuple[str, str]:
    """Return a stable, hashable identity for a JSON-facing stale detail."""

    if isinstance(value, str):
        return ("string", value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        encoded = f"{type(value).__module__}.{type(value).__qualname__}:{value!r}"
        return ("repr", encoded)
    return ("json", encoded)


def _bound_and_measure_stale_lists(
    value: Any,
    limit: int,
    totals: dict[str, int],
    shown: dict[str, int],
    unique_values: dict[str, set[tuple[str, str]]],
    emitted_values: dict[str, set[tuple[str, str]]],
    *,
    root: bool = False,
) -> Any:
    """Globally sample and copy-on-write truncate every ``stale_*`` list.

    Containers are copied only along paths that actually change, so a payload
    with nothing to bound is returned unchanged and the caller's input is never
    mutated.
    """

    if isinstance(value, dict):
        replacements: dict[str, Any] = {}
        entries = list(value.items())
        if root:
            # The canonical freshness object owns the response sample. Process
            # it first regardless of input insertion order so secondary views
            # cannot consume the global allowance and empty `freshness.stale_*`.
            entries.sort(key=lambda entry: entry[0] != "freshness")
        for key, item in entries:
            if (
                root
                and key not in _STALE_DETAIL_ROOT_KEYS
                and key not in _STALE_LIST_KEYS
            ):
                continue
            if key in _STALE_LIST_KEYS and isinstance(item, list):
                known = unique_values.setdefault(key, set())
                emitted = emitted_values.setdefault(key, set())
                sampled: list[Any] = []
                for candidate in item:
                    identity = _stale_detail_identity(candidate)
                    known.add(identity)
                    if identity in emitted:
                        # The sample lives in the canonical `freshness` view.
                        # Repeating it in every secondary view would multiply
                        # the response past the bound the stale warning
                        # declares, which is what this global set exists to
                        # prevent; secondary views carry the count instead.
                        continue
                    if len(emitted) >= limit:
                        continue
                    emitted.add(identity)
                    sampled.append(candidate)
                totals[key] = len(known)
                shown[key] = len(emitted)
                if sampled != item:
                    replacements[key] = sampled
                continue
            bounded_item = _bound_and_measure_stale_lists(
                item,
                limit,
                totals,
                shown,
                unique_values,
                emitted_values,
                root=key in _STALE_DETAIL_PAYLOAD_KEYS,
            )
            if bounded_item is not item:
                replacements[key] = bounded_item
        return {**value, **replacements} if replacements else value
    if isinstance(value, list):
        replacements: dict[int, Any] = {}
        for index, item in enumerate(value):
            bounded_item = _bound_and_measure_stale_lists(
                item,
                limit,
                totals,
                shown,
                unique_values,
                emitted_values,
            )
            if bounded_item is not item:
                replacements[index] = bounded_item
        if replacements:
            items = list(value)
            for index, item in replacements.items():
                items[index] = item
            return items
        return value
    return value


def _bound_freshness_details(payload: dict[str, Any]) -> None:
    """Keep target-scoped freshness evidence useful without repeating full lists."""

    freshness = payload.get("freshness")
    if not isinstance(freshness, dict) or freshness.get("status") != "stale":
        return

    warnings = payload.get("warnings")
    # Record the position alongside the object: the bounding pass below only
    # replaces list items positionally, so the index stays valid afterwards
    # and the warning does not need to be located a second time.
    stale_warning_index, stale_warning = next(
        (
            (index, warning)
            for index, warning in enumerate(warnings or [])
            if isinstance(warning, dict)
            and warning.get("kind") == STALE_INDEX_WARNING_KIND
        ),
        (None, None),
    )
    already_bounded = isinstance(stale_warning, dict) and isinstance(
        stale_warning.get("freshness_details_omitted"), int
    )

    # Some providers also re-emit the lists under `risk_factors`,
    # `index_status`, and per-reason evidence. A single global sample is shared
    # by every copy so repeated provider views cannot multiply the declared
    # response bound.
    totals: dict[str, int] = {}
    shown: dict[str, int] = {}
    bounded_payload = _bound_and_measure_stale_lists(
        payload,
        MAX_FRESHNESS_SAMPLES_PER_KIND,
        totals,
        shown,
        {},
        {},
        root=True,
    )
    file_values = totals.get("stale_files", 0)
    module_values = totals.get("stale_modules", 0)
    if bounded_payload is payload and not already_bounded:
        return

    file_total = file_values
    module_total = module_values
    if isinstance(stale_warning, dict):
        recorded_file_total = stale_warning.get("stale_file_count")
        recorded_module_total = stale_warning.get("stale_module_count")
        if isinstance(recorded_file_total, int):
            file_total = max(file_total, recorded_file_total)
        if isinstance(recorded_module_total, int):
            module_total = max(module_total, recorded_module_total)

    if bounded_payload is not payload:
        payload.clear()
        payload.update(bounded_payload)

    bounded_warnings = payload.get("warnings")
    if not isinstance(stale_warning, dict) or not isinstance(bounded_warnings, list):
        return
    if stale_warning_index is None or stale_warning_index >= len(bounded_warnings):
        return
    current_stale_warning = bounded_warnings[stale_warning_index]
    if (
        not isinstance(current_stale_warning, dict)
        or current_stale_warning.get("kind") != STALE_INDEX_WARNING_KIND
    ):
        return
    bounded_warning = dict(current_stale_warning)
    bounded_warning["stale_file_count"] = file_total
    bounded_warning["stale_module_count"] = module_total
    bounded_warning["freshness_sample_limit"] = MAX_FRESHNESS_SAMPLES_PER_KIND
    bounded_warning["freshness_details_omitted"] = max(
        0,
        file_total - shown.get("stale_files", 0),
    ) + max(
        0,
        module_total - shown.get("stale_modules", 0),
    )
    bounded_warning["detail"] = (
        "Freshness file/module lists are bounded on target-scoped responses; "
        "run `arcgraph current` for the complete lists."
    )
    updated_warnings = list(bounded_warnings)
    updated_warnings[stale_warning_index] = bounded_warning
    payload["warnings"] = updated_warnings


def apply_index_status_contract(payload: dict[str, Any]) -> dict[str, Any]:
    """Add index-wide status warnings without changing schema or capabilities."""

    result = dict(payload)
    _add_stale_index_warning(result)
    return result


def _add_stale_index_warning(payload: dict[str, Any]) -> None:
    """Make a confirmed stale index conspicuous without copying its file paths."""

    freshness = payload.get("freshness")
    if not isinstance(freshness, dict) or freshness.get("status") != "stale":
        return

    existing = payload.get("warnings")
    if isinstance(existing, list):
        warnings = list(existing)
    elif existing is None:
        warnings = []
    else:
        warnings = [existing]
    if any(
        isinstance(warning, dict) and warning.get("kind") == STALE_INDEX_WARNING_KIND
        for warning in warnings
    ):
        return

    stale_files = freshness.get("stale_files")
    stale_file_count = len(stale_files) if isinstance(stale_files, list) else 0
    warnings.append(
        {
            "kind": STALE_INDEX_WARNING_KIND,
            "message": STALE_INDEX_WARNING_MESSAGE,
            "stale_file_count": stale_file_count,
        }
    )
    payload["warnings"] = warnings


def scope_index_warnings(
    index_warnings: Any,
    relevant_paths: set[str],
    *,
    fold: bool = True,
) -> list[Any]:
    """Keep relevant index warnings and summarize unrelated path-scoped records."""

    if not fold:
        return list(index_warnings or [])
    kept: list[Any] = []
    omitted: dict[str, int] = {}
    for warning in index_warnings or []:
        path = warning.get("path") if isinstance(warning, dict) else None
        if not isinstance(path, str) or path in relevant_paths:
            kept.append(warning)
            continue
        kind = warning.get("kind") or "unknown"
        omitted[kind] = omitted.get(kind, 0) + 1
    if omitted:
        counts = dict(sorted(omitted.items()))
        total = sum(counts.values())
        breakdown = ", ".join(f"{kind}={count}" for kind, count in counts.items())
        kept.append(
            {
                "kind": "index_warnings_omitted",
                "message": (
                    f"{total} index-level warning(s) unrelated to the requested "
                    f"targets were omitted ({breakdown}). Run `arcgraph current` "
                    "for the full list."
                ),
                "counts_by_kind": counts,
            }
        )
    return kept


def scoped_warning_inputs(
    index_warnings: Any,
    relevant_paths: set[str],
    target_paths: set[str] | None,
    resolved_targets: Any,
) -> list[Any]:
    """Scope warnings, degrading to unfiltered output when scope is unknown."""

    if target_paths is None:
        return [
            WARNING_SCOPE_UNAVAILABLE,
            *scope_index_warnings(index_warnings, set(), fold=False),
        ]
    return scope_index_warnings(
        index_warnings,
        relevant_paths | target_paths,
        fold=bool(resolved_targets),
    )


def resolved_definition_paths(
    query_engine: Any,
    resolved_targets: Any,
) -> set[str] | None:
    """Return definition paths, or ``None`` for an expected store-read failure."""

    ids = {
        node_id
        for node_id in (resolved_targets or [])
        if isinstance(node_id, str) and node_id
    }
    if not ids:
        return set()
    try:
        nodes = query_engine.nodes_by_ids(sorted(ids))
    except (sqlite3.Error, OSError, json.JSONDecodeError):
        return None
    seen = {node.get("id") for node in nodes if isinstance(node, dict)}
    if not ids <= seen:
        return None
    return {
        node["path"]
        for node in nodes
        if isinstance(node, dict) and isinstance(node.get("path"), str)
    }


def unique_values(values: list[Any]) -> list[Any]:
    """De-duplicate JSON-like values without flattening structured records."""

    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = (
            value
            if isinstance(value, str)
            else json.dumps(value, sort_keys=True, ensure_ascii=False)
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
