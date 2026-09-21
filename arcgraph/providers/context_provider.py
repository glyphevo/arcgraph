"""Agent-facing context assembly built on the read-only query engine."""

from __future__ import annotations

import json
from typing import Any

from arcgraph.core.assurance import build_assurance, target_coverage_status
from arcgraph.core.cleanup import arcgraph_output_storage_status
from arcgraph.core.payload_policy import (
    bounded_target_resolution,
    apply_target_payload_contract,
    normalize_targets,
    resolved_definition_paths,
    scope_index_warnings,
    scoped_warning_inputs,
    unique_values,
)
from arcgraph.core.query_engine import CALL_QUERY_EDGE_KINDS, QueryEngine
from arcgraph.core.recovery import stale_recovery_action
from arcgraph.core.schemas import (
    ContextRequest,
    ContextResponse,
    ExplainResponse,
    SCHEMA_VERSION,
)
from arcgraph.core.target_resolver import is_entrypoint_target
from arcgraph.core.unresolved_classification import compact_unresolved_record
from arcgraph.core.utils import estimated_tokens, strip_source_snippets
from arcgraph.version_info import version_info
from arcgraph.providers.flow_payload import compact_flow
from arcgraph.providers.relationship_payload import (
    compact_relations,
    bound_explain_relations,
)


