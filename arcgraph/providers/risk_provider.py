"""Agent-facing risk reports built from ArcGraph structural queries."""

from __future__ import annotations

from collections import Counter
from typing import Any

from arcgraph.core.assurance import (
    build_assurance,
    evidence_available,
    reference_evidence_available,
    target_coverage_status,
)
from arcgraph.core.payload_policy import (
    apply_target_payload_contract,
    bounded_target_resolution,
    normalize_target_groups,
    resolved_definition_paths,
    scoped_warning_inputs,
    unique_values,
)
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.recovery import stale_recovery_action
from arcgraph.core.schemas import RiskReport, SCHEMA_VERSION
from arcgraph.core.unresolved_classification import compact_unresolved_record
from arcgraph.core.utils import estimated_tokens

# A file target resolves to every symbol it contains; each verified symbol
# can cost one language-service run, so verification is bounded per target
# and the unverified remainder is disclosed on the entry.
_REFERENCE_VERIFICATION_SYMBOL_CAP = 3
# Verification cost is per symbol, and each symbol can cost one language
# service subprocess with a 30s ceiling. A per-target cap multiplies by the
# request's target count (up to MAX_TARGETS_PER_REQUEST), so the request
# carries its own budget. Targets past it are reported unverified rather
# than silently omitted — an absent status would let assurance fall back to
# the index-wide capability and claim more than it checked.
_REFERENCE_VERIFICATION_REQUEST_BUDGET = 12


