"""CI-oriented ArcGraph checks and summaries."""

import fnmatch
import json
import time
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from arcgraph.core.evidence_manifest import evidence_health_check_status
from arcgraph.core.metadata import current_commit
from arcgraph.pipeline.external_protocol import summarize_toolchain_status
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.semantic_metrics import (
    collect_semantic_metrics_from_resolved_callsites,
    resolved_callsite_ids,
)
from arcgraph.core.schemas import (
    SCHEMA_STABLE_TARGET_VERSION,
    SCHEMA_VERSION,
    V2_RECEIVER_RESOLUTION_DEFAULT_SCHEMA_VERSION,
    schema_direct_compatibility_step,
    schema_compatibility_steps,
    schema_version_reached,
)
from arcgraph.interfaces.reports import classify_unresolved_records
from arcgraph.providers.risk_provider import RiskProvider

DEFAULT_PERFORMANCE_BUDGET_MS = {
    "current": 500.0,
    "stats": 1000.0,
    "architecture": 3000.0,
    "semantic_stats": 5000.0,
    "risk": 3000.0,
}
CURRENT_FRESHNESS_BUDGET_BASE_MS = 500.0
CURRENT_FRESHNESS_BUDGET_PER_FILE_MS = 18.0


def run_ci_checks(
    query_engine: QueryEngine,
    *,
    targets: list[str] | None = None,
    changed_files: list[str] | None = None,
    max_results: int = 30,
    similarity_threshold: float = 0.95,
    semantic_baseline: dict[str, Any] | None = None,
    semantic_regression_tolerance: float = 0.02,
    performance_budgets_ms: dict[str, float] | None = None,
    require_precision: bool = False,
    require_coverage: bool = False,
    require_runtime_trace: bool = False,
    require_python_full: bool = False,
) -> dict[str, Any]:
    targets = targets or []
    # A change scope is whatever names the change: explicit targets, or the
    # changed files the risk report is derived from.
    change_scope = list(targets) + list(changed_files or [])
    changed_files = changed_files or []
    max_results = max(1, min(max_results, 100))
    required_inputs = evidence_input_requirements(
        require_precision=require_precision,
        require_coverage=require_coverage,
        require_runtime_trace=require_runtime_trace,
        require_python_full=require_python_full,
    )
    require_precision = required_inputs["precision"]
    require_coverage = required_inputs["coverage"]
    require_runtime_trace = required_inputs["runtime_trace"]
    operation_timings: dict[str, float] = {}
    current = _timed_operation("current", query_engine.current, operation_timings)
    stats = _timed_operation("stats", query_engine.stats, operation_timings)
    architecture = _timed_operation(
        "architecture", query_engine.architecture, operation_timings
    )
    semantic_stats = _timed_operation(
        "semantic_stats", query_engine.semantic_stats, operation_timings
    )
    semantic_summary = _semantic_summary(semantic_stats)
    risk = _timed_operation(
        "risk",
        lambda: RiskProvider(query_engine).get_risk(
            targets=targets,
            changed_files=changed_files,
            max_results=max_results,
        ),
        operation_timings,
    )

    schema_readiness = _schema_readiness_summary()
    checks = [
        _check(
            "schema_compatibility",
            "pass",
            (
                f"Runtime schema {SCHEMA_VERSION} matches the current index; "
                f"{SCHEMA_STABLE_TARGET_VERSION} readiness route is recorded."
            ),
            details=schema_readiness,
        ),
        _check(
            "build_smoke",
            (
                "pass"
                if stats.get("counts", {}).get("files", 0) > 0
                and stats.get("counts", {}).get("nodes", 0) > 0
                else "fail"
            ),
            "Current index has files and nodes.",
            details=stats.get("counts", {}),
        ),
    ]

    language_capabilities = _language_capability_summary(current)
    checks.append(
        _check(
            "language_capability_tiers",
            "pass" if language_capabilities["available"] else "warn",
            (
                "Language capability tiers are declared."
                if language_capabilities["available"]
                else "Language capability tier metadata is missing."
            ),
            details=language_capabilities,
        )
    )

    external_toolchains = summarize_toolchain_status(
        current.get("toolchain_status", {})
        if isinstance(current.get("toolchain_status"), dict)
        else {}
    )
    required_toolchain_failures = external_toolchains["required_unavailable"]
    checks.append(
        _check(
            "external_extractor_toolchains",
            "warn" if required_toolchain_failures else "pass",
            (
                "External extractor toolchains are available or optional."
                if not required_toolchain_failures
                else "Required external extractor toolchains are unavailable."
            ),
            details=external_toolchains,
        )
    )

    freshness = current.get("freshness", {})
    checks.append(
        _check(
            "stale_index",
            "warn" if freshness.get("stale") else "pass",
            (
                "Current index freshness is acceptable."
                if not freshness.get("stale")
                else "Current index is stale for changed files."
            ),
            details=freshness,
        )
    )

    diagnostic_lifecycle = _diagnostic_lifecycle_summary(
        query_engine, max_results=max_results
    )
    checks.append(
        _check(
            "diagnostics_lifecycle",
            "pass",
            "Semantic diagnostics are classified as new or existing.",
            details=diagnostic_lifecycle,
        )
    )

    stale_targets = _stale_target_summary(query_engine, max_results=max_results)
    checks.append(
        _check(
            "stale_target_edges",
            "warn" if stale_targets["diagnostics_count"] else "pass",
            (
                "No stale target edges detected."
                if not stale_targets["diagnostics_count"]
                else (
                    f"Detected {stale_targets['stale_edges_count']} stale edge(s) "
                    "whose target disappeared or changed identity."
                )
            ),
            details=stale_targets,
        )
    )

    adapter_diagnostics = _adapter_diagnostics_summary(
        query_engine, max_results=max_results
    )
    checks.append(
        _check(
            "adapter_diagnostics",
            (
                "fail"
                if adapter_diagnostics["errors_count"]
                else "warn" if adapter_diagnostics["warnings_count"] else "pass"
            ),
            (
                "No adapter diagnostics detected."
                if not adapter_diagnostics["warnings_count"]
                else (
                    f"Detected {adapter_diagnostics['warnings_count']} adapter "
                    "diagnostic warning(s)."
                )
            ),
            details=adapter_diagnostics,
        )
    )

    adapter_metrics = _adapter_metrics_summary(current, max_results=max_results)
    checks.append(
        _check(
            "adapter_metrics_baseline",
            (
                "fail"
                if adapter_metrics["error_count"]
                else "warn" if adapter_metrics["regression_count"] else "pass"
            ),
            (
                "Adapter metrics baseline is available."
                if adapter_metrics["available"]
                and not adapter_metrics["regression_count"]
                else (
                    "Adapter metrics are unavailable for this index."
                    if not adapter_metrics["available"]
                    else "Adapter metrics include warnings or unavailable adapters."
                )
            ),
            details=adapter_metrics,
        )
    )

    evidence_requirements = evidence_requirement_summary(
        current,
        require_precision=require_precision,
        require_coverage=require_coverage,
        require_runtime_trace=require_runtime_trace,
        require_python_full=require_python_full,
    )
    precision_summary = evidence_requirements["requirements"]["precision"]["summary"]
    precision_status = evidence_requirements["requirements"]["precision"][
        "check_status"
    ]
    checks.append(
        _check(
            "precision_inputs",
            precision_status,
            _precision_message(precision_summary),
            details=precision_summary,
        )
    )

    coverage_summary = evidence_requirements["requirements"]["coverage"]["summary"]
    checks.append(
        _check(
            "coverage_inputs",
            _optional_input_check_status(
                coverage_summary["status"],
                available_status="available",
                required=require_coverage,
            ),
            _coverage_message(coverage_summary["status"]),
            details=coverage_summary,
        )
    )

    runtime_summary = evidence_requirements["requirements"]["runtime_trace"]["summary"]
    checks.append(
        _check(
            "runtime_trace_inputs",
            _optional_input_check_status(
                runtime_summary["status"],
                available_status="available",
                required=require_runtime_trace,
            ),
            _runtime_trace_message(runtime_summary["status"]),
            details=runtime_summary,
        )
    )

    evidence_manifest = current.get("evidence_manifest")
    evidence_snapshot_status = evidence_health_check_status(evidence_manifest)
    checks.append(
        _check(
            "evidence_manifest",
            "pass" if isinstance(evidence_manifest, dict) else "warn",
            (
                "Evidence manifest snapshot is present."
                if isinstance(evidence_manifest, dict)
                else "Evidence manifest snapshot is missing from the current index."
            ),
            details={
                "status": evidence_snapshot_status,
                "summary": (
                    evidence_manifest.get("summary", {})
                    if isinstance(evidence_manifest, dict)
                    else {}
                ),
            },
        )
    )

    evidence_commit_summary = _evidence_commit_consistency_summary(current)
    evidence_commit_status = "pass"
    if require_python_full and not evidence_commit_summary["consistent"]:
        evidence_commit_status = "fail"
    checks.append(
        _check(
            "evidence_commit_consistency",
            evidence_commit_status,
            (
                "Index and runtime evidence match the current HEAD."
                if evidence_commit_summary["consistent"]
                else "Index or runtime evidence was generated for a different HEAD."
            ),
            details=evidence_commit_summary,
        )
    )

    performance_summary = _performance_budget_summary(
        operation_timings,
        budgets_ms=performance_budgets_ms,
        file_count=_int_metric(stats.get("counts", {}).get("files")),
    )
    checks.append(
        _check(
            "performance_budget",
            "warn" if performance_summary["violation_count"] else "pass",
            (
                "Observed CI query operations are within the configured budgets."
                if not performance_summary["violation_count"]
                else "Observed CI query operations exceeded configured budgets."
            ),
            details=performance_summary,
        )
    )

    release_gate = _release_gate_summary(current)
    release_gate_status = "warn" if release_gate["exception_count"] else "pass"
    if require_python_full and release_gate["exception_count"]:
        release_gate_status = "fail"
    checks.append(
        _check(
            "python_v2_release_gate",
            release_gate_status,
            (
                "Python V2 selected queries and fallback contract are release-ready."
                if release_gate_status == "pass"
                else "Python V2 release gate has contract exceptions."
            ),
            details=release_gate,
        )
    )

    import_cycles = architecture.get("import_cycles", [])
    checks.append(
        _check(
            "import_cycles",
            "warn" if import_cycles else "pass",
            (
                "No import cycles detected."
                if not import_cycles
                else f"Detected {len(import_cycles)} import cycle(s)."
            ),
            details={"cycles": import_cycles[:max_results]},
        )
    )

    layer_report = _layer_violation_report(
        query_engine, current, max_results=max_results
    )
    layer_violations = layer_report["violations"]
    checks.append(
        _check(
            "layer_violations",
            "warn" if layer_violations else "pass",
            (
                f"Detected {len(layer_violations)} "
                f"{layer_report['model_source']} layer violation(s)."
                if layer_violations
                else (
                    f"No {layer_report['model_source']} layer import violations "
                    "detected."
                    if layer_report["applicable"]
                    else (
                        "No layer model applies to this repository: no import "
                        "edge could be placed in a layer, so this check "
                        "inspected nothing. Configure "
                        "[[tool.arcgraph.ci.layers]] to gate layering here."
                    )
                )
            ),
            details=layer_report,
        )
    )

    risk_factors = risk.get("risk_factors", [])
    high_risk = {
        item.get("kind")
        for item in risk_factors
        if item.get("kind")
        in {"entrypoint_impact", "resource_write", "test_gap", "missing_test_candidate"}
    }
    checks.append(
        _check(
            "high_risk_changes_without_tests",
            (
                "warn"
                if {"entrypoint_impact", "resource_write"} & high_risk
                and {"test_gap", "missing_test_candidate"} & high_risk
                else "pass"
            ),
            (
                (
                    "No change scope was given, so no changed target was "
                    "examined for test evidence. Pass --target or "
                    "--changed-file to check a change here."
                    if not change_scope
                    else "High-risk changed targets have test guidance."
                )
                if not (
                    {"entrypoint_impact", "resource_write"} & high_risk
                    and {"test_gap", "missing_test_candidate"} & high_risk
                )
                else "High-risk changed targets need stronger test evidence."
            ),
            details={
                "risk_factors": risk_factors[:max_results],
                # Without a change scope this check inspects nothing. Saying so
                # keeps a pass from reading as evidence that a change was
                # examined and found covered.
                "scoped": bool(change_scope),
                "target_count": len(change_scope),
            },
        )
    )

    high_risk_unresolved = {
        **_high_risk_unresolved_summary(risk, max_results=max_results),
        # Without a change scope this check inspects nothing. Saying so keeps a
        # pass from reading as evidence that a change was examined and found
        # clean. The PR summary carries the same fields as the check details.
        "scoped": bool(change_scope),
        "target_count": len(change_scope),
    }
    checks.append(
        _check(
            "high_risk_unresolved_callsites",
            "warn" if high_risk_unresolved["unresolved_callsite_total"] else "pass",
            (
                (
                    "No change scope was given, so no target was examined for "
                    "unresolved callsites. Pass --target or --changed-file to "
                    "check a change here."
                    if not change_scope
                    else "No high-risk unresolved callsites detected."
                )
                if not high_risk_unresolved["unresolved_callsite_total"]
                else (
                    "High-risk changed targets include unresolved callsites that "
                    "need review."
                )
            ),
            details=high_risk_unresolved,
        )
    )

    similar = _similarity_warnings(
        query_engine,
        targets=_unique([*targets, *changed_files]),
        threshold=similarity_threshold,
        max_results=max_results,
    )
    checks.append(
        _check(
            "similarity_duplicates",
            "warn" if similar else "pass",
            (
                "No high-similarity parallel implementation hints for the checked targets."
                if not similar
                else f"Found {len(similar)} high-similarity implementation hint(s)."
            ),
            details={"similar": similar},
        )
    )

    checks.append(
        _check(
            "semantic_unresolved_baseline",
            "pass",
            "Semantic callsite, binding, and type baseline recorded.",
            details=semantic_summary,
        )
    )
    unresolved_classification = _unresolved_classification_summary(
        query_engine, max_results=max_results
    )
    unresolved_blocking_count = int(
        unresolved_classification.get("release_blocking_count", 0)
    )
    unresolved_status = "pass"
    if unresolved_blocking_count:
        unresolved_status = "fail" if require_python_full else "warn"
    checks.append(
        _check(
            "semantic_unresolved_classification",
            unresolved_status,
            (
                "No release-blocking unresolved candidates remain."
                if unresolved_status == "pass"
                else "Release-blocking unresolved candidates remain visible and must be resolved before stable release."
            ),
            details=unresolved_classification,
        )
    )

    semantic_regression = _semantic_resolution_trend(
        semantic_summary,
        semantic_baseline=(
            semantic_baseline
            if semantic_baseline is not None
            else _configured_semantic_baseline(current)
        ),
        tolerance=semantic_regression_tolerance,
    )
    checks.append(
        _check(
            "semantic_resolution_trend",
            "warn" if semantic_regression["regressed"] else "pass",
            (
                "Semantic resolution baseline was not provided; trend check recorded current metrics."
                if not semantic_regression["baseline_available"]
                else (
                    "Semantic resolution rate is within the configured baseline tolerance."
                    if not semantic_regression["regressed"]
                    else "Semantic resolution rate regressed below the configured baseline tolerance."
                )
            ),
            details=semantic_regression,
        )
    )
    semantic_quality_summary, semantic_quality_scope = (
        _semantic_quality_summary_for_targets(
            query_engine,
            semantic_summary,
            current,
        )
    )
    semantic_quality = _semantic_quality_target_summary(
        semantic_quality_summary,
        current,
        scope=semantic_quality_scope,
    )
    semantic_quality_status = "warn" if semantic_quality["exceptions"] else "pass"
    if require_python_full and semantic_quality["exceptions"]:
        semantic_quality_status = "fail"
    checks.append(
        _check(
            "semantic_quality_targets",
            semantic_quality_status,
            (
                "Semantic quality targets are met."
                if semantic_quality_status == "pass"
                else (
                    "One or more semantic quality metrics are below target; "
                    "see exceptions for each metric, its current value, and "
                    "its target."
                )
            ),
            details=semantic_quality,
        )
    )

    summary = {
        "pass": sum(1 for check in checks if check["status"] == "pass"),
        "warn": sum(1 for check in checks if check["status"] == "warn"),
        "fail": sum(1 for check in checks if check["status"] == "fail"),
    }
    status = "fail" if summary["fail"] else "warn" if summary["warn"] else "pass"
    return {
        "schema_version": SCHEMA_VERSION,
        "index_version": current.get("index_version"),
        "status": status,
        "freshness": freshness,
        "capabilities": current.get("capabilities", {}),
        "targets": targets,
        "changed_files": changed_files,
        "required_inputs": {
            "python_full": require_python_full,
            "precision": require_precision,
            "coverage": require_coverage,
            "runtime_trace": require_runtime_trace,
        },
        "summary": summary,
        "checks": checks,
        "risk": risk,
        "semantic_summary": semantic_summary,
        "diagnostic_lifecycle": diagnostic_lifecycle,
        "pr_summary": _pr_summary(
            risk,
            similar,
            semantic_summary,
            high_risk_unresolved,
        ),
        "warnings": [
            check["message"] for check in checks if check["status"] in {"warn", "fail"}
        ],
    }