class ContextProvider:
    def __init__(self, query_engine: QueryEngine) -> None:
        self.query_engine = query_engine

    def index_status(self) -> dict[str, Any]:
        current = self.query_engine.current()
        stats = self.query_engine.stats()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "available",
            "index_version": current.get("index_version"),
            "commit_sha": current.get("commit_sha"),
            "created_at": current.get("created_at"),
            "freshness": current["freshness"],
            "capabilities": current.get("capabilities", {}),
            "counts": stats.get("counts", {}),
            "node_kinds": stats.get("node_kinds", {}),
            "edge_kinds": stats.get("edge_kinds", {}),
            "build_identity": version_info(),
            "storage": arcgraph_output_storage_status(self.query_engine.output_dir),
            "warnings": current.get("warnings", []),
        }
        recovery_action = stale_recovery_action(current["freshness"])
        if recovery_action is not None:
            payload["recovery_action"] = recovery_action
        return payload

    def get_context(self, request: ContextRequest) -> dict[str, Any]:
        targets = normalize_targets(request.targets)
        current = self.query_engine.current()
        max_results = _bounded(request.max_results, default=30, upper=100)
        context_limit = _context_limit(request.detail_level, max_results)
        # Index-level warnings are scoped in at the end, once the payload knows
        # which files it actually covers.
        warnings: list[Any] = []
        if not targets:
            warnings.append(
                "No targets were provided; returning index-level context only."
            )

        symbols: dict[str, dict[str, Any]] = {}
        entrypoints: dict[str, dict[str, Any]] = {}
        resources: dict[str, dict[str, Any]] = {}
        edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        tests: dict[str, dict[str, Any]] = {}
        test_gaps: dict[str, dict[str, Any]] = {}
        recommended_reads: dict[str, str] = {}
        grouped_effects: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {
            "confirmed": {},
            "inferred": {},
            "runtime_only": {},
            "heuristic": {},
            "unresolved": {},
        }
        unresolved_risks: dict[str, dict[str, Any]] = {}
        impact_reports: list[dict[str, Any]] = []
        entrypoint_flows: list[dict[str, Any]] = []
        unresolved_targets = False

        for target in targets:
            symbol_result = self.query_engine.symbol(target)
            for node in symbol_result["matches"]:
                _add_node(symbols, node)
                _add_read(recommended_reads, node, reason="target definition")

            if _is_entrypoint_target(target):
                flow = self.entrypoint_flow(target, max_results=max_results)
                entrypoint_flows.append(_compact_flow(flow, max_results))
                for node in flow.get("entrypoints", []):
                    _add_node(entrypoints, node)
                    _add_node(symbols, node)
                    _add_read(recommended_reads, node, reason="entrypoint flow")
                for node in flow.get("nodes", []):
                    _add_node(symbols, node)
                    if node.get("kind") in {"table", "queue"}:
                        _add_node(resources, node)
                        _add_read(recommended_reads, node, reason="data resource flow")
                    else:
                        _add_read(recommended_reads, node, reason="entrypoint flow")
                for edge in flow.get("edges", []):
                    _add_edge(edges, edge)
                warnings.extend(flow.get("warnings", []))
                # This branch returns early, so it has to record an unresolved
                # entrypoint itself. Without this an unmatched route folded the
                # index warnings away while claiming nothing went unresolved.
                if flow.get("status") == "unavailable" or not flow.get("entrypoints"):
                    unresolved_targets = True
                continue

            impact = self.query_engine.impact(target, profile=request.profile)
            impact_reports.append(_compact_impact(impact, context_limit))
            warnings.extend(impact.get("warnings", []))

            for resolved_target in impact.get("resolved_targets", [])[:max_results]:
                for node in self.query_engine.symbol(resolved_target)["matches"]:
                    _add_node(symbols, node)
                    _add_read(recommended_reads, node, reason="target definition")

            for node in impact.get("call_impact", {}).get("affected_symbols", []):
                _add_node(symbols, node)
                _add_read(
                    recommended_reads, node, reason="direct caller or affected symbol"
                )
            for node in impact.get("import_impact", {}).get("affected_modules", []):
                _add_node(symbols, node)
                _add_read(recommended_reads, node, reason="inferred module impact")
            for node in impact.get("entrypoint_impact", {}).get("entrypoints", []):
                _add_node(entrypoints, node)
                _add_node(symbols, node)
                _add_read(recommended_reads, node, reason="entrypoint impact")
            for node in impact.get("resource_impact", {}).get("resources", []):
                _add_node(resources, node)
                _add_node(symbols, node)
                _add_read(recommended_reads, node, reason="data resource impact")

            for section in [
                "call_impact",
                "import_impact",
                "entrypoint_impact",
                "resource_impact",
            ]:
                for edge in impact.get(section, {}).get("edges", []):
                    _add_edge(edges, edge)
            for confidence, bucket in [
                ("confirmed", "confirmed_impact"),
                ("inferred", "inferred_impact"),
                ("runtime_only", "runtime_impact"),
                ("heuristic", "heuristic_impact"),
                ("unresolved", "unresolved_impact"),
            ]:
                for edge in impact.get(bucket, []):
                    _add_edge(grouped_effects[confidence], edge)

            for diagnostic in impact.get("unresolved_risks", {}).get("items", []):
                key = diagnostic.get("diagnostic_id") or json.dumps(
                    diagnostic, sort_keys=True
                )
                unresolved_risks.setdefault(str(key), diagnostic)
                path = diagnostic.get("path") or diagnostic.get("properties", {}).get(
                    "path"
                )
                _add_read_path(
                    recommended_reads, path, reason="unresolved risk evidence"
                )

            for candidate in impact.get("test_candidates", []):
                tests.setdefault(candidate["path"], candidate)
                recommended_reads.setdefault(
                    candidate["path"], "heuristic test candidate"
                )
            for gap in impact.get("test_gaps", []):
                key = str(gap.get("target") or gap.get("reason"))
                test_gaps.setdefault(key, gap)

            if not impact.get("resolved_targets"):
                warnings.append(f"No graph node resolved for target {target!r}.")
                unresolved_targets = True

        freshness = current["freshness"]
        traversal_truncated = any(
            report.get("analysis_limits", {}).get("traversal", {}).get("truncated")
            is True
            for report in [*impact_reports, *entrypoint_flows]
        )
        status = (
            "partial"
            if freshness.get("stale")
            or unresolved_targets
            or not targets
            or traversal_truncated
            else "available"
        )
        truncation = _truncation(
            {
                "symbols": symbols,
                "entrypoints": entrypoints,
                "resources": resources,
                "edges": edges,
                "unresolved_risks": unresolved_risks,
                "impact": impact_reports,
                "entrypoint_flows": entrypoint_flows,
                "test_candidates": tests,
                "test_gaps": test_gaps,
                "recommended_next_reads": recommended_reads,
            },
            max_results=max_results,
            context_limit=context_limit,
        )
        _add_resolved_id_truncation(truncation, [*impact_reports, *entrypoint_flows])
        if any(
            flow.get("truncation", {}).get("truncated") for flow in entrypoint_flows
        ):
            truncation["truncated"] = True
            truncation["entrypoint_flow_truncated"] = True
        assurance = build_assurance(
            current=current,
            target_resolutions=[
                report.get("target_resolution", {})
                for report in [*impact_reports, *entrypoint_flows]
            ],
            confidence_summaries=[
                report.get("confidence_summary", {}) for report in impact_reports
            ],
            coverage_statuses=[
                str(report.get("coverage_status", "unavailable"))
                for report in impact_reports
            ],
            analysis_limits=[
                report.get("analysis_limits", {})
                for report in [*impact_reports, *entrypoint_flows]
            ],
            response_truncation=truncation,
            unresolved_risk_count=len(unresolved_risks),
            max_target_resolutions=context_limit,
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "index_version": current.get("index_version"),
            "status": status,
            "task": request.task,
            "targets": targets,
            "detail_level": request.detail_level,
            "confidence_profile": request.profile,
            "max_results": max_results,
            "freshness": freshness,
            "recovery_action": stale_recovery_action(freshness),
            "capabilities": current.get("capabilities", {}),
            "symbols": _summarize_nodes(symbols.values(), context_limit),
            "entrypoints": _summarize_nodes(entrypoints.values(), context_limit),
            "resources": _summarize_nodes(resources.values(), context_limit),
            "edges": _summarize_edges(edges.values(), context_limit),
            "grouped_effects": {
                confidence: _summarize_edges(effect_edges.values(), context_limit)
                for confidence, effect_edges in grouped_effects.items()
            },
            "unresolved_risks": _summarize_diagnostics(
                unresolved_risks.values(), context_limit
            ),
            "impact": _limited(impact_reports, context_limit),
            "entrypoint_flows": _limited(entrypoint_flows, context_limit),
            "test_candidates": _summarize_test_candidates(
                tests.values(), context_limit
            ),
            "test_gaps": _summarize_test_gaps(test_gaps.values(), context_limit),
            "recommended_next_reads": _ordered_recommended_reads(
                recommended_reads, max_results=context_limit
            ),
            "truncation": truncation,
            "assurance": assurance,
            "warnings": unique_values(
                [
                    *warnings,
                    *scope_index_warnings(
                        current.get("warnings", []),
                        set(recommended_reads),
                        fold=bool(targets) and not unresolved_targets,
                    ),
                ]
            ),
        }
        payload["source_snippets"] = {
            "requested": request.include_source,
            "enabled": request.include_source,
        }
        if not request.include_source:
            payload = strip_source_snippets(payload)
        payload = apply_target_payload_contract(payload)
        payload["estimated_tokens"] = estimated_tokens(payload)
        return ContextResponse.model_validate(payload).model_dump(
            mode="json", exclude_none=True
        )

    def explain(self, request: ContextRequest) -> dict[str, Any]:
        targets = normalize_targets(request.targets)
        current = self.query_engine.current()
        max_results = _bounded(request.max_results, default=30, upper=100)
        context_limit = _context_limit(request.detail_level, max_results)
        evidence_limit = _evidence_limit(request.detail_level)
        warnings: list[Any] = []
        unresolved_targets = False
        recommended_reads: dict[str, str] = {}
        raw_explanations: list[dict[str, Any]] = []
        all_incoming_edges: list[dict[str, Any]] = []
        all_outgoing_edges: list[dict[str, Any]] = []
        all_unresolved: list[dict[str, Any]] = []
        total_evidence_truncated = 0

        for target in targets:
            symbol_result = self.query_engine.symbol(target)
            callers = self.query_engine.callers(target, profile=request.profile)
            callees = self.query_engine.callees(target, profile=request.profile)
            unresolved = self.query_engine.unresolved(target, limit=max_results)
            relations = compact_relations(
                self.query_engine.relations(target, profile=request.profile),
                context_limit,
            )

            resolved_targets = unique_values(
                [
                    *[node["id"] for node in symbol_result.get("matches", [])],
                    *callers.get("resolved_targets", []),
                    *callees.get("resolved_targets", []),
                ]
            )
            symbols = list(symbol_result.get("matches", []))
            seen_symbol_ids = {node["id"] for node in symbols if node.get("id")}
            for resolved_target in resolved_targets:
                if resolved_target in seen_symbol_ids:
                    continue
                for node in self.query_engine.symbol(resolved_target).get(
                    "matches", []
                ):
                    symbols.append(node)
                    seen_symbol_ids.add(node["id"])

            for node in symbols:
                _add_read(recommended_reads, node, reason="target definition")
            for node in callers.get("callers", []):
                _add_read(recommended_reads, node, reason="direct caller")
            for node in callees.get("callees", []):
                _add_read(recommended_reads, node, reason="direct callee")

            incoming_edges, incoming_evidence_truncated = _compact_explain_edges(
                callers.get("edges", []),
                max_results=context_limit,
                evidence_limit=evidence_limit,
            )
            outgoing_edges, outgoing_evidence_truncated = _compact_explain_edges(
                callees.get("edges", []),
                max_results=context_limit,
                evidence_limit=evidence_limit,
            )
            total_evidence_truncated += (
                incoming_evidence_truncated + outgoing_evidence_truncated
            )
            all_incoming_edges.extend(callers.get("edges", []))
            all_outgoing_edges.extend(callees.get("edges", []))

            for edge in [*incoming_edges, *outgoing_edges]:
                for evidence in edge.get("evidence", []):
                    _add_read_path(
                        recommended_reads,
                        evidence.get("path"),
                        reason="edge evidence",
                    )

            unresolved_items = unresolved.get("unresolved", [])
            all_unresolved.extend(unresolved_items)
            for item in unresolved_items:
                _add_read_path(
                    recommended_reads,
                    item.get("path") or item.get("properties", {}).get("path"),
                    reason="unresolved evidence",
                )

            target_warnings = unique_values(
                [
                    *symbol_result.get("warnings", []),
                    *callers.get("warnings", []),
                    *callees.get("warnings", []),
                    *unresolved.get("warnings", []),
                ]
            )
            if not resolved_targets:
                target_warnings.append(f"No graph node resolved for target {target!r}.")
                unresolved_targets = True

            raw_edges = [*callers.get("edges", []), *callees.get("edges", [])]
            explanation_truncation = _truncation(
                {
                    "symbols": symbols,
                    "callers": callers.get("callers", []),
                    "callees": callees.get("callees", []),
                    "incoming_edges": callers.get("edges", []),
                    "outgoing_edges": callees.get("edges", []),
                    "unresolved": unresolved_items,
                },
                max_results=max_results,
                context_limit=context_limit,
            )
            _add_evidence_truncation(
                explanation_truncation,
                incoming_evidence_truncated + outgoing_evidence_truncated,
            )
            raw_explanations.append(
                {
                    "target": target,
                    "status": "available" if resolved_targets else "partial",
                    "resolved_targets": resolved_targets[:context_limit],
                    "symbols": _summarize_nodes(symbols, context_limit),
                    "callers": _summarize_nodes(
                        callers.get("callers", []), context_limit
                    ),
                    "callees": _summarize_nodes(
                        callees.get("callees", []), context_limit
                    ),
                    "incoming_edges": incoming_edges,
                    "outgoing_edges": outgoing_edges,
                    "call_scope": {
                        "edge_kinds": CALL_QUERY_EDGE_KINDS,
                        "confidence_profile": request.profile,
                        "note": "callers/callees and incoming/outgoing_edges use these kinds; resource and queue edges are in relations",
                    },
                    "relations": relations,
                    "resolution_summary": _resolution_summary(raw_edges),
                    "confidence_summary": _confidence_summary(raw_edges),
                    "unresolved": {
                        "summary": unresolved.get("summary", {}),
                        "items": _summarize_diagnostics(
                            unresolved_items, context_limit
                        ),
                    },
                    "truncation": explanation_truncation,
                    "warnings": unique_values(target_warnings),
                }
            )
            warnings.extend(target_warnings)

        freshness = current["freshness"]
        status = (
            "partial"
            if freshness.get("stale")
            or any(item.get("status") == "partial" for item in raw_explanations)
            else "available"
        )
        truncation = _truncation(
            {
                "explanations": raw_explanations,
                "incoming_edges": all_incoming_edges,
                "outgoing_edges": all_outgoing_edges,
                "unresolved": all_unresolved,
                "recommended_next_reads": recommended_reads,
            },
            max_results=max_results,
            context_limit=context_limit,
        )
        _add_evidence_truncation(truncation, total_evidence_truncated)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "index_version": current.get("index_version"),
            "status": status,
            "task": request.task,
            "targets": targets,
            "detail_level": request.detail_level,
            "confidence_profile": request.profile,
            "max_results": max_results,
            "freshness": freshness,
            "recovery_action": stale_recovery_action(freshness),
            "capabilities": current.get("capabilities", {}),
            "explanations": _limited(raw_explanations, context_limit),
            "recommended_next_reads": _ordered_recommended_reads(
                recommended_reads, max_results=context_limit
            ),
            "truncation": truncation,
            "warnings": unique_values(
                [
                    *warnings,
                    *scope_index_warnings(
                        current.get("warnings", []),
                        set(recommended_reads),
                        fold=bool(targets) and not unresolved_targets,
                    ),
                ]
            ),
        }
        payload["source_snippets"] = {
            "requested": request.include_source,
            "enabled": request.include_source,
        }
        if not request.include_source:
            payload = strip_source_snippets(payload)
        payload = apply_target_payload_contract(payload)
        payload = bound_explain_relations(payload)
        payload["estimated_tokens"] = estimated_tokens(payload)
        return ExplainResponse.model_validate(payload).model_dump(
            mode="json", exclude_none=True
        )

    def entrypoint_flow(
        self, entrypoint: str, max_depth: int = 3, max_results: int = 50
    ) -> dict[str, Any]:
        normalized = normalize_targets([entrypoint], label="entrypoint", max_targets=1)[
            0
        ]
        return compact_flow(
            apply_target_payload_contract(
                self.query_engine.entrypoint_flow(
                    normalized, max_depth=max_depth, max_results=max_results
                )
            )
        )

    def find_similar(
        self, target: str, max_results: int = 10, detail_level: str = "summary"
    ) -> dict[str, Any]:
        normalized = normalize_targets([target], label="target", max_targets=1)[0]
        return apply_target_payload_contract(
            self.query_engine.similar(
                normalized,
                max_results=_bounded(max_results, default=10, upper=50),
                detail_level=detail_level,
            )
        )

    def compact_callers(self, target: str, request: ContextRequest) -> dict[str, Any]:
        target = normalize_targets([target], label="target", max_targets=1)[0]
        callers = self.query_engine.callers(target, profile=request.profile)
        return _compact_relation_payload(
            current=self.query_engine.current(),
            request=request,
            query=target,
            relation="callers",
            node_key="callers",
            result=callers,
            target_paths=resolved_definition_paths(
                self.query_engine, callers.get("resolved_targets")
            ),
        )

    def compact_callees(self, target: str, request: ContextRequest) -> dict[str, Any]:
        target = normalize_targets([target], label="target", max_targets=1)[0]
        callees = self.query_engine.callees(target, profile=request.profile)
        return _compact_relation_payload(
            current=self.query_engine.current(),
            request=request,
            query=target,
            relation="callees",
            node_key="callees",
            result=callees,
            target_paths=resolved_definition_paths(
                self.query_engine, callees.get("resolved_targets")
            ),
        )

    def compact_impact(
        self,
        target: str,
        request: ContextRequest,
        *,
        max_depth: int | None = None,
        include_edge_kinds: list[str] | None = None,
        exclude_edge_kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        target = normalize_targets([target], label="target", max_targets=1)[0]
        impact = self.query_engine.impact(
            target,
            max_depth=max_depth,
            profile=request.profile,
            include_edge_kinds=include_edge_kinds,
            exclude_edge_kinds=exclude_edge_kinds,
        )
        return _compact_impact_query_payload(
            current=self.query_engine.current(),
            request=request,
            query=target,
            impact=impact,
            target_paths=resolved_definition_paths(
                self.query_engine, impact.get("resolved_targets")
            ),
        )


def _bounded(value: int, *, default: int, upper: int) -> int:
    if value <= 0:
        return default
    return min(value, upper)


def _context_limit(detail_level: str, max_results: int) -> int:
    if detail_level == "summary":
        return min(max_results, 6)
    if detail_level == "standard":
        return min(max_results, 8)
    return max_results


def _evidence_limit(detail_level: str) -> int:
    if detail_level == "summary":
        return 2
    if detail_level == "standard":
        return 3
    return 5


def _add_node(target: dict[str, dict[str, Any]], node: dict[str, Any]) -> None:
    target.setdefault(node["id"], node)


def _add_edge(
    target: dict[tuple[str, str, str], dict[str, Any]], edge: dict[str, Any]
) -> None:
    target.setdefault((edge["source"], edge["target"], edge["kind"]), edge)


def _add_read(
    target: dict[str, str], node: dict[str, Any], *, reason: str | None = None
) -> None:
    path = node.get("path")
    if path:
        _add_read_path(target, path, reason=reason or f"contains {node['id']}")


def _add_read_path(target: dict[str, str], path: Any, *, reason: str) -> None:
    if not isinstance(path, str) or not path:
        return
    existing = target.get(path)
    if existing is None or _read_priority(reason) < _read_priority(existing):
        target[path] = reason


# The classifier lives beside parse_route_target so every surface shares one
# definition. A real def rather than an alias assignment: ArcGraph's own
# resolver cannot follow calls through an alias binding, and the unresolved
# callsite would trip the release gate.
def _is_entrypoint_target(target: str) -> bool:
    return is_entrypoint_target(target)


def _compact_impact(report: dict[str, Any], max_results: int) -> dict[str, Any]:
    unresolved_risks = report.get("unresolved_risks", {})
    call_symbols = report.get("call_impact", {}).get("affected_symbols", [])
    import_modules = report.get("import_impact", {}).get("affected_modules", [])
    entrypoints = report.get("entrypoint_impact", {}).get("entrypoints", [])
    resources = report.get("resource_impact", {}).get("resources", [])
    sample_limit = min(max_results, 5)
    return {
        "target": report["query"],
        "target_resolution": bounded_target_resolution(
            report.get("target_resolution", {}), max_results
        ),
        "resolved_targets": report.get("resolved_targets", [])[:max_results],
        "analysis_limits": report.get("analysis_limits", {}),
        "call_impact": {
            "affected_symbol_ids": _node_ids(call_symbols, max_results),
            "sample": _summarize_nodes(call_symbols, sample_limit),
            "edge_count": len(report.get("call_impact", {}).get("edges", [])),
        },
        "import_impact": {
            "affected_module_ids": _node_ids(import_modules, max_results),
            "sample": _summarize_nodes(import_modules, sample_limit),
            "edge_count": len(report.get("import_impact", {}).get("edges", [])),
        },
        "entrypoint_impact": {
            "entrypoint_ids": _node_ids(entrypoints, max_results),
            "sample": _summarize_nodes(entrypoints, sample_limit),
            "edge_count": len(report.get("entrypoint_impact", {}).get("edges", [])),
        },
        "resource_impact": {
            "resource_ids": _node_ids(resources, max_results),
            "sample": _summarize_nodes(resources, sample_limit),
            "edge_count": len(report.get("resource_impact", {}).get("edges", [])),
        },
        "test_candidate_count": len(report.get("test_candidates", [])),
        "test_gap_count": len(report.get("test_gaps", [])),
        "coverage_status": target_coverage_status(
            report.get("coverage", {}).get("status", "unavailable"),
            bool(report.get("test_gaps")),
        ),
        "confidence_profile": report.get("confidence_profile", "review_default"),
        "confidence_summary": report.get("confidence_summary", {}),
        "grouped_effect_counts": {
            "confirmed": len(report.get("confirmed_impact", [])),
            "inferred": len(report.get("inferred_impact", [])),
            "runtime_only": len(report.get("runtime_impact", [])),
            "heuristic": len(report.get("heuristic_impact", [])),
            "unresolved": len(report.get("unresolved_impact", [])),
        },
        "unresolved_risks": {
            "summary": unresolved_risks.get("summary", {}),
            "items": _summarize_diagnostics(
                unresolved_risks.get("items", []), max_results
            ),
        },
    }


def _compact_flow(report: dict[str, Any], max_results: int) -> dict[str, Any]:
    return {
        "query": report["query"],
        "status": report["status"],
        "target_resolution": bounded_target_resolution(
            report.get("target_resolution", {}), max_results
        ),
        "entrypoints": _summarize_nodes(report.get("entrypoints", []), max_results),
        "nodes": _summarize_nodes(report.get("nodes", []), max_results),
        "edge_count": len(report.get("edges", [])),
        "analysis_limits": report.get("analysis_limits", {}),
        "truncation": report.get("truncation", {}),
        "max_depth": report.get("max_depth"),
    }


#: Emitted when the definition-path lookup failed, so the payload cannot tell
#: which warnings concern its own target. Folding is disabled in that case.
def _compact_relation_payload(
    *,
    current: dict[str, Any],
    request: ContextRequest,
    query: str,
    relation: str,
    node_key: str,
    result: dict[str, Any],
    target_paths: set[str] | None = None,
) -> dict[str, Any]:
    max_results = _bounded(request.max_results, default=30, upper=100)
    context_limit = _context_limit(request.detail_level, max_results)
    evidence_limit = _evidence_limit(request.detail_level)
    nodes = result.get(node_key, [])
    raw_edges = result.get("edges", [])
    compact_edges, evidence_truncated = _compact_explain_edges(
        raw_edges,
        max_results=context_limit,
        evidence_limit=evidence_limit,
    )
    recommended_reads: dict[str, str] = {}
    for node in nodes:
        _add_read(recommended_reads, node, reason=f"direct {relation[:-1]}")
    _add_edge_evidence_reads(recommended_reads, compact_edges)

    warnings = unique_values(
        [
            *result.get("warnings", []),
            *scoped_warning_inputs(
                current.get("warnings", []),
                set(recommended_reads),
                target_paths,
                result.get("resolved_targets"),
            ),
        ]
    )
    freshness = current["freshness"]
    status = (
        "partial"
        if freshness.get("stale") or result.get("status") == "partial"
        else "available"
    )
    truncation = _truncation(
        {
            node_key: nodes,
            "edges": raw_edges,
            "recommended_next_reads": recommended_reads,
        },
        max_results=max_results,
        context_limit=context_limit,
    )
    _add_evidence_truncation(truncation, evidence_truncated)
    bounded_resolution = bounded_target_resolution(
        result.get("target_resolution", {}), max_results
    )
    _add_resolved_id_truncation(truncation, [{"target_resolution": bounded_resolution}])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "index_version": current.get("index_version"),
        "status": status,
        "query": query,
        "relation": relation,
        "target_resolution": bounded_resolution,
        "resolved_targets": result.get("resolved_targets", [])[:context_limit],
        "detail_level": request.detail_level,
        "confidence_profile": request.profile,
        "max_results": max_results,
        "freshness": freshness,
        **(
            {"recovery_action": result["recovery_action"]}
            if isinstance(result.get("recovery_action"), dict)
            else {}
        ),
        "capabilities": result.get("capabilities", current.get("capabilities", {})),
        node_key: _summarize_nodes(nodes, context_limit),
        "edges": compact_edges,
        "provenance_summary": {
            "resolution": _resolution_summary(raw_edges),
            "confidence": _confidence_summary(raw_edges),
        },
        "recommended_next_reads": _ordered_recommended_reads(
            recommended_reads, max_results=context_limit
        ),
        "source_snippets": {
            "requested": request.include_source,
            "enabled": request.include_source,
        },
        "truncation": truncation,
        "warnings": warnings,
    }
    if not request.include_source:
        payload = strip_source_snippets(payload)
    payload = apply_target_payload_contract(payload)
    payload["estimated_tokens"] = estimated_tokens(payload)
    return payload