class RiskProvider:
    def __init__(self, query_engine: QueryEngine) -> None:
        self.query_engine = query_engine

    def get_risk(
        self,
        targets: list[str] | None = None,
        *,
        changed_files: list[str] | None = None,
        max_depth: int = 2,
        max_results: int = 30,
        verify_references: bool = False,
    ) -> dict[str, Any]:
        normalized_targets, normalized_changed_files, query_targets = (
            normalize_target_groups(targets, changed_files)
        )
        current = self.query_engine.current()
        max_results = _bounded(max_results, default=30, upper=100)
        # Index-level warnings are scoped in at the end, once the report knows
        # which files it actually covers. Scoping has to happen before
        # ``_warning_text`` flattens each record and drops its ``path``.
        warnings: list[Any] = []
        unresolved_targets = False

        if not query_targets:
            warnings.append(
                "No targets or changed files were provided for risk analysis."
            )

        reports: list[dict[str, Any]] = []
        assurance_confidence_summaries: list[dict[str, Any]] = []
        assurance_coverage_statuses: list[str] = []
        assurance_analysis_limits: list[dict[str, Any]] = []
        entrypoints: dict[str, dict[str, Any]] = {}
        resources: dict[str, dict[str, Any]] = {}
        affected_symbols: dict[str, dict[str, Any]] = {}
        affected_modules: dict[str, dict[str, Any]] = {}
        tests: dict[str, dict[str, Any]] = {}
        test_gaps: dict[str, dict[str, Any]] = {}
        risk_factors: dict[str, dict[str, Any]] = {}
        similar_implementations: dict[str, dict[str, Any]] = {}
        traversal_truncated = False
        precise_references: list[dict[str, Any]] = []

        resolved_target_ids: set[str] = set()
        reference_symbol_budget = _REFERENCE_VERIFICATION_REQUEST_BUDGET
        for target in query_targets:
            impact = self.query_engine.impact(target, max_depth=max_depth)
            resolved_target_ids.update(
                node_id
                for node_id in impact.get("resolved_targets", [])
                if isinstance(node_id, str) and node_id
            )
            reports.append(_compact_target_report(impact, max_results))
            assurance_confidence_summaries.append(impact.get("confidence_summary", {}))
            assurance_coverage_statuses.append(
                target_coverage_status(
                    impact.get("coverage", {}).get("status", "unavailable"),
                    bool(impact.get("test_gaps")),
                )
            )
            assurance_analysis_limits.append(impact.get("analysis_limits", {}))
            # Impact's string caveats are represented structurally by assurance;
            # keep structured, target-scoped warnings without repeating prose.
            warnings.extend(
                warning
                for warning in impact.get("warnings", [])
                if isinstance(warning, dict)
            )
            if (
                impact.get("analysis_limits", {}).get("traversal", {}).get("truncated")
                is True
            ):
                traversal_truncated = True
                risk_factors.setdefault(
                    "analysis_truncated",
                    {
                        "kind": "analysis_truncated",
                        "severity": "high",
                        "reason": (
                            "At least one impact traversal hit an analysis guardrail; "
                            "the full blast radius is not known."
                        ),
                    },
                )

            for node in impact.get("entrypoint_impact", {}).get("entrypoints", []):
                entrypoints.setdefault(node["id"], node)
            for node in impact.get("resource_impact", {}).get("resources", []):
                resources.setdefault(node["id"], node)
            for node in impact.get("call_impact", {}).get("affected_symbols", []):
                affected_symbols.setdefault(node["id"], node)
            for node in impact.get("import_impact", {}).get("affected_modules", []):
                affected_modules.setdefault(node["id"], node)
            for candidate in impact.get("test_candidates", []):
                tests.setdefault(candidate["path"], candidate)
            for gap in impact.get("test_gaps", []):
                key = str(gap.get("target") or gap.get("reason"))
                test_gaps.setdefault(key, gap)

            if impact.get("entrypoint_impact", {}).get("entrypoints"):
                risk_factors.setdefault(
                    "entrypoint_impact",
                    {
                        "kind": "entrypoint_impact",
                        "severity": "medium",
                        "reason": "Target is reachable from indexed entrypoints (which may include tests).",
                    },
                )
            if any(
                edge["kind"] in {"writes", "publishes", "enqueues"}
                for edge in impact.get("resource_impact", {}).get("edges", [])
            ):
                risk_factors.setdefault(
                    "resource_write",
                    {
                        "kind": "resource_write",
                        "severity": "medium",
                        "reason": "Target writes or publishes to a table, queue, or external resource.",
                    },
                )
            if not impact.get("test_candidates"):
                risk_factors.setdefault(
                    "missing_test_candidate",
                    {
                        "kind": "missing_test_candidate",
                        "severity": "medium",
                        "reason": "No heuristic test candidate was found for at least one target.",
                    },
                )
            if impact.get("test_gaps"):
                risk_factors.setdefault(
                    "test_gap",
                    {
                        "kind": "test_gap",
                        "severity": "medium",
                        "reason": "Runtime coverage is missing or incomplete for at least one target.",
                    },
                )
            if not impact.get("resolved_targets"):
                unresolved_targets = True
                risk_factors.setdefault(
                    "unresolved_target",
                    {
                        "kind": "unresolved_target",
                        "severity": "low",
                        "reason": "At least one requested target did not resolve to graph nodes.",
                    },
                )
            else:
                similar = self.query_engine.similar(
                    target,
                    max_results=max_results,
                    detail_level="summary",
                )
                for item in similar.get("similar", []):
                    node = item.get("node", {})
                    node_id = node.get("id")
                    if not isinstance(node_id, str) or not node_id:
                        continue
                    similar_implementations.setdefault(
                        node_id,
                        {
                            **_compact_node(node),
                            "score": item.get("score"),
                            "reasons": list(item.get("reasons", []))[:3],
                        },
                    )
                if verify_references:
                    entry = self._verify_target_references(
                        target,
                        impact.get("resolved_targets", []),
                        max_results=max_results,
                        symbol_budget=reference_symbol_budget,
                    )
                    reference_symbol_budget -= len(entry.get("verified_symbols", []))
                    precise_references.append(entry)

        target_paths = resolved_definition_paths(
            self.query_engine,
            sorted(resolved_target_ids),
        )
        freshness = current["freshness"]
        if freshness.get("stale"):
            risk_factors["stale_index"] = {
                "kind": "stale_index",
                "severity": "high",
                "reason": "The current ArcGraph index is stale.",
                "stale_files": freshness.get("stale_files", [])[:max_results],
            }

        scoped_index_warnings = scoped_warning_inputs(
            current.get("warnings", []),
            _report_paths(
                entrypoints,
                resources,
                affected_symbols,
                affected_modules,
                tests,
            ),
            target_paths,
            (
                sorted(resolved_target_ids)
                if query_targets and not unresolved_targets
                else []
            ),
        )
        compact_index_warnings, warning_details_omitted = (
            _compact_preflight_index_warnings(
                scoped_index_warnings,
                scope_known=bool(
                    target_paths is not None
                    and query_targets
                    and not unresolved_targets
                ),
                max_results=max_results,
                priority_paths={*normalized_targets, *normalized_changed_files},
            )
        )

        truncation = _risk_truncation(
            {
                "blast_radius.entrypoints": entrypoints,
                "blast_radius.resources": resources,
                "blast_radius.affected_symbols": affected_symbols,
                "blast_radius.affected_modules": affected_modules,
                "test_candidates": tests,
                "test_gaps": test_gaps,
                "target_reports": reports,
                "similar_implementations": similar_implementations,
            },
            reports=reports,
            precise_references=precise_references,
            max_results=max_results,
            warning_details_omitted=warning_details_omitted,
        )
        assurance = build_assurance(
            current=current,
            target_resolutions=[
                report.get("target_resolution", {}) for report in reports
            ],
            confidence_summaries=assurance_confidence_summaries,
            coverage_statuses=assurance_coverage_statuses,
            precise_reference_statuses=[
                str(result.get("status", "unavailable"))
                for result in precise_references
            ],
            analysis_limits=assurance_analysis_limits,
            response_truncation=truncation,
            unresolved_risk_count=sum(
                int(report.get("unresolved_risk_count", 0)) for report in reports
            ),
            max_target_resolutions=max_results,
        )
        ordered_entrypoints = sorted(
            entrypoints.values(),
            key=lambda node: (node.get("kind") == "test_case", node["id"]),
        )
        compact_entrypoints = [
            {
                "id": node["id"],
                "kind": node.get("kind"),
                "category": "test" if node.get("kind") == "test_case" else "non_test",
            }
            for node in ordered_entrypoints[:max_results]
        ]
        entrypoint_summary = {
            "scope": "entrypoints found within traversal limits; non_test does not prove production use",
            "ordering": "non_test_first",
            "counts": {
                category: {
                    "total": sum(
                        (node.get("kind") == "test_case") == is_test
                        for node in ordered_entrypoints
                    ),
                    "returned": sum(
                        node["category"] == category for node in compact_entrypoints
                    ),
                }
                for category, is_test in (("non_test", False), ("test", True))
            },
        }
        for counts in entrypoint_summary["counts"].values():
            counts["omitted"] = counts["total"] - counts["returned"]
        compact_resources = _compact_impact_nodes(resources.values(), max_results)
        compact_symbols = _compact_impact_nodes(affected_symbols.values(), max_results)
        compact_modules = _compact_impact_nodes(affected_modules.values(), max_results)
        compact_tests = [
            _compact_test(item) for item in _limited(tests.values(), max_results)
        ]
        compact_similar = _limited(similar_implementations.values(), max_results)
        unknowns = _unknowns(assurance)
        recommended_next_reads = _recommended_next_reads(
            reports=reports,
            affected_symbols=_compact_nodes(affected_symbols.values(), max_results),
            tests=compact_tests,
            similar=compact_similar,
            max_results=max_results,
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "index_version": current.get("index_version"),
            "status": (
                "partial"
                if freshness.get("stale")
                or unresolved_targets
                or not query_targets
                or traversal_truncated
                else "available"
            ),
            "targets": normalized_targets,
            "changed_files": normalized_changed_files,
            "max_depth": max_depth,
            "max_results": max_results,
            "freshness": freshness,
            "recovery_action": stale_recovery_action(freshness),
            "capabilities": current.get("capabilities", {}),
            "blast_radius": {
                "entrypoints": compact_entrypoints,
                "resources": compact_resources,
                "affected_symbols": compact_symbols,
                "affected_modules": compact_modules,
            },
            "entrypoint_summary": entrypoint_summary,
            "test_candidates": compact_tests,
            "test_gaps": _limited(test_gaps.values(), max_results),
            # Risk factor kinds are a small closed set and are safety signals,
            # not examples.  Never let list ordering hide one from CI.
            "risk_factors": list(risk_factors.values()),
            "target_reports": _limited(reports, max_results),
            "similar_implementations": compact_similar,
            "unknowns": unknowns,
            "recommended_next_reads": recommended_next_reads,
            "precise_references": _limited(precise_references, max_results),
            "truncation": truncation,
            "assurance": assurance,
            "warnings": unique_values([*warnings, *compact_index_warnings]),
        }
        payload = apply_target_payload_contract(payload)
        payload["estimated_tokens"] = estimated_tokens(payload)
        return RiskReport.model_validate(payload).model_dump(
            mode="json", exclude_none=True
        )

    def _verify_target_references(
        self,
        target: str,
        resolved_ids: list[Any],
        *,
        max_results: int,
        symbol_budget: int,
    ) -> dict[str, Any]:
        """Verify references through the ids impact already resolved.

        references() refuses path-shaped queries under its one-symbol
        policy, so a changed-file target must be verified via its resolved
        node ids, which resolve unambiguously by literal id. A file target
        resolves to every symbol it contains, and each verified symbol can
        cost one language-service subprocess, so verification is bounded
        twice: per target, and by the budget left for the whole request.
        A target that gets no budget still produces an entry — one that says
        it verified nothing — because assurance reads these statuses and an
        absent one would let it claim the index-wide capability instead.
        """
        ids = [item for item in resolved_ids if isinstance(item, str) and item]
        allowance = max(0, min(_REFERENCE_VERIFICATION_SYMBOL_CAP, symbol_budget))
        verified_ids = ids[:allowance]
        cap = min(max_results, 50)
        runs = [
            self.query_engine.references(node_id, max_results=cap)
            for node_id in verified_ids
        ]
        if len(runs) == 1:
            summary = dict(runs[0].get("summary", {}))
        else:
            summary = {
                "total": sum(
                    int(run.get("summary", {}).get("total", 0) or 0) for run in runs
                ),
                "truncated": any(
                    bool(run.get("summary", {}).get("truncated")) for run in runs
                ),
            }
        references: list[Any] = []
        warnings: list[str] = []
        backends = []
        status = "available" if runs else "partial"
        for run in runs:
            references.extend(run.get("references", []))
            warnings.extend(str(item) for item in run.get("warnings", []))
            backend = run.get("backend")
            if backend:
                backends.append(backend)
            if run.get("status") != "available":
                status = "partial"
        if len(references) > cap:
            references = references[:cap]
            summary["truncated"] = True
        summary["returned"] = len(references)
        skipped = len(ids) - len(verified_ids)
        if skipped:
            status = "partial"
            exhausted = (
                ""
                if allowance
                else " The request's verification budget was already spent."
            )
            warnings.append(
                f"Reference verification covered {len(verified_ids)} of "
                f"{len(ids)} resolved symbols for this target; the remaining "
                f"symbols are unverified.{exhausted}"
            )
        unique_backends = list(dict.fromkeys(backends))
        return {
            "target": target,
            "status": status,
            "backend": (
                unique_backends[0]
                if len(unique_backends) == 1
                else ("mixed" if unique_backends else None)
            ),
            "summary": summary,
            "references": references,
            "warnings": warnings,
            "verified_symbols": verified_ids,
            "unverified_symbol_count": skipped,
        }


