"""Native ArcGraph context orchestration for internal agents."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from arcgraph.core.payload_policy import (
    apply_target_payload_contract,
    normalize_target_groups,
    normalize_targets,
    unique_values,
)
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.schemas import (
    AgentContextPolicy,
    AgentContextRequest,
    AgentTaskContext,
    ContextRequest,
    ContextResponse,
)
from arcgraph.core.utils import estimated_tokens
from arcgraph.providers.context_provider import ContextProvider
from arcgraph.providers.risk_provider import RiskProvider
from arcgraph.providers.memory_connector import (
    MemoryConnectorError,
    ExternalMemoryConnector,
    MemoryWhyQuery,
    UnavailableExternalMemoryConnector,
)
from arcgraph.providers.why_provider import WhyProvider


class ArcGraphContextProvider:
    """Typed native provider facade over the shared read-only query engine."""

    def __init__(
        self,
        query_engine: QueryEngine,
        external_memory: ExternalMemoryConnector | None = None,
    ) -> None:
        self.query_engine = query_engine
        self.context = ContextProvider(query_engine)
        self.risk = RiskProvider(query_engine)
        self.why = WhyProvider(query_engine, external_memory=external_memory)

    def index_status(self) -> dict[str, Any]:
        return self.context.index_status()

    def get_context(self, request: ContextRequest) -> dict[str, Any]:
        payload = self.context.get_context(request)
        if request.detail_level == "detailed":
            payload["architecture"] = self.query_engine.architecture()
        return ContextResponse.model_validate(payload).model_dump(
            mode="json", exclude_none=True
        )

    def get_risk(
        self,
        *,
        targets: list[str],
        changed_files: list[str],
        max_results: int,
        verify_references: bool = False,
    ) -> dict[str, Any]:
        return self.risk.get_risk(
            targets=targets,
            changed_files=changed_files,
            max_results=max_results,
            verify_references=verify_references,
        )

    def get_why(
        self,
        *,
        repo_id: str,
        target: str,
        task: str,
        max_results: int,
    ) -> dict[str, Any]:
        return self.why.get_why(
            target,
            repo_id=repo_id,
            task=task,
            max_results=max_results,
        )

    def find_similar(
        self, targets: list[str], *, max_results: int, detail_level: str
    ) -> list[dict[str, Any]]:
        normalized_targets = normalize_targets(targets)
        max_results = _bounded(max_results, default=10, upper=100)
        return [
            self.context.find_similar(
                target,
                max_results=max_results,
                detail_level=detail_level,
            )
            for target in normalized_targets[:max_results]
        ]


class TestRecommendationProvider:
    """Collect test recommendations for native agent contexts."""

    def __init__(self, query_engine: QueryEngine) -> None:
        self.query_engine = query_engine

    def recommend(
        self, targets: list[str], *, max_results: int
    ) -> list[dict[str, Any]]:
        targets = normalize_targets(targets)
        recommendations: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for target in targets[:max_results]:
            payload = self.query_engine.tests(target)
            for candidate in payload.get("candidates", [])[:max_results]:
                key = (target, candidate.get("path", ""))
                if key in seen:
                    continue
                seen.add(key)
                recommendations.append(
                    {
                        "target": target,
                        "path": candidate.get("path"),
                        "reason": candidate.get("reason"),
                        "evidence": candidate.get("evidence"),
                        "coverage": payload.get("coverage", {}),
                        "test_gaps": payload.get("test_gaps", []),
                    }
                )
        return recommendations[:max_results]


class ExternalMemoryProvider:
    """Native memory provider around the external memory connector boundary."""

    def __init__(self, connector: ExternalMemoryConnector | None = None) -> None:
        self.connector = connector or UnavailableExternalMemoryConnector()

    def get_memory_context(
        self,
        *,
        repo_id: str,
        task: str,
        target: str,
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
            payload = self.connector.get_why_context(query).model_dump(mode="json")
        except MemoryConnectorError as exc:
            payload = (
                UnavailableExternalMemoryConnector(str(exc))
                .get_why_context(query)
                .model_dump(mode="json")
            )
        except Exception as exc:
            payload = (
                UnavailableExternalMemoryConnector(
                    f"External memory connector failed: {exc}"
                )
                .get_why_context(query)
                .model_dump(mode="json")
            )
        payload["memory_count"] = len(payload.get("memories", []))
        return payload


class GitDiffProvider:
    """Read-only Git diff summary provider for native agent contexts."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()

    def get_diff_context(
        self,
        *,
        changed_files: list[str] | None = None,
        max_diff_lines: int = 200,
    ) -> dict[str, Any]:
        warnings: list[str] = []
        files = changed_files or self.changed_files(warnings)
        diff_stat = self._git(["diff", "--stat", "HEAD", "--", *files], warnings)
        diff = self._git(["diff", "--unified=0", "HEAD", "--", *files], warnings)
        diff_lines = diff.splitlines()
        truncated = len(diff_lines) > max_diff_lines
        return {
            "status": "available" if not warnings else "partial",
            "changed_files": files,
            "diff_stat": diff_stat.strip(),
            "diff": "\n".join(diff_lines[:max_diff_lines]),
            "truncated": truncated,
            "warnings": warnings,
        }

    def changed_files(self, warnings: list[str] | None = None) -> list[str]:
        rows = self._git(
            ["status", "--porcelain", "--untracked-files=all"],
            warnings if warnings is not None else [],
        ).splitlines()
        files: set[str] = set()
        for row in rows:
            if not row:
                continue
            path = row[3:] if len(row) > 3 else ""
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[-1]
            if path:
                files.add(path.replace("\\", "/"))
        return sorted(files)

    def _git(self, args: list[str], warnings: list[str]) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.repo_root), *args],
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            warnings.append(f"git unavailable: {exc}")
            return ""
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            warnings.append(f"git {' '.join(args)} failed: {message}")
            return ""
        return completed.stdout