def _compact_impact_query_payload(
    *,
    current: dict[str, Any],
    request: ContextRequest,
    query: str,
    impact: dict[str, Any],
    target_paths: set[str] | None = None,
) -> dict[str, Any]:
    max_results = _bounded(request.max_results, default=30, upper=100)
    context_limit = _context_limit(request.detail_level, max_results)
    evidence_limit = _evidence_limit(request.detail_level)
    recommended_reads: dict[str, str] = {}

    call_impact, call_truncated = _compact_impact_query_section(
        impact.get("call_impact", {}),
        node_key="affected_symbols",
        max_results=context_limit,
        evidence_limit=evidence_limit,
        recommended_reads=recommended_reads,
        read_reason="call impact",
    )
    import_impact, import_truncated = _compact_impact_query_section(
        impact.get("import_impact", {}),
        node_key="affected_modules",
        max_results=context_limit,
        evidence_limit=evidence_limit,
        recommended_reads=recommended_reads,
        read_reason="import impact",
    )
    entrypoint_impact, entrypoint_truncated = _compact_impact_query_section(
        impact.get("entrypoint_impact", {}),
        node_key="entrypoints",
        max_results=context_limit,
        evidence_limit=evidence_limit,
        recommended_reads=recommended_reads,
        read_reason="entrypoint impact",
    )
    resource_impact, resource_truncated = _compact_impact_query_section(
        impact.get("resource_impact", {}),
        node_key="resources",
        max_results=context_limit,
        evidence_limit=evidence_limit,
        recommended_reads=recommended_reads,
        read_reason="resource impact",
    )
    unresolved_risks = impact.get("unresolved_risks", {})
    unresolved_items = unresolved_risks.get("items", [])
    for item in unresolved_items:
        _add_read_path(
            recommended_reads,
            item.get("path") or item.get("properties", {}).get("path"),
            reason="unresolved risk evidence",
        )

    raw_edges = [
        *impact.get("call_impact", {}).get("edges", []),
        *impact.get("import_impact", {}).get("edges", []),
        *impact.get("entrypoint_impact", {}).get("edges", []),
        *impact.get("resource_impact", {}).get("edges", []),
    ]
    total_evidence_truncated = (
        call_truncated + import_truncated + entrypoint_truncated + resource_truncated
    )
    freshness = current["freshness"]
    status = (
        "partial"
        if freshness.get("stale") or impact.get("status") == "partial"
        else "available"
    )
    truncation = _truncation(
        {
            "call_edges": impact.get("call_impact", {}).get("edges", []),
            "call_nodes": impact.get("call_impact", {}).get("affected_symbols", []),
            "import_edges": impact.get("import_impact", {}).get("edges", []),
            "import_nodes": impact.get("import_impact", {}).get("affected_modules", []),
            "entrypoint_edges": impact.get("entrypoint_impact", {}).get("edges", []),
            "entrypoint_nodes": impact.get("entrypoint_impact", {}).get(
                "entrypoints", []
            ),
            "resource_edges": impact.get("resource_impact", {}).get("edges", []),
            "resource_nodes": impact.get("resource_impact", {}).get("resources", []),
            "test_candidates": impact.get("test_candidates", []),
            "test_gaps": impact.get("test_gaps", []),
            "unresolved_risks": unresolved_items,
            "recommended_next_reads": recommended_reads,
        },
        max_results=max_results,
        context_limit=context_limit,
    )
    _add_evidence_truncation(truncation, total_evidence_truncated)
    _add_resolved_id_truncation(
        truncation,
        [
            {
                "target_resolution": bounded_target_resolution(
                    impact.get("target_resolution", {}), max_results
                )
            }
        ],
    )
    assurance = build_assurance(
        current=current,
        target_resolutions=[impact.get("target_resolution", {})],
        confidence_summaries=[impact.get("confidence_summary", {})],
        coverage_statuses=[
            target_coverage_status(
                impact.get("coverage", {}).get("status", "unavailable"),
                bool(impact.get("test_gaps")),
            )
        ],
        analysis_limits=[impact.get("analysis_limits", {})],
        response_truncation=truncation,
        unresolved_risk_count=int(unresolved_risks.get("summary", {}).get("total", 0)),
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "index_version": current.get("index_version"),
        "status": status,
        "query": query,
        "target_resolution": bounded_target_resolution(
            impact.get("target_resolution", {}), max_results
        ),
        "resolved_targets": impact.get("resolved_targets", [])[:context_limit],
        "detail_level": request.detail_level,
        "confidence_profile": request.profile,
        "max_results": max_results,
        "freshness": freshness,
        **(
            {"recovery_action": impact["recovery_action"]}
            if isinstance(impact.get("recovery_action"), dict)
            else {}
        ),
        "capabilities": impact.get("capabilities", current.get("capabilities", {})),
        "call_impact": call_impact,
        "import_impact": import_impact,
        "entrypoint_impact": entrypoint_impact,
        "resource_impact": resource_impact,
        "test_candidates": _summarize_test_candidates(
            impact.get("test_candidates", []), context_limit
        ),
        "test_gaps": _summarize_test_gaps(impact.get("test_gaps", []), context_limit),
        "coverage": impact.get("coverage", {}),
        "edge_kind_filter": impact.get("edge_kind_filter", {}),
        "depth_warnings": impact.get("depth_warnings", []),
        "analysis_limits": impact.get("analysis_limits", {}),
        "confidence_summary": impact.get("confidence_summary", {}),
        "provenance_summary": {
            "resolution": _resolution_summary(raw_edges),
            "confidence": _confidence_summary(raw_edges),
        },
        "unresolved_risks": {
            "summary": unresolved_risks.get("summary", {}),
            "items": _summarize_diagnostics(unresolved_items, context_limit),
        },
        "recommended_next_reads": _ordered_recommended_reads(
            recommended_reads, max_results=context_limit
        ),
        "source_snippets": {
            "requested": request.include_source,
            "enabled": request.include_source,
        },
        "truncation": truncation,
        "assurance": assurance,
        "warnings": unique_values(
            [
                *impact.get("warnings", []),
                *scoped_warning_inputs(
                    current.get("warnings", []),
                    set(recommended_reads),
                    target_paths,
                    impact.get("resolved_targets"),
                ),
            ]
        ),
    }
    if not request.include_source:
        payload = strip_source_snippets(payload)
    payload = apply_target_payload_contract(payload)
    payload["estimated_tokens"] = estimated_tokens(payload)
    return payload