def _compact_target_report(report: dict[str, Any], max_results: int) -> dict[str, Any]:
    unresolved_risks = report.get("unresolved_risks", {})
    unresolved_summary = unresolved_risks.get("summary", {})
    compact = {
        "target": report["query"],
        "target_resolution": _compact_target_resolution(
            report.get("target_resolution", {}), max_results
        ),
        "resolved_targets": report.get("resolved_targets", [])[:max_results],
        "entrypoint_count": len(
            report.get("entrypoint_impact", {}).get("entrypoints", [])
        ),
        "resource_count": len(report.get("resource_impact", {}).get("resources", [])),
        "affected_symbol_count": len(
            report.get("call_impact", {}).get("affected_symbols", [])
        ),
        "affected_module_count": len(
            report.get("import_impact", {}).get("affected_modules", [])
        ),
        "test_candidate_count": len(report.get("test_candidates", [])),
        "test_gap_count": len(report.get("test_gaps", [])),
        "unresolved_scope": {
            "selection": unresolved_risks.get("scope"),
            "counts": unresolved_summary.get("by_inclusion_scope", {}),
            "ordering": "target, affected_symbol, affected_module, same_file",
            "release_blocking_scope": "individual indexed diagnostic classification; not a release verdict for this target or change",
        },
    }
    unresolved_count = int(unresolved_summary.get("total", 0) or 0)
    if unresolved_count:
        compact["unresolved_risk_count"] = unresolved_count
        compact["unresolved_risks"] = [
            compact_unresolved_record(item)
            for item in unresolved_risks.get("items", [])[:max_results]
            if isinstance(item, dict)
        ]
    analysis_limits = report.get("analysis_limits", {})
    compact["analysis_limits"] = {
        name: {
            key: value
            for key, value in section.items()
            if key in {"truncated", "reasons"}
        }
        for name, section in analysis_limits.items()
        if isinstance(section, dict)
    }
    return compact