def _check(
    name: str,
    status: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "message": message,
        "details": details or {},
    }


def _language_capability_summary(current: dict[str, Any]) -> dict[str, Any]:
    tiers = current.get("language_tiers", {})
    if not isinstance(tiers, dict):
        tiers = {}
    normalized = {
        str(language): entry
        for language, entry in tiers.items()
        if isinstance(entry, dict)
    }
    tier_counts = Counter(
        str(entry.get("tier"))
        for entry in normalized.values()
        if isinstance(entry.get("tier"), str)
    )
    status_counts = Counter(
        str(entry.get("status", "unknown")) for entry in normalized.values()
    )
    return {
        "available": bool(normalized),
        "languages": sorted(normalized),
        "by_tier": dict(sorted(tier_counts.items())),
        "by_status": dict(sorted(status_counts.items())),
        "available_languages": sorted(
            language
            for language, entry in normalized.items()
            if entry.get("status") == "available"
        ),
        "requires_artifact": sorted(
            language
            for language, entry in normalized.items()
            if str(entry.get("status", "")).startswith("requires_")
        ),
        "tiers": normalized,
    }


def _timed_operation(name: str, operation: Any, timings: dict[str, float]) -> Any:
    started = time.perf_counter()
    try:
        return operation()
    finally:
        timings[name] = round((time.perf_counter() - started) * 1000, 3)