def _compact_impact_query_section(
    section: dict[str, Any],
    *,
    node_key: str,
    max_results: int,
    evidence_limit: int,
    recommended_reads: dict[str, str],
    read_reason: str,
) -> tuple[dict[str, Any], int]:
    nodes = section.get(node_key, [])
    raw_edges = section.get("edges", [])
    compact_edges, evidence_truncated = _compact_explain_edges(
        raw_edges,
        max_results=max_results,
        evidence_limit=evidence_limit,
    )
    for node in nodes:
        _add_read(recommended_reads, node, reason=read_reason)
    _add_edge_evidence_reads(recommended_reads, compact_edges)
    return (
        {
            node_key: _summarize_nodes(nodes, max_results),
            "edges": compact_edges,
            "node_count": len(nodes),
            "edge_count": len(raw_edges),
        },
        evidence_truncated,
    )


def _add_edge_evidence_reads(
    target: dict[str, str], edges: list[dict[str, Any]]
) -> None:
    for edge in edges:
        for evidence in edge.get("evidence", []):
            if isinstance(evidence, dict):
                _add_read_path(target, evidence.get("path"), reason="edge evidence")


def _limited(values: Any, max_results: int) -> list[Any]:
    return list(values)[:max_results]


def _summarize_nodes(values: Any, max_results: int) -> list[dict[str, Any]]:
    return QueryEngine._summarize_nodes(_limited(values, max_results))[:max_results]