def _bounded(value: int, *, default: int, upper: int) -> int:
    if value <= 0:
        return default
    return min(value, upper)


def _limited(values: Any, max_results: int) -> list[Any]:
    return list(values)[:max_results]


def _compact_target_resolution(
    value: dict[str, Any], max_results: int
) -> dict[str, Any]:
    bounded = bounded_target_resolution(value, max_results)
    compact = {
        "query": value.get("query"),
        "status": value.get("status"),
        "strategy": value.get("strategy"),
        "resolved_ids": bounded["resolved_ids"],
        "resolved_id_summary": bounded["resolved_id_summary"],
    }
    candidates = value.get("candidates", [])
    if candidates:
        compact["candidates"] = [
            _compact_node(item) for item in candidates if isinstance(item, dict)
        ]
        compact["candidates_truncated"] = bool(value.get("candidates_truncated"))
    suggestions = value.get("suggestions", [])
    if suggestions:
        compact["suggestions"] = [
            _compact_node(item) for item in suggestions if isinstance(item, dict)
        ]
    return compact


def _compact_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        key: node[key]
        for key in ("id", "kind", "name", "qualname", "path", "start_line", "end_line")
        if node.get(key) is not None
    }


def _compact_nodes(values: Any, max_results: int) -> list[dict[str, Any]]:
    return [_compact_node(node) for node in _limited(values, max_results)]