def evidence_input_requirements(
    *,
    require_precision: bool = False,
    require_coverage: bool = False,
    require_runtime_trace: bool = False,
    require_python_full: bool = False,
) -> dict[str, bool]:
    return {
        "python_full": require_python_full,
        "precision": require_precision or require_python_full,
        "coverage": require_coverage or require_python_full,
        "runtime_trace": require_runtime_trace or require_python_full,
    }


def evidence_requirement_summary(
    current: dict[str, Any],
    *,
    require_precision: bool = False,
    require_coverage: bool = False,
    require_runtime_trace: bool = False,
    require_python_full: bool = False,
) -> dict[str, Any]:
    required = evidence_input_requirements(
        require_precision=require_precision,
        require_coverage=require_coverage,
        require_runtime_trace=require_runtime_trace,
        require_python_full=require_python_full,
    )
    precision_summary = _precision_summary(current)
    precision_status = _optional_input_check_status(
        precision_summary["status"],
        available_status="precision_available",
        required=required["precision"],
    )
    if require_python_full and not precision_summary["type_precision_complete"]:
        precision_status = "fail"

    coverage_summary = _coverage_summary(current)
    coverage_status = _optional_input_check_status(
        coverage_summary["status"],
        available_status="available",
        required=required["coverage"],
    )

    runtime_summary = _runtime_trace_summary(current)
    runtime_status = _optional_input_check_status(
        runtime_summary["status"],
        available_status="available",
        required=required["runtime_trace"],
    )

    return {
        "required_inputs": required,
        "requirements": {
            "precision": {
                "required": required["precision"],
                "check_status": precision_status,
                "blocks_python_full": require_python_full
                and precision_status == "fail",
                "summary": precision_summary,
            },
            "coverage": {
                "required": required["coverage"],
                "check_status": coverage_status,
                "blocks_python_full": require_python_full and coverage_status == "fail",
                "summary": coverage_summary,
            },
            "runtime_trace": {
                "required": required["runtime_trace"],
                "check_status": runtime_status,
                "blocks_python_full": require_python_full and runtime_status == "fail",
                "summary": runtime_summary,
            },
        },
    }


def _optional_input_check_status(
    status: str,
    *,
    available_status: str,
    required: bool,
) -> str:
    if status == available_status:
        return "pass"
    if required:
        return "fail"
    return "warn" if status in {"partial", "stale"} else "pass"


def _schema_readiness_summary() -> dict[str, Any]:
    steps = schema_compatibility_steps()
    legacy_steps = schema_compatibility_steps("0.6.0", SCHEMA_STABLE_TARGET_VERSION)
    legacy_direct_step = schema_direct_compatibility_step(
        "0.6.0", SCHEMA_STABLE_TARGET_VERSION
    )
    return {
        "current_schema_version": SCHEMA_VERSION,
        "stable_target_schema_version": SCHEMA_STABLE_TARGET_VERSION,
        "steps": [step.model_dump(mode="json") for step in steps],
        "legacy_0_6_to_stable_steps": [
            step.model_dump(mode="json") for step in legacy_steps
        ],
        "legacy_0_6_to_stable_direct_strategy": (
            legacy_direct_step.model_dump(mode="json")
            if legacy_direct_step is not None
            else None
        ),
        "required_rebuilds": sum(1 for step in steps if step.required_rebuild),
        "ready_for_stable_contract": SCHEMA_VERSION == SCHEMA_STABLE_TARGET_VERSION,
        "receiver_resolution_default_schema_version": (
            V2_RECEIVER_RESOLUTION_DEFAULT_SCHEMA_VERSION
        ),
    }