def _summarize_edges(values: Any, max_results: int) -> list[dict[str, Any]]:
    return QueryEngine._summarize_edges(_limited(values, max_results))[:max_results]


def _compact_explain_edges(
    values: Any, *, max_results: int, evidence_limit: int
) -> tuple[list[dict[str, Any]], int]:
    compact_edges: list[dict[str, Any]] = []
    evidence_truncated = 0
    for edge in _limited(values, max_results):
        evidence = edge.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = []
        current_truncated = max(0, len(evidence) - evidence_limit)
        evidence_truncated += current_truncated
        compact_edges.append(
            {
                "source": edge.get("source"),
                "target": edge.get("target"),
                "kind": edge.get("kind"),
                "confidence": edge.get("confidence"),
                "semantic_role": edge.get("semantic_role"),
                "resolution": edge.get("resolution", {}),
                "confidence_sources": edge.get("confidence_sources", {}),
                "evidence": _limited(evidence, evidence_limit),
                "evidence_total": len(evidence),
                "evidence_truncated": current_truncated,
            }
        )
    return compact_edges, evidence_truncated


def _node_ids(values: Any, max_results: int) -> list[str]:
    return [
        item["id"]
        for item in _limited(values, max_results)
        if isinstance(item.get("id"), str) and item["id"]
    ]


