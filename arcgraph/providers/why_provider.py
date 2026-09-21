"""Structural why provider with optional external memory context."""

from __future__ import annotations

from typing import Any

from arcgraph.core.payload_policy import (
    apply_target_payload_contract,
    normalize_targets,
    unique_values,
)
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.recovery import stale_recovery_action
from arcgraph.core.schemas import SCHEMA_VERSION, WhyReport
from arcgraph.providers.memory_connector import (
    MemoryConnectorError,
    ExternalMemoryConnector,
    MemoryWhyQuery,
    UnavailableExternalMemoryConnector,
    build_memory_candidate,
)


class WhyProvider:
    def __init__(
        self,
        query_engine: QueryEngine,
        external_memory: ExternalMemoryConnector | None = None,
    ) -> None:
        self.query_engine = query_engine
        self.external_memory = external_memory or UnavailableExternalMemoryConnector()

    def get_why(
        self,
        target: str,
        max_results: int = 10,
        *,
        repo_id: str = "default",
        task: str | None = None,
    ) -> dict[str, Any]:
        target = normalize_targets([target], label="target", max_targets=1)[0]
        context = self._structural_context(target, max_results=max_results)
        current = context["current"]
        impact = context["impact"]
        resolved_targets = context["resolved_targets"]
        structural_why = context["structural_why"]
        max_results = context["max_results"]
        external_memory = self._external_memory_context(
            repo_id=repo_id,
            target=target,
            task=task,
            resolved_targets=resolved_targets,
            paths=context["paths"],
            structural_why=structural_why,
            max_results=max_results,
        )

        payload = {
            "schema_version": SCHEMA_VERSION,
            "index_version": current.get("index_version"),
            "status": "available" if impact.get("resolved_targets") else "partial",
            "target": target,
            "task": task,
            "resolved_targets": resolved_targets,
            "freshness": current["freshness"],
            "recovery_action": stale_recovery_action(current["freshness"]),
            "capabilities": current.get("capabilities", {}),
            "structural_why": structural_why,
            "historical_decisions": external_memory.get("historical_decisions", []),
            "lessons_learned": external_memory.get("lessons_learned", []),
            "best_practices": external_memory.get("best_practices", []),
            "external_memory": external_memory,
            "warnings": unique_values(
                [
                    *impact.get("warnings", []),
                    *external_memory.get("warnings", []),
                ]
            ),
        }
        payload = apply_target_payload_contract(payload)
        return WhyReport.model_validate(payload).model_dump(
            mode="json", exclude_none=True
        )

    def propose_learning(
        self,
        *,
        repo_id: str,
        target: str,
        observation: str,
        memory_type: str = "lesson_learned",
        title: str | None = None,
    ) -> dict[str, Any]:
        target = normalize_targets([target], label="target", max_targets=1)[0]
        context = self._structural_context(target, max_results=5)
        current = context["current"]
        proposal_title = title or f"ArcGraph lesson for {target}"
        resolved_targets = context["resolved_targets"]
        paths = context["paths"]
        content = observation.strip() or self._default_learning_content(
            target=target,
            structural_why=context["structural_why"],
            paths=paths,
        )
        candidate = build_memory_candidate(
            title=proposal_title,
            content=content,
            memory_type=memory_type,
            repo_id=repo_id,
            target=target,
            resolved_targets=resolved_targets,
            paths=paths,
            index_version=current.get("index_version"),
            commit_sha=current.get("commit_sha"),
        )
        candidate_payload = candidate.model_dump(mode="json")
        return apply_target_payload_contract(
            {
                "schema_version": SCHEMA_VERSION,
                "index_version": current.get("index_version"),
                "status": "available",
                "mode": "proposal-only",
                "read_only": True,
                "freshness": current["freshness"],
                "capabilities": current.get("capabilities", {}),
                "memory_candidate": candidate_payload,
                "proposal": candidate_payload,
                "external_memory": {
                    "status": "candidate_only",
                    "reason": "ArcGraph generated an external memory candidate but did not persist it.",
                },
                "warnings": [
                    "This is a read-only proposal. Persist it through external memory proposal tools after review."
                ],
            }
        )

    def _structural_context(self, target: str, *, max_results: int) -> dict[str, Any]:
        current = self.query_engine.current()
        max_results = max(1, min(max_results, 50))
        impact = self.query_engine.impact(target, max_depth=2)
        callers = self.query_engine.callers(target)
        callees = self.query_engine.callees(target)
        architecture = self.query_engine.architecture()
        resolved_targets = impact.get("resolved_targets", [])[:max_results]
        paths = self._paths_for_targets(resolved_targets, max_results=max_results)
        structural_why = self._structural_reasons(
            target=target,
            current=current,
            impact=impact,
            callers=callers,
            callees=callees,
            test_candidates=impact.get("test_candidates", []),
            architecture=architecture,
            max_results=max_results,
        )
        return {
            "current": current,
            "max_results": max_results,
            "impact": impact,
            "resolved_targets": resolved_targets,
            "paths": paths,
            "structural_why": structural_why,
        }

    def _external_memory_context(
        self,
        *,
        repo_id: str,
        target: str,
        task: str | None,
        resolved_targets: list[str],
        paths: list[str],
        structural_why: list[dict[str, Any]],
        max_results: int,
    ) -> dict[str, Any]:
        query = MemoryWhyQuery(
            repo_id=repo_id,
            target=target,
            task=task,
            resolved_targets=resolved_targets,
            paths=paths,
            structural_why=structural_why,
            max_results=max_results,
        )
        try:
            context = self.external_memory.get_why_context(query)
            payload = context.model_dump(mode="json")
        except MemoryConnectorError as exc:
            context = UnavailableExternalMemoryConnector(str(exc)).get_why_context(
                query
            )
            payload = context.model_dump(mode="json")
        except Exception as exc:
            context = UnavailableExternalMemoryConnector(
                f"External memory connector failed: {exc}"
            ).get_why_context(query)
            payload = context.model_dump(mode="json")
        payload["memory_count"] = len(payload.get("memories", []))
        return payload

    def _paths_for_targets(
        self, target_ids: list[str], *, max_results: int
    ) -> list[str]:
        paths: dict[str, None] = {}
        for node in self.query_engine.nodes_by_ids(target_ids[:max_results]):
            path = node.get("path")
            if path:
                paths.setdefault(path, None)
        return list(paths)[:max_results]

    @staticmethod
    def _default_learning_content(
        *, target: str, structural_why: list[dict[str, Any]], paths: list[str]
    ) -> str:
        entrypoints = [
            item
            for reason in structural_why
            if reason.get("kind") == "entrypoint_reachability"
            for item in reason.get("evidence", [])
        ]
        resources = [
            item
            for reason in structural_why
            if reason.get("kind") == "resource_flow"
            for item in reason.get("evidence", [])
        ]
        parts = [
            f"When changing {target}, inspect its ArcGraph impact before editing.",
        ]
        if entrypoints:
            parts.append(
                "Relevant entrypoints include "
                + ", ".join(item.get("id", "") for item in entrypoints[:5] if item)
                + "."
            )
        if resources:
            parts.append(
                "Check resource effects on "
                + ", ".join(item.get("id", "") for item in resources[:5] if item)
                + "."
            )
        if paths:
            parts.append("Start with " + ", ".join(paths[:5]) + ".")
        return " ".join(parts)

    @staticmethod
    def _structural_reasons(
        *,
        target: str,
        current: dict[str, Any],
        impact: dict[str, Any],
        callers: dict[str, Any],
        callees: dict[str, Any],
        test_candidates: list[dict[str, Any]],
        architecture: dict[str, Any],
        max_results: int,
    ) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        freshness = current["freshness"]
        if freshness.get("stale"):
            reasons.append(
                {
                    "kind": "freshness",
                    "summary": "The ArcGraph index is stale for this repository.",
                    "evidence": {
                        "stale_files": freshness.get("stale_files", [])[:max_results]
                    },
                }
            )

        entrypoints = impact.get("entrypoint_impact", {}).get("entrypoints", [])
        if entrypoints:
            reasons.append(
                {
                    "kind": "entrypoint_reachability",
                    "summary": f"{target} is reachable from framework entrypoints.",
                    "evidence": entrypoints[:max_results],
                }
            )

        resources = impact.get("resource_impact", {}).get("resources", [])
        if resources:
            reasons.append(
                {
                    "kind": "resource_flow",
                    "summary": f"{target} touches indexed tables, queues, or resources.",
                    "evidence": resources[:max_results],
                }
            )

        caller_nodes = callers.get("callers", [])
        if caller_nodes:
            reasons.append(
                {
                    "kind": "fan_in",
                    "summary": f"{target} has direct callers in the static call graph.",
                    "evidence": caller_nodes[:max_results],
                }
            )

        callee_nodes = callees.get("callees", [])
        if callee_nodes:
            reasons.append(
                {
                    "kind": "fan_out",
                    "summary": f"{target} calls other indexed symbols.",
                    "evidence": callee_nodes[:max_results],
                }
            )

        if test_candidates:
            reasons.append(
                {
                    "kind": "test_surface",
                    "summary": "Heuristic test candidates are available, but coverage evidence is not.",
                    "evidence": test_candidates[:max_results],
                }
            )

        fan_in = [
            item
            for item in architecture.get("top_fan_in", [])
            if item.get("target") in impact.get("resolved_targets", [])
        ]
        fan_out = [
            item
            for item in architecture.get("top_fan_out", [])
            if item.get("source") in impact.get("resolved_targets", [])
        ]
        if fan_in or fan_out:
            reasons.append(
                {
                    "kind": "architecture_hotspot",
                    "summary": f"{target} appears in top fan-in/fan-out architecture statistics.",
                    "evidence": {"fan_in": fan_in, "fan_out": fan_out},
                }
            )

        if not impact.get("resolved_targets"):
            reasons.append(
                {
                    "kind": "unresolved_target",
                    "summary": f"{target} did not resolve to a graph node.",
                    "evidence": {},
                }
            )

        return reasons