def _performance_budget_summary(
    timings_ms: dict[str, float],
    *,
    budgets_ms: dict[str, float] | None,
    file_count: int = 0,
) -> dict[str, Any]:
    budgets = {**DEFAULT_PERFORMANCE_BUDGET_MS, **(budgets_ms or {})}
    budget_model: dict[str, Any] = {}
    if budgets_ms is None or "current" not in budgets_ms:
        adaptive_current_budget = (
            CURRENT_FRESHNESS_BUDGET_BASE_MS
            + max(file_count, 0) * CURRENT_FRESHNESS_BUDGET_PER_FILE_MS
        )
        budgets["current"] = max(budgets["current"], adaptive_current_budget)
        budget_model["current"] = {
            "base_ms": CURRENT_FRESHNESS_BUDGET_BASE_MS,
            "per_file_ms": CURRENT_FRESHNESS_BUDGET_PER_FILE_MS,
            "file_count": max(file_count, 0),
            "reason": "current includes source freshness scanning",
        }
    violations = [
        {
            "operation": operation,
            "duration_ms": duration,
            "budget_ms": budgets[operation],
        }
        for operation, duration in sorted(timings_ms.items())
        if operation in budgets and duration > budgets[operation]
    ]
    unmeasured = sorted(set(budgets) - set(timings_ms))
    return {
        "budgets_ms": budgets,
        "timings_ms": dict(sorted(timings_ms.items())),
        "budget_model": budget_model,
        "violation_count": len(violations),
        "violations": violations,
        "unmeasured": unmeasured,
    }


def _release_gate_summary(current: dict[str, Any]) -> dict[str, Any]:
    capabilities = current.get("capabilities", {})
    selected_queries = {
        "semantic_stats": capabilities.get("semantic_stats"),
        "unresolved": capabilities.get("unresolved"),
        "bindings": capabilities.get("bindings"),
        "types": capabilities.get("types"),
        "impact": "available",
    }
    unavailable = [
        query
        for query, status in selected_queries.items()
        if status is not None
        and status not in {"available", "basic", "call-import-entrypoint-resource"}
    ]
    missing = [query for query, status in selected_queries.items() if status is None]
    receiver_resolution = capabilities.get("receiver_resolution", "available")
    fallback_window_expired = schema_version_reached(
        SCHEMA_VERSION,
        V2_RECEIVER_RESOLUTION_DEFAULT_SCHEMA_VERSION,
    )
    fallback_contract = {
        "legacy_ast_call_resolution_default": False,
        "v2_receiver_resolution_default": True,
        "build_flag": "--semantic-call-resolution",
        "rebuild_flag": "ops rebuild --semantic-call-resolution",
        "compat_build_flag": "--semantic-call-resolution",
        "compat_rebuild_flag": "ops rebuild --semantic-call-resolution",
        "fallback_window": "closed",
        "fallback_expires_at_schema_version": (
            V2_RECEIVER_RESOLUTION_DEFAULT_SCHEMA_VERSION
        ),
        "fallback_window_expired": fallback_window_expired,
        "default_after_schema_version": V2_RECEIVER_RESOLUTION_DEFAULT_SCHEMA_VERSION,
        "legacy_escape_hatch": "--legacy-call-resolution",
    }
    exceptions = [
        {"kind": "query_unavailable", "query": query} for query in unavailable
    ]
    if receiver_resolution not in {"available", "legacy", "opt-in"}:
        exceptions.append(
            {
                "kind": "receiver_resolution_unknown",
                "status": receiver_resolution,
            }
        )
    if fallback_window_expired and receiver_resolution in {"opt-in", "legacy"}:
        exceptions.append(
            {
                "kind": "legacy_receiver_resolution_enabled",
                "status": receiver_resolution,
                "required_default_schema_version": (
                    V2_RECEIVER_RESOLUTION_DEFAULT_SCHEMA_VERSION
                ),
            }
        )
    return {
        "selected_v2_queries": selected_queries,
        "unavailable_selected_queries": unavailable,
        "missing_capability_metadata": missing,
        "receiver_resolution": receiver_resolution,
        "fallback_contract": fallback_contract,
        "exception_count": len(exceptions),
        "exceptions": exceptions,
    }


def _layer_violation_report(
    query_engine: QueryEngine,
    current: dict[str, Any],
    *,
    max_results: int,
) -> dict[str, Any]:
    """Report layer violations, and whether any layer model applied at all.

    The built-in model names a conventional web layering. A repository laid out
    differently places no file in any layer, so the check would inspect nothing
    and still report that no violations were detected. That reads as evidence
    of clean layering while proving only that the model did not match, which is
    why `applicable` and `classified_edges` are reported beside the result.
    """

    configured = _configured_layer_model(current)
    order = configured or {"model": 0, "repository": 1, "service": 2, "api": 3}
    prefixes = _configured_layer_prefixes(current) if configured else None
    allowed = _configured_layer_exemptions(current) if configured else {}
    violations: list[dict[str, Any]] = []
    exempted: list[dict[str, Any]] = []
    classified = 0
    with query_engine.store.connect() as conn:
        rows = conn.execute("""
            SELECT e.source, e.target, source.path AS source_path, target.path AS target_path
            FROM edges e
            JOIN nodes source ON source.id = e.source
            JOIN nodes target ON target.id = e.target
            WHERE e.kind = 'imports'
            ORDER BY e.source, e.target
            """).fetchall()

    for row in rows:
        if _is_test_path(row["source_path"]):
            continue
        source_layer = _layer_for_path(row["source_path"], prefixes)
        target_layer = _layer_for_path(row["target_path"], prefixes)
        if not source_layer or not target_layer:
            continue
        if source_layer not in order or target_layer not in order:
            continue
        classified += 1
        if order[target_layer] > order[source_layer]:
            reason = allowed.get((source_layer, target_layer))
            if reason is not None:
                exempted.append(
                    {
                        "source_layer": source_layer,
                        "target_layer": target_layer,
                        "source_path": row["source_path"],
                        "target_path": row["target_path"],
                        "reason": reason,
                    }
                )
                continue
            violations.append(
                {
                    "source": row["source"],
                    "target": row["target"],
                    "source_layer": source_layer,
                    "target_layer": target_layer,
                    "source_path": row["source_path"],
                    "target_path": row["target_path"],
                }
            )
        if len(violations) >= max_results:
            break
    return {
        "violations": violations,
        # Exemptions are reported, never silently dropped: an accepted inward
        # import is still a coupling a reader should be able to see and count.
        "exempted": exempted,
        "exempted_count": len(exempted),
        "applicable": classified > 0,
        "classified_edges": classified,
        "model_source": "configured" if configured else "default",
        "layers": sorted(order, key=order.__getitem__),
    }


def _unresolved_classification_summary(
    query_engine: QueryEngine, *, max_results: int
) -> dict[str, Any]:
    payload = query_engine.unresolved(limit=10000)
    classification = classify_unresolved_records(payload)
    records = classification.get("records", [])
    if isinstance(records, list):
        classification["records"] = records[:max_results]
        classification["truncated_records"] = max(0, len(records) - max_results)
    return classification


def _diagnostic_lifecycle_summary(
    query_engine: QueryEngine, *, max_results: int
) -> dict[str, Any]:
    current_index = str(query_engine.store.metadata.get("index_version") or "")
    with query_engine.store.connect() as conn:
        rows = conn.execute("""
            SELECT diagnostic_id, diagnostic_kind, severity, path, start_line,
                frontend_name, first_seen_index, last_seen_index, seen_count
            FROM diagnostics
            ORDER BY diagnostic_kind, path, start_line, diagnostic_id
            """).fetchall()

    new_diagnostics: list[dict[str, Any]] = []
    existing_diagnostics: list[dict[str, Any]] = []
    by_kind: dict[str, dict[str, int]] = {}
    for row in rows:
        item = {
            "diagnostic_id": row["diagnostic_id"],
            "diagnostic_kind": row["diagnostic_kind"],
            "severity": row["severity"],
            "path": row["path"],
            "start_line": row["start_line"],
            "frontend_name": row["frontend_name"],
            "first_seen_index": row["first_seen_index"],
            "last_seen_index": row["last_seen_index"],
            "seen_count": row["seen_count"],
        }
        lifecycle_status = (
            "new" if row["first_seen_index"] == current_index else "existing"
        )
        by_kind.setdefault(row["diagnostic_kind"], {"new": 0, "existing": 0})[
            lifecycle_status
        ] += 1
        if lifecycle_status == "new":
            new_diagnostics.append(item)
        else:
            existing_diagnostics.append(item)

    return {
        "current_index": current_index,
        "diagnostics_total": len(rows),
        "new_diagnostics_count": len(new_diagnostics),
        "existing_diagnostics_count": len(existing_diagnostics),
        "retention_policy": {"recent_index_versions": 3},
        "new_diagnostics": new_diagnostics[:max_results],
        "existing_diagnostics": existing_diagnostics[:max_results],
        "by_kind": dict(sorted(by_kind.items())),
    }