def _summarize_diagnostics(values: Any, max_results: int) -> list[dict[str, Any]]:
    return [compact_unresolved_record(item) for item in _limited(values, max_results)]


def _resolution_summary(edges: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_strategy: dict[str, int] = {}
    by_fallback: dict[str, int] = {}
    for edge in edges:
        resolution = edge.get("resolution", {})
        if not isinstance(resolution, dict):
            continue
        status = str(resolution.get("status") or "unknown")
        strategy = str(resolution.get("strategy") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
        fallbacks = resolution.get("fallbacks", [])
        if isinstance(fallbacks, list):
            for fallback in fallbacks:
                key = str(fallback)
                by_fallback[key] = by_fallback.get(key, 0) + 1
    return {
        "edge_count": len(edges),
        "by_status": dict(sorted(by_status.items())),
        "by_strategy": dict(sorted(by_strategy.items())),
        "by_fallback": dict(sorted(by_fallback.items())),
    }


def _confidence_summary(edges: list[dict[str, Any]]) -> dict[str, Any]:
    by_confidence: dict[str, int] = {}
    by_evidence_kind: dict[str, int] = {}
    by_confidence_source: dict[str, int] = {}
    for edge in edges:
        confidence = str(edge.get("confidence") or "unknown")
        by_confidence[confidence] = by_confidence.get(confidence, 0) + 1
        evidence = edge.get("evidence", [])
        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, dict):
                    key = str(item.get("kind") or "unknown")
                    by_evidence_kind[key] = by_evidence_kind.get(key, 0) + 1
        confidence_sources = edge.get("confidence_sources", {})
        if isinstance(confidence_sources, dict):
            for source, kinds in confidence_sources.items():
                increment = len(kinds) if isinstance(kinds, list) else 1
                key = str(source)
                by_confidence_source[key] = by_confidence_source.get(key, 0) + increment
    return {
        "edge_count": len(edges),
        "by_confidence": dict(sorted(by_confidence.items())),
        "by_evidence_kind": dict(sorted(by_evidence_kind.items())),
        "by_confidence_source": dict(sorted(by_confidence_source.items())),
    }


def _summarize_test_candidates(values: Any, max_results: int) -> list[dict[str, Any]]:
    return [
        _compact_fields(item, ("path", "target", "reason", "evidence", "coverage"))
        for item in _limited(values, max_results)
    ]


def _summarize_test_gaps(values: Any, max_results: int) -> list[dict[str, Any]]:
    return [
        _compact_fields(item, ("target", "path", "reason", "severity", "coverage"))
        for item in _limited(values, max_results)
    ]


def _compact_fields(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: item[key] for key in keys if key in item and item[key] is not None}


def _ordered_recommended_reads(
    reads: dict[str, str], *, max_results: int
) -> list[dict[str, str]]:
    return [
        {"path": path, "reason": reason}
        for path, reason in sorted(
            reads.items(),
            key=lambda item: (_read_priority(item[1]), item[0]),
        )[:max_results]
    ]


def _read_priority(reason: str) -> int:
    lowered = reason.lower()
    if "entrypoint" in lowered:
        return 0
    if "direct caller" in lowered or "target definition" in lowered:
        return 1
    if "resource" in lowered:
        return 2
    if "test" in lowered:
        return 3
    if "unresolved" in lowered:
        return 4
    if "similar" in lowered:
        return 5
    if "inferred" in lowered:
        return 6
    return 7


def _truncation(
    collections: dict[str, Any], *, max_results: int, context_limit: int
) -> dict[str, Any]:
    truncated_counts = {
        name: len(values) - context_limit
        for name, values in collections.items()
        if len(values) > context_limit
    }
    reason = "context_limit" if context_limit < max_results else "max_results"
    return {
        "truncated": bool(truncated_counts),
        "scope": "response_presentation",
        "reason": reason if truncated_counts else None,
        "max_results": max_results,
        "context_limit": context_limit,
        "truncated_counts": truncated_counts,
    }


def _add_resolved_id_truncation(
    truncation: dict[str, Any], reports: list[dict[str, Any]]
) -> None:
    """Fold bounded resolution id lists into the response truncation.

    Bounding those lists is a presentation omission like any other; left out
    of truncation, assurance reports the response as complete while the
    payload dropped ids.
    """

    omitted = sum(
        int(
            report.get("target_resolution", {})
            .get("resolved_id_summary", {})
            .get("omitted", 0)
            or 0
        )
        for report in reports
        if isinstance(report, dict)
    )
    if not omitted:
        return
    counts = truncation.setdefault("truncated_counts", {})
    counts["target_resolution.resolved_ids"] = omitted
    truncation["truncated"] = True
    if truncation.get("reason") is None:
        truncation["reason"] = "max_results"


def _add_evidence_truncation(
    truncation: dict[str, Any], evidence_truncated: int
) -> None:
    if evidence_truncated <= 0:
        return
    truncation["truncated"] = True
    truncation["reason"] = truncation.get("reason") or "evidence_limit"
    truncation.setdefault("truncated_counts", {})["evidence_items"] = evidence_truncated