def _compact_impact_nodes(values: Any, max_results: int) -> list[dict[str, Any]]:
    return [
        {"id": node["id"]}
        for node in _limited(values, max_results)
        if node.get("id") is not None
    ]


def _compact_test(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in ("path", "reason", "score", "evidence")
        if item.get(key) is not None
    }


def _compact_preflight_index_warnings(
    values: list[Any],
    *,
    scope_known: bool,
    max_results: int,
    priority_paths: set[str] | None = None,
) -> tuple[list[Any], int]:
    """Bound index diagnostics without inventing target attribution.

    An unresolved target makes it unsafe to call path-scoped warnings
    unrelated. Returning every path diagnostic is also unsafe because a noisy
    repository can consume the whole response budget. Preserve index-wide
    caveats verbatim, sample path-scoped details up to ``max_results``, and
    disclose exact counts for what the caller must inspect through ``current``.
    """

    warnings = unique_values(values)
    if scope_known or len(warnings) <= max_results:
        return warnings, 0

    # Pathless warnings are index-wide capability/safety caveats.  They remain
    # verbatim even when this exceeds the requested sample size; dropping them
    # would change the meaning of the response.  Path-scoped parse failures are
    # sampled first because they often explain why a path target did not resolve.
    safety_warnings = [item for item in warnings if not _warning_path(item)]
    diagnostics = [item for item in warnings if _warning_path(item)]
    prioritized = sorted(
        diagnostics,
        key=lambda item: _warning_priority(item, priority_paths or set()),
    )
    sample_limit = max(0, max_results - len(safety_warnings))
    sampled = prioritized[:sample_limit]
    omitted = prioritized[sample_limit:]
    if not omitted:
        return [*safety_warnings, *sampled], 0
    counts = Counter(_warning_kind(item) for item in omitted)
    return (
        [
            *safety_warnings,
            *sampled,
            {
                "kind": "index_warnings_unscoped_summary",
                "message": (
                    f"Target scope is unresolved; {len(omitted)} path-scoped "
                    "index warning detail(s) were omitted without claiming that "
                    "they are unrelated."
                ),
                "total": len(omitted),
                "counts_by_kind": dict(sorted(counts.items())),
                "next_command": "arcgraph current",
            },
        ],
        len(omitted),
    )