def _stale_target_summary(
    query_engine: QueryEngine, *, max_results: int
) -> dict[str, Any]:
    with query_engine.store.connect() as conn:
        rows = conn.execute("""
            SELECT diagnostic_id, properties_json
            FROM diagnostics
            WHERE diagnostic_kind = 'stale_target'
            ORDER BY diagnostic_id
            """).fetchall()

    stale_edges_count = 0
    recommended_paths: set[str] = set()
    diagnostics: list[dict[str, Any]] = []
    for row in rows:
        properties = json.loads(row["properties_json"])
        count = properties.get("stale_edges_count", 0)
        if isinstance(count, int):
            stale_edges_count += count
        paths = properties.get("reanalysis_recommended_paths", [])
        if isinstance(paths, list):
            recommended_paths.update(str(path) for path in paths if path)
        diagnostics.append(
            {
                "diagnostic_id": row["diagnostic_id"],
                "stale_edges_count": count if isinstance(count, int) else 0,
                "reanalysis_recommended_paths": (
                    paths if isinstance(paths, list) else []
                ),
            }
        )

    return {
        "diagnostics_count": len(rows),
        "stale_edges_count": stale_edges_count,
        "reanalysis_recommended_paths": sorted(recommended_paths)[:max_results],
        "diagnostics": diagnostics[:max_results],
    }


def _adapter_diagnostics_summary(
    query_engine: QueryEngine, *, max_results: int
) -> dict[str, Any]:
    with query_engine.store.connect() as conn:
        rows = conn.execute(
            """
            SELECT kind, path, message
            FROM warnings
            WHERE kind LIKE 'adapter_%'
            ORDER BY id
            LIMIT ?
            """,
            (max_results,),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM warnings WHERE kind LIKE 'adapter_%'"
        ).fetchone()[0]
        errors = conn.execute(
            "SELECT COUNT(*) FROM warnings WHERE kind = 'adapter_error'"
        ).fetchone()[0]

    return {
        "warnings_count": total,
        "errors_count": errors,
        "warnings": [
            {"kind": row["kind"], "path": row["path"], "message": row["message"]}
            for row in rows
        ],
    }


def _adapter_metrics_summary(
    current: dict[str, Any], *, max_results: int
) -> dict[str, Any]:
    adapters = current.get("adapter_metrics", {}).get("adapters", {})
    if not isinstance(adapters, dict) or not adapters:
        return {
            "available": False,
            "adapter_count": 0,
            "regression_count": 0,
            "error_count": 0,
            "adapters": {},
            "regressions": [],
        }

    regressions: list[dict[str, Any]] = []
    for name, metrics in sorted(adapters.items()):
        if not isinstance(metrics, dict):
            continue
        status = metrics.get("status", "unknown")
        warnings_count = _int_metric(metrics.get("warnings"))
        if status not in ("available", "skipped") or warnings_count > 0:
            regressions.append(
                {
                    "adapter": name,
                    "status": status,
                    "warnings": warnings_count,
                    "unresolved_registrations": _int_metric(
                        metrics.get("unresolved_registrations")
                    ),
                }
            )
    return {
        "available": True,
        "adapter_count": len(adapters),
        "regression_count": len(regressions),
        "error_count": sum(
            1
            for metrics in adapters.values()
            if isinstance(metrics, dict) and metrics.get("status") == "error"
        ),
        "adapters": {
            name: {
                "status": (
                    metrics.get("status", "unknown")
                    if isinstance(metrics, dict)
                    else "unknown"
                ),
                "nodes": (
                    _int_metric(metrics.get("nodes"))
                    if isinstance(metrics, dict)
                    else 0
                ),
                "edges": (
                    _int_metric(metrics.get("edges"))
                    if isinstance(metrics, dict)
                    else 0
                ),
                "discovered_registrations": (
                    _int_metric(metrics.get("discovered_registrations"))
                    if isinstance(metrics, dict)
                    else 0
                ),
                "resolved_handlers": (
                    _int_metric(metrics.get("resolved_handlers"))
                    if isinstance(metrics, dict)
                    else 0
                ),
                "warnings": (
                    _int_metric(metrics.get("warnings"))
                    if isinstance(metrics, dict)
                    else 0
                ),
                "unresolved_registrations": (
                    _int_metric(metrics.get("unresolved_registrations"))
                    if isinstance(metrics, dict)
                    else 0
                ),
            }
            for name, metrics in sorted(adapters.items())
        },
        "regressions": regressions[:max_results],
    }


def _precision_summary(current: dict[str, Any]) -> dict[str, Any]:
    capabilities = current.get("capabilities", {})
    status = capabilities.get("precision", "ast_fallback_only")
    metrics = current.get("precision_metrics", {})
    metrics = metrics if isinstance(metrics, dict) else {}
    pyright_status = str(metrics.get("pyright_status") or "unavailable")
    scip_type_occurrences = _int_metric(metrics.get("scip_type_occurrences"))
    type_occurrences = _int_metric(metrics.get("type_occurrences"))
    pyright_type_info_total = _int_metric(metrics.get("pyright_type_info_total"))
    pyright_lsp_type_info_total = _int_metric(
        metrics.get("pyright_lsp_type_info_total")
    )
    type_precision_complete = (
        pyright_status == "available"
        and type_occurrences > 0
        and pyright_lsp_type_info_total > 0
    )
    return {
        "status": status,
        "precise_references": capabilities.get("precise_references", "ast-fallback"),
        "pyright_status": pyright_status,
        "scip_type_occurrences": scip_type_occurrences,
        "type_occurrences": type_occurrences,
        "pyright_type_info_total": pyright_type_info_total,
        "pyright_lsp_type_info_total": pyright_lsp_type_info_total,
        "pyright_lsp_probe_total": _int_metric(metrics.get("pyright_lsp_probe_total")),
        "pyright_lsp_requestable_probe_total": _int_metric(
            metrics.get("pyright_lsp_requestable_probe_total")
        ),
        "pyright_lsp_skipped_unmappable_total": _int_metric(
            metrics.get("pyright_lsp_skipped_unmappable_total")
        ),
        "pyright_lsp_requests_total": _int_metric(
            metrics.get("pyright_lsp_requests_total")
        ),
        "pyright_lsp_unresolved_total": _int_metric(
            metrics.get("pyright_lsp_unresolved_total")
        ),
        "type_precision_complete": type_precision_complete,
        "metrics": metrics,
    }


def _precision_message(summary: dict[str, Any]) -> str:
    status = summary.get("status")
    type_complete = bool(summary.get("type_precision_complete"))
    pyright_status = str(summary.get("pyright_status", "unavailable"))
    scip_type_occurrences = _int_metric(summary.get("scip_type_occurrences"))
    type_occurrences = _int_metric(summary.get("type_occurrences"))
    pyright_type_info_total = _int_metric(summary.get("pyright_type_info_total"))
    pyright_lsp_type_info_total = _int_metric(
        summary.get("pyright_lsp_type_info_total")
    )
    if status == "precision_available":
        if type_complete:
            return "Precision inputs are available and imported; type precision is complete."
        return (
            "Precision inputs are available, but type precision is incomplete "
            f"(pyright_status={pyright_status}, "
            f"scip_type_occurrences={scip_type_occurrences}, "
            f"pyright_type_info_total={pyright_type_info_total}, "
            f"pyright_lsp_type_info_total={pyright_lsp_type_info_total}, "
            f"type_occurrences={type_occurrences})."
        )
    if status == "precision_partial":
        return "Precision inputs are configured but partially unavailable."
    return "No precision input configured; AST fallback is explicit."