class AgentContextOrchestrator:
    """Build a merged structural, memory, test, similarity, and diff context."""

    def __init__(
        self,
        *,
        repo_root: Path,
        output_dir: Path,
        external_memory: ExternalMemoryConnector | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.output_dir = output_dir.resolve()
        self.query_engine = QueryEngine(self.output_dir)
        self.external_memory = external_memory or UnavailableExternalMemoryConnector()
        self.arcgraph = ArcGraphContextProvider(
            self.query_engine,
            external_memory=self.external_memory,
        )
        self.tests = TestRecommendationProvider(self.query_engine)
        self.memories = ExternalMemoryProvider(self.external_memory)
        self.git = GitDiffProvider(self.repo_root)

    async def build_task_context(
        self,
        *,
        task: str,
        repo_id: str = "default",
        targets: list[str] | None = None,
        changed_files: list[str] | None = None,
        policy: AgentContextPolicy = "coding_change",
        max_results: int = 30,
        detail_level: str = "summary",
        profile: str = "review_default",
    ) -> dict[str, Any]:
        request = AgentContextRequest(
            task=task,
            repo_id=repo_id,
            targets=targets or [],
            changed_files=changed_files or [],
            policy=policy,
            max_results=max_results,
            detail_level=detail_level,
            profile=profile,
        )
        return self._build_task_context(request).model_dump(mode="json")

    def _build_task_context(self, request: AgentContextRequest) -> AgentTaskContext:
        max_results = _bounded(request.max_results, default=30, upper=100)
        requested_targets, requested_changed_files, _requested_work = (
            normalize_target_groups(request.targets, request.changed_files)
        )
        git_diff = self.git.get_diff_context(
            changed_files=requested_changed_files,
            max_diff_lines=200 if request.detail_level == "summary" else 500,
        )
        changed_files = requested_changed_files or normalize_targets(
            git_diff.get("changed_files", []),
            label="changed_files",
        )
        _targets, _changed, context_targets = normalize_target_groups(
            requested_targets,
            changed_files,
        )
        primary_target = _primary_target(context_targets)

        index_status = self.arcgraph.index_status()
        structural_context = self.arcgraph.get_context(
            ContextRequest(
                task=request.task,
                targets=context_targets,
                max_results=max_results,
                detail_level=request.detail_level,
                profile=request.profile,
            )
        )
        if request.policy == "architecture_investigation":
            structural_context["architecture"] = self.query_engine.architecture()

        risk = self.arcgraph.get_risk(
            targets=requested_targets,
            changed_files=changed_files,
            max_results=max_results,
        )
        why = (
            self.arcgraph.get_why(
                repo_id=request.repo_id,
                target=primary_target,
                task=request.task,
                max_results=max_results,
            )
            if primary_target
            else {}
        )
        similar = self.arcgraph.find_similar(
            context_targets,
            max_results=max_results,
            detail_level=request.detail_level,
        )
        test_recommendations = self.tests.recommend(
            context_targets,
            max_results=max_results,
        )
        memory_context = (
            self.memories.get_memory_context(
                repo_id=request.repo_id,
                task=request.task,
                target=primary_target,
                resolved_targets=why.get("resolved_targets", []),
                paths=_paths_from_context(structural_context),
                structural_why=why.get("structural_why", []),
                max_results=max_results,
            )
            if primary_target
            else {"status": "unavailable", "memories": [], "memory_count": 0}
        )

        # ``index_status`` remains nested as the explicit whole-index view.
        # Top-level task warnings aggregate the already scoped target payloads
        # instead of copying the full index warning table a second time.
        warnings = unique_values(
            [
                *structural_context.get("warnings", []),
                *risk.get("warnings", []),
                *why.get("warnings", []),
                *git_diff.get("warnings", []),
                *memory_context.get("warnings", []),
            ]
        )
        freshness = index_status.get("freshness", {})
        status = _aggregate_status(
            freshness=freshness,
            index_status=index_status,
            structural_context=structural_context,
            risk=risk,
            why=why,
            similar=similar,
            git_diff=git_diff,
        )
        context = AgentTaskContext(
            status=status,
            repo_id=request.repo_id,
            task=request.task,
            policy=request.policy,
            targets=requested_targets,
            changed_files=changed_files,
            max_results=max_results,
            detail_level=request.detail_level,
            profile=request.profile,
            freshness=freshness,
            capabilities=index_status.get("capabilities", {}),
            index_status=index_status,
            structural_context=structural_context,
            risk=risk,
            why=why,
            similar_implementations=similar,
            test_recommendations=test_recommendations,
            git_diff=git_diff,
            memories=memory_context.get("memories", []),
            recommended_next_reads=structural_context.get("recommended_next_reads", []),
            estimated_tokens=structural_context.get("estimated_tokens", 0),
            truncation=structural_context.get("truncation", {}),
            warnings=warnings,
        )
        context = AgentTaskContext.model_validate(
            apply_target_payload_contract(context.model_dump(mode="python"))
        )
        context.estimated_tokens = estimated_tokens(context.model_dump(mode="json"))
        return context


def _bounded(value: int, *, default: int, upper: int) -> int:
    if value <= 0:
        return default
    return min(value, upper)


def _primary_target(targets: list[str]) -> str:
    return targets[0] if targets else ""


def _paths_from_context(context: dict[str, Any]) -> list[str]:
    paths: dict[str, None] = {}
    for key in ["symbols", "entrypoints", "resources"]:
        for node in context.get(key, []):
            path = node.get("path")
            if path:
                paths.setdefault(path, None)
    for item in context.get("recommended_next_reads", []):
        path = item.get("path")
        if path:
            paths.setdefault(path, None)
    return list(paths)


def _aggregate_status(
    *,
    freshness: dict[str, Any],
    index_status: dict[str, Any],
    structural_context: dict[str, Any],
    risk: dict[str, Any],
    why: dict[str, Any],
    similar: list[dict[str, Any]],
    git_diff: dict[str, Any],
) -> str:
    required_statuses = [
        index_status.get("status"),
        structural_context.get("status"),
        risk.get("status"),
    ]
    if why:
        required_statuses.append(why.get("status"))
    if any(item.get("status") == "unavailable" for item in similar):
        required_statuses.append("unavailable")

    if "unavailable" in required_statuses:
        return "unavailable"

    risk_kinds = {
        item.get("kind")
        for item in risk.get("risk_factors", [])
        if isinstance(item, dict)
    }
    if (
        freshness.get("stale")
        or "partial" in required_statuses
        or git_diff.get("status") == "partial"
        or "unresolved_target" in risk_kinds
        or any(item.get("status") == "partial" for item in similar)
    ):
        return "partial"

    return "available"