def _warning_kind(value: Any) -> str:
    if isinstance(value, dict):
        kind = value.get("kind")
        if isinstance(kind, str) and kind:
            return kind
    return "unstructured"


def _warning_path(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    return path if isinstance(path, str) and path else None


def _warning_priority(value: Any, priority_paths: set[str]) -> tuple[int, int, str]:
    kind = _warning_kind(value)
    important = "parse" in kind or "syntax" in kind or "error" in kind
    requested_path = _warning_path(value) in priority_paths
    return (0 if requested_path else 1, 0 if important else 1, kind)


def _unknowns(assurance: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = assurance.get("evidence", {})
    limits = assurance.get("limits", {})
    unknowns: list[dict[str, Any]] = []
    if not reference_evidence_available(
        evidence.get("precision"),
        evidence.get("target_reference_verification"),
    ):
        unknowns.append(
            {
                "kind": "exact_references",
                "status": "not_established",
                # Not a standing limit: one caller-settable flag retires it,
                # so the value names that parameter rather than the internal
                # evidence field a caller cannot act on.
                "class": "evidence_gap",
                "resolvable_by": ["verify_references"],
            }
        )
    if not evidence_available(evidence.get("coverage")):
        unknowns.append(
            {
                "kind": "test_coverage",
                "status": "not_established",
                "class": "evidence_gap",
                "resolvable_by": ["coverage_evidence_for_target"],
            }
        )
    # Mirrors build_assurance: an ingested trace is an index-level input, so
    # the target's own runtime-derived edges are what retire this unknown.
    if not (
        evidence_available(evidence.get("runtime_trace"))
        and int(
            evidence.get("impact_edges_by_confidence", {}).get("runtime-only", 0) or 0
        )
        > 0
    ):
        unknowns.append(
            {
                "kind": "dynamic_runtime",
                "status": "not_established",
                # Same shape as test_coverage: both are ingestable evidence
                # artifacts, so they cannot carry different classes.
                "class": "evidence_gap",
                "resolvable_by": ["runtime_trace_with_target_evidence"],
            }
        )
    if any(
        limits.get(key) is True
        for key in ("traversal_truncated", "analysis_data_truncated")
    ):
        unknowns.append(
            {
                "kind": "analysis_truncated_scope",
                "status": "not_established",
                "class": "analysis_limit",
                "resolvable_by": ["narrower_target", "direct_follow_up_queries"],
            }
        )
    if limits.get("response_truncated") is True:
        unknowns.append(
            {
                "kind": "truncated_scope",
                "status": "not_established",
                "class": "response_limit",
                "resolvable_by": ["narrower_target", "higher_max_results"],
            }
        )
    return unknowns


def _recommended_next_reads(
    *,
    reports: list[dict[str, Any]],
    affected_symbols: list[dict[str, Any]],
    tests: list[dict[str, Any]],
    similar: list[dict[str, Any]],
    max_results: int,
) -> list[dict[str, Any]]:
    reads: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()

    def add(path: Any, line: Any, reason: str) -> None:
        if not isinstance(path, str) or not path:
            return
        normalized_line = line if isinstance(line, int) else None
        key = (path, normalized_line)
        if key in seen or len(reads) >= max_results:
            return
        seen.add(key)
        reads.append({"path": path, "line": normalized_line, "reason": reason})

    for report in reports:
        resolution = report.get("target_resolution", {})
        for key, reason in (
            ("candidates", "ambiguous target candidate"),
            ("suggestions", "unresolved target suggestion"),
        ):
            for candidate in resolution.get(key, []):
                add(candidate.get("path"), candidate.get("start_line"), reason)
    for node in affected_symbols:
        add(node.get("path"), node.get("start_line"), "affected caller")
    for item in tests:
        add(item.get("path"), None, "associated test")
    for node in similar:
        add(node.get("path"), node.get("start_line"), "similar implementation")
    return reads


def _risk_truncation(
    collections: dict[str, Any],
    *,
    reports: list[dict[str, Any]],
    precise_references: list[dict[str, Any]],
    max_results: int,
    warning_details_omitted: int = 0,
) -> dict[str, Any]:
    truncated_counts = {
        name: len(values) - max_results
        for name, values in collections.items()
        if len(values) > max_results
    }
    nested_unresolved = sum(
        max(
            int(report.get("unresolved_risk_count", 0))
            - len(report.get("unresolved_risks", [])),
            0,
        )
        for report in reports[:max_results]
    )
    if nested_unresolved:
        truncated_counts["target_reports.unresolved_risks"] = nested_unresolved
    # Bounding the nested id lists is a presentation omission like any other:
    # it has to reach truncation, or assurance reports the response as
    # complete while the payload dropped ids.
    omitted_ids = sum(
        int(
            report.get("target_resolution", {})
            .get("resolved_id_summary", {})
            .get("omitted", 0)
            or 0
        )
        for report in reports[:max_results]
    )
    if omitted_ids:
        truncated_counts["target_reports.target_resolution.resolved_ids"] = omitted_ids
    # Entries beyond max_results are dropped whole, so every reference they
    # held is omitted, not just the part each entry itself trimmed.
    kept_entries = precise_references[:max_results]
    dropped_entries = precise_references[max_results:]
    if dropped_entries:
        truncated_counts["precise_references"] = len(dropped_entries)
    precise_reference_omitted = sum(
        max(
            int(item.get("summary", {}).get("total", 0) or 0)
            - int(item.get("summary", {}).get("returned", 0) or 0),
            0,
        )
        for item in kept_entries
    ) + sum(
        int(item.get("summary", {}).get("total", 0) or 0) for item in dropped_entries
    )
    if precise_reference_omitted:
        truncated_counts["precise_references.references"] = precise_reference_omitted
    if warning_details_omitted:
        truncated_counts["warnings.details"] = warning_details_omitted
    return {
        "truncated": bool(truncated_counts),
        "scope": "response_presentation",
        "reason": "max_results" if truncated_counts else None,
        "max_results": max_results,
        "truncated_counts": truncated_counts,
    }


def _report_paths(*node_groups: Any) -> set[str]:
    """Repository paths the risk report actually names."""
    paths: set[str] = set()
    for group in node_groups:
        values = group.values() if isinstance(group, dict) else group
        for node in values or []:
            if isinstance(node, dict) and isinstance(node.get("path"), str):
                paths.add(node["path"])
    return paths