def _coverage_summary(current: dict[str, Any]) -> dict[str, Any]:
    capabilities = current.get("capabilities", {})
    metrics = current.get("coverage_metrics", {})
    metric_status = metrics.get("status") if isinstance(metrics, dict) else None
    status = str(metric_status or capabilities.get("coverage", "unavailable"))
    return {
        "status": status,
        "capability": capabilities.get("coverage", "unavailable"),
        "metrics": metrics if isinstance(metrics, dict) else {},
    }


def _coverage_message(status: str) -> str:
    if status == "available":
        return "Coverage input is available and imported."
    if status == "partial":
        return "Coverage input is configured but partially unavailable or stale."
    return "No coverage input configured; test recommendations remain heuristic."


def _runtime_trace_summary(current: dict[str, Any]) -> dict[str, Any]:
    capabilities = current.get("capabilities", {})
    metrics = current.get("runtime_metrics", {})
    metrics = metrics if isinstance(metrics, dict) else {}
    metric_status = metrics.get("status")
    status = str(metric_status or capabilities.get("runtime_trace", "unavailable"))
    if metrics.get("stale_trace") is True:
        status = "stale"
    return {
        "status": status,
        "capability": capabilities.get("runtime_trace", "unavailable"),
        "stale_trace": metrics.get("stale_trace") is True,
        "metrics": metrics,
    }


def _runtime_trace_message(status: str) -> str:
    if status == "available":
        return "Runtime trace input is available and imported."
    if status in {"partial", "stale"}:
        return "Runtime trace input is configured but partially unavailable or stale."
    return "No runtime trace input configured; static and coverage evidence remain explicit."


def _evidence_commit_consistency_summary(current: dict[str, Any]) -> dict[str, Any]:
    repo_root = current.get("repo_root")
    head_commit_sha = (
        current_commit(Path(str(repo_root))) if isinstance(repo_root, str) else None
    )
    index_commit_sha = _str_metric(current.get("commit_sha"))
    runtime_metrics = current.get("runtime_metrics", {})
    runtime_metrics = runtime_metrics if isinstance(runtime_metrics, dict) else {}
    runtime_trace_commit_sha = _str_metric(runtime_metrics.get("commit_sha"))
    checked = bool(head_commit_sha)
    index_matches_head = (
        bool(checked and index_commit_sha == head_commit_sha)
        if index_commit_sha
        else not checked
    )
    runtime_trace_matches_head = (
        bool(checked and runtime_trace_commit_sha == head_commit_sha)
        if runtime_trace_commit_sha
        else True
    )
    return {
        "checked": checked,
        "head_commit_sha": head_commit_sha,
        "index_commit_sha": index_commit_sha,
        "runtime_trace_commit_sha": runtime_trace_commit_sha,
        "index_matches_head": index_matches_head,
        "runtime_trace_matches_head": runtime_trace_matches_head,
        "consistent": (
            not checked or (index_matches_head and runtime_trace_matches_head)
        ),
    }


def _high_risk_unresolved_summary(
    risk: dict[str, Any], *, max_results: int
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    total = 0
    for report in risk.get("target_reports", []):
        is_high_risk = bool(
            report.get("entrypoint_count", 0) or report.get("resource_count", 0)
        )
        unresolved_count = report.get("unresolved_risk_count", 0)
        if not isinstance(unresolved_count, int) or not is_high_risk:
            continue
        if unresolved_count <= 0:
            continue
        total += unresolved_count
        reports.append(
            {
                "target": report.get("target"),
                "resolved_targets": report.get("resolved_targets", [])[:max_results],
                "entrypoint_count": report.get("entrypoint_count", 0),
                "resource_count": report.get("resource_count", 0),
                "unresolved_risk_count": unresolved_count,
                "unresolved_risks": report.get("unresolved_risks", [])[:max_results],
            }
        )
        if len(reports) >= max_results:
            break
    return {
        "unresolved_callsite_total": total,
        "targets": reports,
    }


def _configured_layer_model(current: dict[str, Any]) -> dict[str, int] | None:
    """Read an ordered layer model, innermost first, from pyproject.toml."""

    entries = _pyproject_arcgraph_ci(current).get("layers")
    if not isinstance(entries, list) or not entries:
        return None
    order: dict[str, int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        if not isinstance(name, str) or not name or name in order:
            return None
        order[name] = index
    return order


def _configured_layer_prefixes(
    current: dict[str, Any],
) -> dict[str, tuple[str, ...]] | None:
    entries = _pyproject_arcgraph_ci(current).get("layers")
    if not isinstance(entries, list) or not entries:
        return None
    prefixes: dict[str, tuple[str, ...]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        paths = entry.get("paths")
        if not isinstance(name, str) or not isinstance(paths, list):
            continue
        prefixes[name] = tuple(
            str(value).replace("\\", "/") for value in paths if isinstance(value, str)
        )
    return prefixes or None


def _configured_layer_exemptions(
    current: dict[str, Any],
) -> dict[tuple[str, str], str]:
    """Read accepted inward imports, each with the reason it is accepted.

    An exemption records a coupling the repository has decided to keep, so the
    honest form is a named pair with a reason rather than deleting the edge or
    relocating a constant until the import graph stops showing it.
    """

    entries = _pyproject_arcgraph_ci(current).get("layer_exemptions")
    if not isinstance(entries, list):
        return {}
    exemptions: dict[tuple[str, str], str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = entry.get("from")
        target = entry.get("to")
        reason = entry.get("reason")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        if not isinstance(reason, str) or not reason.strip():
            continue
        exemptions[(source, target)] = reason
    return exemptions


def _pyproject_arcgraph_ci(current: dict[str, Any]) -> dict[str, Any]:
    repo_root = current.get("repo_root")
    if not repo_root:
        return {}
    pyproject_path = Path(str(repo_root)) / "pyproject.toml"
    if not pyproject_path.exists():
        return {}
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("tool", {}).get("arcgraph", {}).get("ci", {})
    return section if isinstance(section, dict) else {}


def _layer_for_path(
    path: str | None,
    prefixes: dict[str, tuple[str, ...]] | None = None,
) -> str | None:
    if not path:
        return None
    normalized = path.replace("\\", "/")
    if prefixes is not None:
        for name, candidates in prefixes.items():
            if any(normalized.startswith(candidate) for candidate in candidates):
                return name
        return None
    parts = set(normalized.split("/"))
    stem = normalized.rsplit("/", 1)[-1].removesuffix(".py")
    if "api" in parts or stem.endswith("_routes") or stem == "api":
        return "api"
    if "services" in parts or stem.endswith("_service") or stem == "service":
        return "service"
    if (
        "repositories" in parts
        or "repository" in parts
        or stem.endswith("_repo")
        or stem == "repository"
    ):
        return "repository"
    if "models" in parts or stem == "model":
        return "model"
    return None


def _is_test_path(path: str | None) -> bool:
    if not path:
        return False
    normalized = path.replace("\\", "/")
    return (
        normalized.startswith("tests/")
        or "/tests/" in normalized
        or normalized.startswith("backend/tests/")
    )


def _similarity_warnings(
    query_engine: QueryEngine,
    *,
    targets: list[str],
    threshold: float,
    max_results: int,
) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    for target in _expand_similarity_targets(
        query_engine, targets, max_results=max_results
    ):
        result = query_engine.similar(target, max_results=max_results)
        for item in result.get("similar", []):
            score = item.get("score", 0.0)
            if score >= threshold:
                hints.append(
                    {
                        "target": target,
                        "similar": item.get("node", {}).get("id"),
                        "score": score,
                        "reasons": item.get("reasons", []),
                    }
                )
        if len(hints) >= max_results:
            break
    return hints[:max_results]


def _expand_similarity_targets(
    query_engine: QueryEngine, targets: list[str], *, max_results: int
) -> list[str]:
    expanded: list[str] = []
    with query_engine.store.connect() as conn:
        for target in targets:
            resolution = query_engine._resolve_target(conn, target)
            resolved = list(resolution.resolved_ids)
            if resolution.status == "ambiguous":
                # Similarity hints are a sweep, not a one-target analysis:
                # an ambiguous name must expand to every bounded candidate,
                # not silently to none.
                resolved = [
                    str(item["id"]) for item in resolution.candidates if item.get("id")
                ]
            if not resolved:
                expanded.append(target)
                continue
            symbol_ids = [
                node_id
                for node_id in resolved
                if node_id.startswith(("fn:", "method:"))
            ]
            expanded.extend(symbol_ids or resolved)
            if len(expanded) >= max_results:
                break
    return _unique(expanded)[:max_results]


def _semantic_summary(semantic_stats: dict[str, Any]) -> dict[str, Any]:
    metrics = semantic_stats.get("metrics", {})
    binding_summary = semantic_stats.get("binding_summary") or metrics.get(
        "binding_summary", {}
    )
    type_summary = semantic_stats.get("type_summary") or metrics.get("type_summary", {})
    return {
        "callsite_total": metrics.get("callsite_total", 0),
        "resolved_callsite_total": metrics.get("resolved_callsite_total", 0),
        "unresolved_callsite_total": metrics.get("unresolved_callsite_total", 0),
        "resolution_rate": metrics.get("resolution_rate", 1.0),
        "binding_summary": binding_summary,
        "type_summary": type_summary,
        "by_context": metrics.get("by_context", {}),
        "by_expression_kind": metrics.get("by_expression_kind", {}),
        "resolved_by_expression_kind": metrics.get("resolved_by_expression_kind", {}),
        "unresolved_by_expression_kind": metrics.get(
            "unresolved_by_expression_kind", {}
        ),
        "resolution_rate_by_expression_kind": metrics.get(
            "resolution_rate_by_expression_kind", {}
        ),
        "by_diagnostic_kind": metrics.get("by_diagnostic_kind", {}),
    }


def _semantic_resolution_trend(
    semantic_summary: dict[str, Any],
    *,
    semantic_baseline: dict[str, Any] | None,
    tolerance: float,
) -> dict[str, Any]:
    baseline = _semantic_baseline_summary(semantic_baseline)
    current_rate = _float_metric(semantic_summary.get("resolution_rate"), default=1.0)
    if not baseline:
        return {
            "baseline_available": False,
            "regressed": False,
            "current_resolution_rate": current_rate,
            "baseline_resolution_rate": None,
            "tolerance": tolerance,
        }

    baseline_rate = _float_metric(baseline.get("resolution_rate"), default=1.0)
    delta = round(current_rate - baseline_rate, 4)
    return {
        "baseline_available": True,
        "regressed": current_rate + tolerance < baseline_rate,
        "current_resolution_rate": current_rate,
        "baseline_resolution_rate": baseline_rate,
        "delta": delta,
        "tolerance": tolerance,
        "current_callsite_total": semantic_summary.get("callsite_total", 0),
        "baseline_callsite_total": baseline.get("callsite_total", 0),
    }


def _semantic_baseline_summary(
    payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not payload:
        return None
    if "semantic_summary" in payload and isinstance(payload["semantic_summary"], dict):
        return payload["semantic_summary"]
    if "metrics" in payload and isinstance(payload["metrics"], dict):
        return _semantic_summary(payload)
    if "resolution_rate" in payload:
        return payload
    return None


def _semantic_quality_target_summary(
    semantic_summary: dict[str, Any],
    current: dict[str, Any],
    *,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    targets = _semantic_quality_targets(current)
    current_overall = _float_metric(
        semantic_summary.get("resolution_rate"), default=1.0
    )
    expression_counts = semantic_summary.get("by_expression_kind", {})
    attribute_total = (
        _int_metric(expression_counts.get("attribute"))
        if isinstance(expression_counts, dict)
        else 0
    )
    chain_total = (
        _int_metric(expression_counts.get("chain"))
        if isinstance(expression_counts, dict)
        else 0
    )
    expression_rates = semantic_summary.get("resolution_rate_by_expression_kind", {})
    attribute_rate = (
        _float_metric(expression_rates.get("attribute"), default=None)
        if isinstance(expression_rates, dict)
        else None
    )
    chain_rate = (
        _float_metric(expression_rates.get("chain"), default=None)
        if isinstance(expression_rates, dict)
        else None
    )
    adapter_summary = _adapter_metrics_summary(current, max_results=100)
    adapters = adapter_summary.get("adapters", {})
    discovered = 0
    unresolved = 0
    framework_by_adapter: dict[str, dict[str, Any]] = {}
    for adapter_name, metrics in (
        adapters.items() if isinstance(adapters, dict) else []
    ):
        if not isinstance(metrics, dict):
            continue
        adapter_discovered = _int_metric(metrics.get("discovered_registrations"))
        adapter_unresolved = _int_metric(metrics.get("unresolved_registrations"))
        if adapter_discovered <= 0 and adapter_unresolved <= 0:
            continue
        adapter_resolved = max(adapter_discovered - adapter_unresolved, 0)
        adapter_rate = (
            round(adapter_resolved / adapter_discovered, 4)
            if adapter_discovered
            else None
        )
        framework_by_adapter[str(adapter_name)] = {
            "discovered_registrations": adapter_discovered,
            "resolved_registrations": adapter_resolved,
            "unresolved_registrations": adapter_unresolved,
            "resolution_rate": adapter_rate,
        }
        discovered += adapter_discovered
        unresolved += adapter_unresolved
    resolved = max(discovered - unresolved, 0)
    framework_rate = round(resolved / discovered, 4) if discovered else None
    exceptions: list[dict[str, Any]] = []
    if current_overall < targets["overall_callsite_resolution_rate"]:
        exceptions.append(
            {
                "metric": "overall_callsite_resolution_rate",
                "current": current_overall,
                "target": targets["overall_callsite_resolution_rate"],
                "reason": "Overall callsite resolution is below the stable schema target.",
            }
        )
    if attribute_rate is None:
        exceptions.append(
            {
                "metric": "attribute_call_resolution_rate",
                "current": None,
                "target": targets["attribute_call_resolution_rate"],
                "attribute_call_total": attribute_total,
                "reason": "Per-expression resolved counts are not persisted for this index.",
            }
        )
    elif attribute_rate < targets["attribute_call_resolution_rate"]:
        exceptions.append(
            {
                "metric": "attribute_call_resolution_rate",
                "current": attribute_rate,
                "target": targets["attribute_call_resolution_rate"],
                "attribute_call_total": attribute_total,
                "reason": "Attribute call resolution remains below target.",
            }
        )
    if chain_rate is None:
        exceptions.append(
            {
                "metric": "chain_call_resolution_rate",
                "current": None,
                "target": targets["chain_call_resolution_rate"],
                "chain_call_total": chain_total,
                "reason": "Per-expression resolved counts are not persisted for this index.",
            }
        )
    elif chain_rate < targets["chain_call_resolution_rate"]:
        exceptions.append(
            {
                "metric": "chain_call_resolution_rate",
                "current": chain_rate,
                "target": targets["chain_call_resolution_rate"],
                "chain_call_total": chain_total,
                "reason": "Chain call resolution remains below target.",
            }
        )
    if framework_rate is None:
        exceptions.append(
            {
                "metric": "known_framework_registration_resolution_rate",
                "current": None,
                "target": targets["known_framework_registration_resolution_rate"],
                "reason": "Adapter metrics are unavailable for this compatibility index.",
            }
        )
    elif framework_rate < targets["known_framework_registration_resolution_rate"]:
        exceptions.append(
            {
                "metric": "known_framework_registration_resolution_rate",
                "current": framework_rate,
                "target": targets["known_framework_registration_resolution_rate"],
                "reason": "Adapter unresolved registrations are visible in adapter metrics.",
            }
        )
    return {
        "targets": targets,
        "scope": scope
        or {
            "mode": "full_index",
            "include": [],
            "exclude": [],
            "source_node_count": None,
            "filtered_node_count": None,
            "source_callsite_total": semantic_summary.get("callsite_total", 0),
            "filtered_callsite_total": semantic_summary.get("callsite_total", 0),
        },
        "current": {
            "overall_callsite_resolution_rate": current_overall,
            "attribute_call_resolution_rate": attribute_rate,
            "attribute_call_total": attribute_total,
            "chain_call_resolution_rate": chain_rate,
            "chain_call_total": chain_total,
            "known_framework_registration_resolution_rate": framework_rate,
            "known_framework_registration_metric_source": "adapter_metrics",
            "known_framework_registration_discovered_total": discovered,
            "known_framework_registration_resolved_total": resolved,
            "known_framework_registration_unresolved_total": unresolved,
            "known_framework_registration_by_adapter": framework_by_adapter,
        },
        # There is no mechanism for declaring an accepted allowance: this flag
        # only reports whether any metric is currently below its target. The
        # earlier name implied a reviewable budget that does not exist.
        "exceptions_present": bool(exceptions),
        "exceptions": exceptions,
    }


def _semantic_quality_summary_for_targets(
    query_engine: QueryEngine,
    semantic_summary: dict[str, Any],
    current: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    scope_config = _semantic_quality_scope(current)
    include = scope_config["include"]
    exclude = scope_config["exclude"]
    if not include and not exclude:
        return semantic_summary, {
            "mode": "full_index",
            "include": [],
            "exclude": [],
            "source_node_count": None,
            "filtered_node_count": None,
            "source_callsite_total": semantic_summary.get("callsite_total", 0),
            "filtered_callsite_total": semantic_summary.get("callsite_total", 0),
        }

    nodes = query_engine.store.read_nodes()
    filtered_nodes = [
        node
        for node in nodes
        if _matches_semantic_quality_scope(node.path, include=include, exclude=exclude)
    ]
    metrics = collect_semantic_metrics_from_resolved_callsites(
        filtered_nodes,
        resolved_callsite_ids(query_engine.store.read_edges()),
        edge_kind_counts=Counter(),
        confidence_counts=Counter(),
    )
    scoped_summary = _semantic_summary({"metrics": metrics})
    return scoped_summary, {
        "mode": "configured",
        "include": include,
        "exclude": exclude,
        "source_node_count": len(nodes),
        "filtered_node_count": len(filtered_nodes),
        "source_callsite_total": semantic_summary.get("callsite_total", 0),
        "filtered_callsite_total": scoped_summary.get("callsite_total", 0),
    }


def _semantic_quality_targets(current: dict[str, Any]) -> dict[str, float]:
    targets = {
        "overall_callsite_resolution_rate": 0.96,
        "attribute_call_resolution_rate": 0.93,
        "chain_call_resolution_rate": 0.93,
        "known_framework_registration_resolution_rate": 0.95,
    }
    repo_root = current.get("repo_root")
    if not repo_root:
        return targets
    pyproject_path = Path(str(repo_root)) / "pyproject.toml"
    if not pyproject_path.exists():
        return targets
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return targets
    configured = (
        data.get("tool", {})
        .get("arcgraph", {})
        .get("ci", {})
        .get("semantic_quality_targets", {})
    )
    if not isinstance(configured, dict):
        return targets
    for key in list(targets):
        value = configured.get(key)
        if isinstance(value, int | float):
            targets[key] = float(value)
    return targets


def _configured_semantic_baseline(current: dict[str, Any]) -> dict[str, Any] | None:
    """Read the committed resolution baseline the trend check ratchets against.

    An explicit ``--semantic-baseline`` still wins. Without this fallback the
    trend check has no baseline in any ordinary run, so it reports the current
    numbers and can never find a regression. Keeping the value beside the
    quality targets makes raising it a reviewed act rather than invisible
    drift, and makes the check apply locally and in CI without extra wiring.
    """

    repo_root = current.get("repo_root")
    if not repo_root:
        return None
    pyproject_path = Path(str(repo_root)) / "pyproject.toml"
    if not pyproject_path.exists():
        return None
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    configured = (
        data.get("tool", {})
        .get("arcgraph", {})
        .get("ci", {})
        .get("semantic_resolution_baseline", {})
    )
    if not isinstance(configured, dict):
        return None
    rate = configured.get("resolution_rate")
    if not isinstance(rate, int | float):
        return None
    baseline: dict[str, Any] = {"resolution_rate": float(rate)}
    callsite_total = configured.get("callsite_total")
    if isinstance(callsite_total, int):
        baseline["callsite_total"] = callsite_total
    return baseline


def _semantic_quality_scope(current: dict[str, Any]) -> dict[str, list[str]]:
    repo_root = current.get("repo_root")
    if not repo_root:
        return {"include": [], "exclude": []}
    pyproject_path = Path(str(repo_root)) / "pyproject.toml"
    if not pyproject_path.exists():
        return {"include": [], "exclude": []}
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {"include": [], "exclude": []}
    configured = (
        data.get("tool", {})
        .get("arcgraph", {})
        .get("ci", {})
        .get("semantic_quality_scope", {})
    )
    if not isinstance(configured, dict):
        return {"include": [], "exclude": []}
    return {
        "include": _path_patterns(configured.get("include")),
        "exclude": _path_patterns(configured.get("exclude")),
    }


def _path_patterns(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    patterns: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        pattern = item.replace("\\", "/").strip()
        if pattern:
            patterns.append(pattern)
    return patterns


def _matches_semantic_quality_scope(
    path: str | None,
    *,
    include: list[str],
    exclude: list[str],
) -> bool:
    if not path:
        return not include
    normalized = path.replace("\\", "/")
    if include and not _path_matches_any(normalized, include):
        return False
    return not _path_matches_any(normalized, exclude)


def _path_matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _pr_summary(
    risk: dict[str, Any],
    similar: list[dict[str, Any]],
    semantic_summary: dict[str, Any],
    high_risk_unresolved: dict[str, Any],
) -> dict[str, Any]:
    blast_radius = risk.get("blast_radius", {})
    return {
        "changed_symbols": risk.get("targets", []),
        "changed_files": risk.get("changed_files", []),
        "affected_entrypoints": [
            node.get("id") for node in blast_radius.get("entrypoints", [])
        ],
        "affected_resources": [
            node.get("id") for node in blast_radius.get("resources", [])
        ],
        "related_tests": [item.get("path") for item in risk.get("test_candidates", [])],
        "risk_factors": [item.get("kind") for item in risk.get("risk_factors", [])],
        "similarity": similar,
        "semantic_summary": semantic_summary,
        "high_risk_unresolved": high_risk_unresolved,
    }


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _int_metric(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _str_metric(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _float_metric(value: Any, *, default: float) -> float:
    return float(value) if isinstance(value, int | float) else default
