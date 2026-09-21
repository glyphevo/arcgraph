"""Read-only MCP facade for external ArcGraph clients.

The facade owns protocol-shaped concerns: repo scope, path permissions, source
snippet policy, response trimming, and optional registration into an MCP server.
Core query behavior remains in providers and the query engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
from typing import Any

try:
    # The MCP runtime is an optional extra. When it is installed, anticipated
    # tool failures must derive from the SDK's ToolError so the server reports
    # them as expected errors instead of logging a crash traceback. The name is
    # always bound to a class so the exception below stays a single
    # module-level definition that static analysis can resolve.
    from mcp.server.mcpserver.exceptions import ToolError as _AnticipatedToolError
except ImportError:  # pragma: no cover - exercised without the MCP extra

    class _AnticipatedToolError(Exception):
        """Stand-in base used when the optional MCP runtime is absent."""


from arcgraph.change.contracts import (
    CHANGE_CONTRACT_VERSION,
    ChangeIntent,
    ChangeTarget,
    canonical_digest,
)
from arcgraph.change.errors import ChangeSafetyError
from arcgraph.change.evidence import redact_sensitive_text
from arcgraph.change.paths import is_any_os_absolute as _is_any_os_absolute
from arcgraph.change.service import ChangeSafetyService
from arcgraph.change.trusted_runners import TrustedRunnerRegistry
from arcgraph.change.verdicts import verdict_is_blocking
from arcgraph.interfaces.agent_capabilities import (
    ALL_MCP_CAPABILITY_NAMES,
    AgentCapabilitySpec,
    mcp_capability_specs,
)
from arcgraph.interfaces.agent_help import HelpTopic, build_agent_help
from arcgraph.interfaces.trial_feedback import (
    TrialFeedbackDisabledError,
    TrialFeedbackError,
    TrialFeedbackPayload,
    TrialFeedbackStore,
    feedback_error_payload,
    feedback_success_payload,
    validate_feedback_request,
)
from arcgraph.core.payload_policy import (
    MAX_TARGETS_PER_REQUEST,
    apply_target_payload_contract,
    normalize_targets,
    unique_values,
)
from arcgraph.core.query_engine import (
    QueryEngine,
    SchemaVersionError,
)
from arcgraph.core.schemas import (
    ContextRequest,
    DetailLevel,
    ContextResponse,
    ExplainResponse,
    RiskReport,
    SCHEMA_VERSION,
    WhyReport,
)
from arcgraph.core.target_resolver import parse_route_target
from arcgraph.core.utils import strip_source_snippets
from arcgraph.providers.flow_payload import bound_flow_payload
from arcgraph.providers.relationship_payload import bound_explain_relations
from arcgraph.providers.context_provider import ContextProvider
from arcgraph.providers.risk_provider import RiskProvider
from arcgraph.providers.memory_connector import ExternalMemoryConnector
from arcgraph.providers.why_provider import WhyProvider


class ArcGraphRequestError(_AnticipatedToolError, ValueError):
    """Raised for invalid request arguments without crashing the server."""


class ArcGraphPermissionError(_AnticipatedToolError, ValueError):
    """Raised when an MCP request references an unauthorized repo or path."""


@dataclass(slots=True)
class ArcGraphRepositoryConfig:
    root_path: str | Path
    output_dir: str | Path | None = None
    mode: str = "read_only"
    expose_source_snippets: bool = False


@dataclass(slots=True)
class ArcGraphMCPConfig:
    allowed_roots: list[str | Path]
    repositories: dict[str, ArcGraphRepositoryConfig]
    default_output_dir: str | Path = "output/arcgraph"
    max_results_limit: int = 100
    max_targets_limit: int = 100
    trusted_runner_registry_path: str | Path | None = None
    external_memory_connector: ExternalMemoryConnector | None = None
    feedback_store: TrialFeedbackStore | None = None

    @classmethod
    def for_single_repo(
        cls,
        repo_root: str | Path,
        *,
        repo_id: str = "default",
        output_dir: str | Path | None = None,
        allowed_roots: list[str | Path] | None = None,
        expose_source_snippets: bool = False,
        max_targets_limit: int = 100,
        trusted_runner_registry_path: str | Path | None = None,
        external_memory_connector: ExternalMemoryConnector | None = None,
        feedback_store: TrialFeedbackStore | None = None,
    ) -> "ArcGraphMCPConfig":
        root = Path(repo_root).resolve()
        return cls(
            allowed_roots=allowed_roots or [root],
            max_targets_limit=max_targets_limit,
            trusted_runner_registry_path=trusted_runner_registry_path,
            external_memory_connector=external_memory_connector,
            feedback_store=feedback_store,
            repositories={
                repo_id: ArcGraphRepositoryConfig(
                    root_path=root,
                    output_dir=output_dir,
                    expose_source_snippets=expose_source_snippets,
                )
            },
        )


@dataclass(slots=True)
class _ResolvedRepo:
    repo_id: str
    root_path: Path
    output_dir: Path
    expose_source_snippets: bool
    mode: str = "read_only"


@dataclass(slots=True)
class _Providers:
    context: ContextProvider
    risk: RiskProvider
    why: WhyProvider


class ArcGraphMCPToolGroup:
    def __init__(self, config: ArcGraphMCPConfig) -> None:
        self.config = config
        self._allowed_roots = [Path(root).resolve() for root in config.allowed_roots]

    @property
    def feedback_enabled(self) -> bool:
        return self.config.feedback_store is not None

    def arcgraph_help(
        self,
        topic: HelpTopic = "overview",
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        return build_agent_help(
            topic=topic,
            tool_name=tool_name,
            surface="mcp",
            feedback_enabled=self.feedback_enabled,
            cli_commands=tuple(_cli_top_level_command_names()),
        )

    def arcgraph_record_trial_feedback(
        self,
        feedback: TrialFeedbackPayload,
    ) -> dict[str, Any]:
        store = self.config.feedback_store
        if store is None:
            return feedback_error_payload(
                TrialFeedbackDisabledError(
                    "Trial feedback is disabled for this MCP server."
                )
            )
        try:
            request = validate_feedback_request(
                feedback,
                allowed_tool_names_by_surface={
                    "mcp": ALL_MCP_CAPABILITY_NAMES,
                    "cli": _cli_top_level_command_names(),
                },
            )
            record, duplicate = store.record(request)
        except TrialFeedbackError as exc:
            return feedback_error_payload(exc)
        return feedback_success_payload(record, duplicate=duplicate)

    def arcgraph_index_status(self, repo_id: str = "default") -> dict[str, Any]:
        repo = self._resolve_repo(repo_id)
        try:
            payload = self._providers(repo).context.index_status()
        except (FileNotFoundError, SchemaVersionError, RuntimeError) as exc:
            return self._unavailable_for_repo(
                repo=repo,
                repo_id=repo_id,
                warning=str(exc),
                include_source=False,
            )
        payload["repo_root"] = str(repo.root_path)
        payload["mode"] = repo.mode
        return self._sanitize_payload(
            payload, repo=repo, repo_id=repo_id, include_source=False
        )

    def arcgraph_get_context(
        self,
        repo_id: str = "default",
        task: str | None = None,
        targets: list[str] | None = None,
        max_results: int = 30,
        detail_level: DetailLevel = "summary",
        profile: str = "review_default",
        include_source: bool = False,
    ) -> dict[str, Any]:
        detail_level = _detail_level(detail_level)
        repo = self._resolve_repo(repo_id)
        target_list = self._normalize_targets(targets, label="targets")
        self._validate_path_targets(repo, target_list)
        try:
            providers = self._providers(repo)
        except (FileNotFoundError, SchemaVersionError, RuntimeError) as exc:
            return self._unavailable_for_repo(
                repo=repo,
                repo_id=repo_id,
                warning=str(exc),
                include_source=include_source,
                target_scoped=True,
            )
        request = ContextRequest(
            task=task,
            targets=target_list,
            max_results=self._cap_results(max_results),
            detail_level=detail_level,
            profile=profile,
            include_source=include_source,
        )
        payload = providers.context.get_context(request)
        sanitized = self._sanitize_payload(
            payload,
            repo=repo,
            repo_id=repo_id,
            include_source=include_source,
            target_scoped=True,
        )
        return ContextResponse.model_validate(sanitized).model_dump(
            mode="json", exclude_none=True
        )

    def arcgraph_explain(
        self,
        repo_id: str = "default",
        task: str | None = None,
        targets: list[str] | None = None,
        max_results: int = 30,
        detail_level: DetailLevel = "summary",
        profile: str = "review_default",
        include_source: bool = False,
    ) -> dict[str, Any]:
        detail_level = _detail_level(detail_level)
        repo = self._resolve_repo(repo_id)
        target_list = self._normalize_targets(targets, label="targets")
        self._validate_path_targets(repo, target_list)
        try:
            providers = self._providers(repo)
        except (FileNotFoundError, SchemaVersionError, RuntimeError) as exc:
            sanitized = self._unavailable_for_repo(
                repo=repo,
                repo_id=repo_id,
                warning=str(exc),
                include_source=include_source,
                target_scoped=True,
            )
            return ExplainResponse.model_validate(sanitized).model_dump(
                mode="json", exclude_none=True
            )
        request = ContextRequest(
            task=task,
            targets=target_list,
            max_results=self._cap_results(max_results),
            detail_level=detail_level,
            profile=profile,
            include_source=include_source,
        )
        payload = providers.context.explain(request)
        sanitized = self._sanitize_payload(
            payload,
            repo=repo,
            repo_id=repo_id,
            include_source=include_source,
            target_scoped=True,
        )
        sanitized = bound_explain_relations(sanitized)
        return ExplainResponse.model_validate(sanitized).model_dump(
            mode="json", exclude_none=True
        )

    def arcgraph_get_risk(
        self,
        repo_id: str = "default",
        targets: list[str] | None = None,
        changed_files: list[str] | None = None,
        max_depth: int = 2,
        max_results: int = 3,
        verify_references: bool = False,
        include_source: bool = False,
    ) -> dict[str, Any]:
        repo = self._resolve_repo(repo_id)
        normalized_targets = self._normalize_targets(targets, label="targets")
        normalized_changed_files = self._normalize_targets(
            changed_files,
            label="changed_files",
        )
        self._normalize_targets(
            [*normalized_targets, *normalized_changed_files],
            label="targets and changed_files",
        )
        self._validate_path_targets(repo, normalized_targets)
        self._validate_path_targets(repo, normalized_changed_files)
        try:
            providers = self._providers(repo)
            payload = providers.risk.get_risk(
                normalized_targets,
                changed_files=normalized_changed_files,
                max_depth=max_depth,
                max_results=self._cap_results(max_results),
                verify_references=verify_references,
            )
        except (FileNotFoundError, SchemaVersionError, RuntimeError) as exc:
            sanitized = self._unavailable_for_repo(
                repo=repo,
                repo_id=repo_id,
                warning=str(exc),
                include_source=include_source,
                target_scoped=True,
            )
            return RiskReport.model_validate(sanitized).model_dump(
                mode="json", exclude_none=True
            )
        sanitized = self._sanitize_payload(
            payload,
            repo=repo,
            repo_id=repo_id,
            include_source=include_source,
            target_scoped=True,
        )
        return RiskReport.model_validate(sanitized).model_dump(
            mode="json", exclude_none=True
        )

    def arcgraph_entrypoint_flow(
        self,
        repo_id: str = "default",
        entrypoint: str | None = None,
        method: str | None = None,
        path: str | None = None,
        max_depth: int = 3,
        max_results: int = 50,
        include_source: bool = False,
    ) -> dict[str, Any]:
        repo = self._resolve_repo(repo_id)
        normalized = _entrypoint_query(entrypoint=entrypoint, method=method, path=path)
        normalized = self._normalize_targets(
            [normalized],
            label="entrypoint",
            max_targets=1,
        )[0]
        self._validate_path_targets(repo, [normalized])
        try:
            providers = self._providers(repo)
            payload = providers.context.entrypoint_flow(
                normalized,
                max_depth=max_depth,
                max_results=self._cap_results(max_results),
            )
        except (FileNotFoundError, SchemaVersionError, RuntimeError) as exc:
            return self._unavailable_for_repo(
                repo=repo,
                repo_id=repo_id,
                warning=str(exc),
                include_source=include_source,
                target_scoped=True,
            )
        return bound_flow_payload(
            self._sanitize_payload(
                payload,
                repo=repo,
                repo_id=repo_id,
                include_source=include_source,
                target_scoped=True,
            )
        )

    def arcgraph_get_why(
        self,
        repo_id: str = "default",
        target: str = "",
        task: str | None = None,
        max_results: int = 10,
        include_source: bool = False,
    ) -> dict[str, Any]:
        repo = self._resolve_repo(repo_id)
        target = self._normalize_targets(
            [target],
            label="target",
            max_targets=1,
        )[0]
        self._validate_path_targets(repo, [target])
        try:
            providers = self._providers(repo)
            payload = providers.why.get_why(
                target,
                max_results=self._cap_results(max_results),
                repo_id=repo_id,
                task=task,
            )
        except (FileNotFoundError, SchemaVersionError, RuntimeError) as exc:
            sanitized = self._unavailable_for_repo(
                repo=repo,
                repo_id=repo_id,
                warning=str(exc),
                include_source=include_source,
                target_scoped=True,
            )
            return WhyReport.model_validate(sanitized).model_dump(
                mode="json", exclude_none=True
            )
        sanitized = self._sanitize_payload(
            payload,
            repo=repo,
            repo_id=repo_id,
            include_source=include_source,
            target_scoped=True,
        )
        return WhyReport.model_validate(sanitized).model_dump(
            mode="json", exclude_none=True
        )

    def arcgraph_find_similar(
        self,
        repo_id: str = "default",
        target: str = "",
        max_results: int = 10,
        detail_level: DetailLevel = "summary",
        include_source: bool = False,
    ) -> dict[str, Any]:
        detail_level = _detail_level(detail_level)
        repo = self._resolve_repo(repo_id)
        target = self._normalize_targets(
            [target],
            label="target",
            max_targets=1,
        )[0]
        self._validate_path_targets(repo, [target])
        try:
            providers = self._providers(repo)
            payload = providers.context.find_similar(
                target,
                max_results=self._cap_results(max_results),
                detail_level=detail_level,
            )
        except (FileNotFoundError, SchemaVersionError, RuntimeError) as exc:
            return self._unavailable_for_repo(
                repo=repo,
                repo_id=repo_id,
                warning=str(exc),
                include_source=include_source,
                target_scoped=True,
            )
        return self._sanitize_payload(
            payload,
            repo=repo,
            repo_id=repo_id,
            include_source=include_source,
            target_scoped=True,
        )

    def arcgraph_record_learning(
        self,
        repo_id: str = "default",
        target: str = "",
        observation: str = "",
        memory_type: str = "lesson_learned",
        title: str | None = None,
        include_source: bool = False,
    ) -> dict[str, Any]:
        repo = self._resolve_repo(repo_id)
        target = self._normalize_targets(
            [target],
            label="target",
            max_targets=1,
        )[0]
        self._validate_path_targets(repo, [target])
        try:
            providers = self._providers(repo)
            payload = providers.why.propose_learning(
                repo_id=repo_id,
                target=target,
                observation=observation,
                memory_type=memory_type,
                title=title,
            )
        except (FileNotFoundError, SchemaVersionError, RuntimeError) as exc:
            return self._unavailable_for_repo(
                repo=repo,
                repo_id=repo_id,
                warning=str(exc),
                include_source=include_source,
                target_scoped=True,
            )
        return self._sanitize_payload(
            payload,
            repo=repo,
            repo_id=repo_id,
            include_source=include_source,
            target_scoped=True,
        )

    def arcgraph_preview_change_plan(
        self,
        repo_id: str = "default",
        task: str = "",
        targets: list[dict[str, Any]] | None = None,
        acceptance_criteria: list[str] | None = None,
        constraints: list[str] | None = None,
        protected_surfaces: list[str] | None = None,
        openapi_input: str | None = None,
    ) -> dict[str, Any]:
        """Compute a plan only; never activate a revision or a pin."""

        repo = self._resolve_repo(repo_id)
        try:
            if not task.strip():
                raise ValueError("task must not be empty")
            target_records = _mcp_change_targets(
                repo_id=repo_id,
                targets=targets or [],
            )
            self._validate_path_targets(
                repo,
                [item.value for item in target_records if item.kind == "path"],
            )
            declarations = list(protected_surfaces or [])
            self._validate_path_targets(
                repo,
                [value for value in declarations if _looks_like_path(value)],
            )
            if openapi_input is not None:
                self._validate_path_targets(repo, [openapi_input])
            identity = canonical_digest(
                {
                    "repo_id": repo_id,
                    "task": task,
                    "targets": [
                        target.model_dump(mode="json") for target in target_records
                    ],
                    "acceptance_criteria": acceptance_criteria or [],
                    "constraints": constraints or [],
                    "protected_surfaces": declarations,
                    "openapi_input": openapi_input,
                }
            )[:16]
            revision = self._change_service(repo).preview_plan(
                ChangeIntent(
                    repo_id=repo_id,
                    intent_id=f"mcp-preview-{identity}",
                    task=task,
                    targets=target_records,
                    acceptance_criteria=list(acceptance_criteria or []),
                    constraints=list(constraints or []),
                    user_declared_protected_surfaces=declarations,
                ),
                plan_id=f"mcp-preview-plan-{identity}",
                pin_id=f"mcp-preview-pin-{identity}",
                openapi_input=openapi_input,
            )
        except ArcGraphPermissionError:
            raise
        except (ChangeSafetyError, FileNotFoundError, RuntimeError, ValueError) as exc:
            return _change_mcp_error(repo_id, exc)
        return _change_mcp_payload(
            repo_id=repo_id,
            operation="preview_change_plan",
            data=revision.model_dump(mode="json"),
            verdict=revision.planning_verdict,
        )

    def arcgraph_get_change_plan(
        self,
        repo_id: str = "default",
        plan_id: str = "",
    ) -> dict[str, Any]:
        """Read the current plan view and dynamic evidence summary only."""

        repo = self._resolve_repo(repo_id)
        try:
            view = self._change_service(repo).get_view(plan_id)
        except (ChangeSafetyError, FileNotFoundError, RuntimeError, ValueError) as exc:
            return _change_mcp_error(repo_id, exc)
        return _change_mcp_payload(
            repo_id=repo_id,
            operation="get_change_plan",
            data=view.model_dump(mode="json"),
            verdict=view.plan_revision.planning_verdict,
        )

    def arcgraph_list_change_plans(
        self,
        repo_id: str = "default",
    ) -> dict[str, Any]:
        """List current views without writing a plan, pointer, or cache."""

        repo = self._resolve_repo(repo_id)
        try:
            views = self._change_service(repo).list_views()
        except (ChangeSafetyError, FileNotFoundError, RuntimeError, ValueError) as exc:
            return _change_mcp_error(repo_id, exc)
        return _change_mcp_payload(
            repo_id=repo_id,
            operation="list_change_plans",
            data={"plans": [view.model_dump(mode="json") for view in views]},
        )

    def arcgraph_get_graph_delta(
        self,
        repo_id: str = "default",
        plan_id: str = "",
        revision: int = 0,
        plan_content_digest: str = "",
    ) -> dict[str, Any]:
        """Compute an exact current graph delta without persisting a report."""

        repo = self._resolve_repo(repo_id)
        try:
            delta = self._change_service(repo).diff(
                plan_id,
                revision,
                plan_content_digest,
            )
        except (ChangeSafetyError, FileNotFoundError, RuntimeError, ValueError) as exc:
            return _change_mcp_error(repo_id, exc)
        return _change_mcp_payload(
            repo_id=repo_id,
            operation="get_graph_delta",
            data=delta.model_dump(mode="json"),
        )

    def arcgraph_verify_change(
        self,
        repo_id: str = "default",
        plan_id: str = "",
        revision: int = 0,
        plan_content_digest: str = "",
    ) -> dict[str, Any]:
        """Compute verification without writing a report or lifecycle event."""

        repo = self._resolve_repo(repo_id)
        try:
            report = self._change_service(repo).preview_verify(
                plan_id,
                revision,
                plan_content_digest,
            )
        except (ChangeSafetyError, FileNotFoundError, RuntimeError, ValueError) as exc:
            return _change_mcp_error(repo_id, exc)
        return _change_mcp_payload(
            repo_id=repo_id,
            operation="verify_change",
            data=report.model_dump(mode="json"),
            verdict=report.verdict,
        )

    def _providers(self, repo: _ResolvedRepo) -> _Providers:
        engine = QueryEngine(repo.output_dir)
        return _Providers(
            context=ContextProvider(engine),
            risk=RiskProvider(engine),
            why=WhyProvider(
                engine,
                external_memory=self.config.external_memory_connector,
            ),
        )

    def _change_service(self, repo: _ResolvedRepo) -> ChangeSafetyService:
        """Create a read-only-use service facade with an external trust root."""

        registry_path = self.config.trusted_runner_registry_path
        registry = (
            TrustedRunnerRegistry(Path(registry_path), repo_root=repo.root_path)
            if registry_path is not None
            else None
        )

        return ChangeSafetyService(
            repo.root_path,
            repo.output_dir,
            repo_id=repo.repo_id,
            trusted_runner_registry=registry,
        )

    def _resolve_repo(self, repo_id: str) -> _ResolvedRepo:
        if repo_id not in self.config.repositories:
            raise ArcGraphPermissionError(
                f"ArcGraph repo_id {repo_id!r} is not registered."
            )

        configured = self.config.repositories[repo_id]
        root_path = Path(configured.root_path).resolve()
        if not any(
            _is_relative_to(root_path, allowed) for allowed in self._allowed_roots
        ):
            raise ArcGraphPermissionError(
                f"Repository {repo_id!r} is outside configured allowed_roots."
            )

        output_dir = (
            Path(configured.output_dir)
            if configured.output_dir is not None
            else Path(self.config.default_output_dir)
        )
        if not output_dir.is_absolute():
            output_dir = root_path / output_dir

        return _ResolvedRepo(
            repo_id=repo_id,
            root_path=root_path,
            output_dir=output_dir.resolve(),
            expose_source_snippets=configured.expose_source_snippets,
            mode=configured.mode,
        )

    def _validate_path_targets(self, repo: _ResolvedRepo, targets: list[str]) -> None:
        for target in targets:
            # `METHOD /path` targets are route queries, not filesystem paths:
            # `_looks_like_path` excludes them and they are resolved by node-id
            # lookup, never by a path resolver, so repo containment does not
            # apply. Anything that later resolves such a target against the
            # filesystem must be validated at that call site instead.
            if not _looks_like_path(target):
                continue
            resolved = _resolve_path_target(target, repo.root_path)
            if not _is_relative_to(resolved, repo.root_path):
                raise ArcGraphPermissionError(
                    f"Path target {target!r} is outside repository {repo.repo_id!r}."
                )

    def _sanitize_payload(
        self,
        payload: dict[str, Any],
        *,
        repo: _ResolvedRepo,
        repo_id: str,
        include_source: bool,
        target_scoped: bool = False,
    ) -> dict[str, Any]:
        allow_snippets = repo.expose_source_snippets and include_source
        sanitized = payload if allow_snippets else strip_source_snippets(payload)
        if target_scoped:
            sanitized = apply_target_payload_contract(sanitized)
        sanitized = _contain_paths(sanitized, repo.root_path)
        if include_source and not repo.expose_source_snippets:
            warnings = list(sanitized.get("warnings", []))
            warnings.append(
                "Source snippets were requested but are disabled for this repository."
            )
            sanitized = {**sanitized, "warnings": unique_values(warnings)}
        sanitized.setdefault("schema_version", SCHEMA_VERSION)
        sanitized.setdefault("status", "available")
        sanitized["repo_id"] = repo_id
        sanitized["read_only"] = repo.mode == "read_only"
        sanitized["source_snippets"] = {
            "requested": include_source,
            "enabled": repo.expose_source_snippets,
        }
        return sanitized

    def _unavailable_for_repo(
        self,
        *,
        repo: _ResolvedRepo,
        repo_id: str,
        warning: str,
        include_source: bool,
        target_scoped: bool = False,
    ) -> dict[str, Any]:
        return self._sanitize_payload(
            self._base_unavailable(repo_id, warning),
            repo=repo,
            repo_id=repo_id,
            include_source=include_source,
            target_scoped=target_scoped,
        )

    def _cap_results(self, requested: int) -> int:
        if requested <= 0:
            return min(30, self.config.max_results_limit)
        return min(requested, self.config.max_results_limit)

    def _normalize_targets(
        self,
        values: list[str] | None,
        *,
        label: str,
        max_targets: int | None = None,
    ) -> list[str]:
        return normalize_targets(
            values,
            label=label,
            max_targets=(
                min(self.config.max_targets_limit, MAX_TARGETS_PER_REQUEST)
                if max_targets is None
                else min(
                    max_targets,
                    self.config.max_targets_limit,
                    MAX_TARGETS_PER_REQUEST,
                )
            ),
        )

    @staticmethod
    def _base_unavailable(repo_id: str, warning: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "repo_id": repo_id,
            "freshness": {
                "status": "unknown",
                "stale": False,
                "stale_files": [],
                "stale_modules": [],
                "reason": warning,
            },
            "capabilities": {},
            "warnings": [warning],
        }


def register_arcgraph_mcp_tools(mcp: Any, tool_group: ArcGraphMCPToolGroup) -> None:
    for spec in mcp_capability_specs(feedback_enabled=tool_group.feedback_enabled):
        decorator = mcp.tool(
            name=spec.name,
            title=spec.title,
            description=spec.description,
            annotations=_mcp_annotations(spec),
        )
        decorator(getattr(tool_group, spec.name))


@lru_cache(maxsize=1)
def _cli_top_level_command_names() -> frozenset[str]:
    # Imported lazily to avoid making the MCP module depend on CLI import order.
    from arcgraph.interfaces.cli import build_parser

    parser = build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    return frozenset(subparsers.choices)


def _mcp_annotations(spec: AgentCapabilitySpec) -> dict[str, str | bool]:
    """Return protocol-shaped hints without importing the optional MCP runtime."""

    return {
        "title": spec.title,
        "readOnlyHint": spec.behavior.read_only,
        "destructiveHint": spec.behavior.destructive,
        "idempotentHint": spec.behavior.idempotent,
        "openWorldHint": spec.behavior.open_world,
    }


def _entrypoint_query(
    *, entrypoint: str | None, method: str | None, path: str | None
) -> str:
    if method and path:
        normalized_path = path if path.startswith("/") else f"/{path}"
        # Hand back the method/path form rather than a literal route id so the
        # query engine's alias expansion (including ANY/ALL wildcards) applies.
        return f"{method.upper()} {normalized_path}"
    if entrypoint:
        return entrypoint
    raise ValueError("Provide either entrypoint or both method and path.")


def _detail_level(value: str) -> str:
    if value not in {"summary", "standard", "detailed"}:
        raise ArcGraphRequestError(
            "detail_level must be summary, standard, or detailed"
        )
    return value


def _mcp_change_targets(
    *,
    repo_id: str,
    targets: list[dict[str, Any]],
) -> list[ChangeTarget]:
    """Parse explicit MCP targets without allowing hidden free-text discovery."""

    records: list[ChangeTarget] = []
    allowed_fields = {
        "kind",
        "value",
        "resolution_policy",
        "reason",
        "requested_by",
        "repo_id",
    }
    for raw in targets:
        if not isinstance(raw, dict):
            raise ValueError("each change target must be an object")
        if set(raw) - allowed_fields:
            raise ValueError("change target contains unsupported fields")
        supplied_repo_id = raw.get("repo_id")
        if supplied_repo_id is not None and supplied_repo_id != repo_id:
            raise ValueError("change target repo_id does not match the MCP repo_id")
        fields = {key: value for key, value in raw.items() if key != "repo_id"}
        records.append(ChangeTarget(repo_id=repo_id, **fields))
    return records


def _change_mcp_payload(
    *,
    repo_id: str,
    operation: str,
    data: dict[str, Any],
    verdict: str | None = None,
) -> dict[str, Any]:
    blocked = verdict is not None and verdict_is_blocking(verdict)  # type: ignore[arg-type]
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "change_contract_version": CHANGE_CONTRACT_VERSION,
        "repo_id": repo_id,
        "operation": operation,
        "status": "blocked" if blocked else "available",
        "read_only": True,
        "source_snippets": {"requested": False, "enabled": False},
        "data": data,
    }
    if verdict is not None:
        payload["verdict"] = verdict
    return payload


def _change_mcp_error(repo_id: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ChangeSafetyError):
        detail = exc.to_payload()
        error_code = detail["code"]
        message = detail["message"]
    elif isinstance(exc, ValueError):
        error_code = "CHANGE_MCP_INPUT_INVALID"
        message = str(exc)
    else:
        error_code = "CHANGE_MCP_RUNTIME_ERROR"
        message = str(exc)
    message = redact_sensitive_text(message, redact_paths=True)[0]
    return {
        "schema_version": SCHEMA_VERSION,
        "change_contract_version": CHANGE_CONTRACT_VERSION,
        "repo_id": repo_id,
        "status": "error",
        "read_only": True,
        "source_snippets": {"requested": False, "enabled": False},
        "error_code": error_code,
        "error": {"code": error_code, "message": message},
    }


def _route_target_path(value: str) -> str | None:
    """Return the path component of a ``METHOD /path`` entrypoint target."""

    parsed = parse_route_target(value)
    return parsed[1] if parsed else None


def _looks_like_path(value: str) -> bool:
    if not value:
        return False
    stable_prefixes = (
        "mod:",
        "class:",
        "fn:",
        "method:",
        "route:",
        "mcp_tool:",
        "mcp:",
        "worker:",
        "queue:",
        "table:",
    )
    if value.startswith(stable_prefixes):
        return False
    if _route_target_path(value) is not None:
        return False
    path = Path(value)
    return (
        path.is_absolute()
        or "/" in value
        or "\\" in value
        or value.endswith(".py")
        or value.startswith((".", ".."))
    )


def _resolve_path_target(value: str, repo_root: Path) -> Path:
    if _is_any_os_absolute(value):
        candidate = Path(value)
        if not candidate.is_absolute():
            raise ArcGraphPermissionError(
                f"Path target {value!r} uses absolute path syntax from another OS."
            )
        return candidate.resolve(strict=False)
    candidate = Path(value.replace("\\", "/"))
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate.resolve(strict=False)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


# Fields whose values should be checked for path containment.
_PATH_FIELDS = frozenset({"path", "abs_path", "source_path", "target_path"})
_PATH_LIST_FIELDS = frozenset(
    {
        "paths",
        "stale_paths",
        "source_paths",
        "target_paths",
        "reanalysis_recommended_paths",
    }
)

# Domain path fields that are intentionally not filesystem paths.
_DOMAIN_PROPERTY_PATH_FIELDS = frozenset({("route", "path")})


def _contain_paths(
    value: Any,
    repo_root: Path,
    *,
    _owner_kind: str | None = None,
    _parent_key: str | None = None,
    _path_replacements: tuple[tuple[str, str], ...] = (),
) -> Any:
    """Recursively replace path values outside *repo_root* with ``<external>``.

    Path-like keys are sanitized everywhere except for explicitly
    whitelisted domain fields such as ``route.properties.path``.
    """
    if isinstance(value, dict):
        owner_kind = _dict_kind(value) or _owner_kind
        path_replacements = list(_path_replacements)
        for key, item in value.items():
            if _is_domain_property_path(owner_kind, _parent_key, key):
                continue
            if not _is_path_field(key):
                continue
            for raw in _path_strings(item):
                sanitized_path = _sanitize_path_value(raw, repo_root)
                if sanitized_path != raw:
                    path_replacements.append((raw, sanitized_path))
        replacement_tuple = tuple(
            sorted(set(path_replacements), key=lambda item: len(item[0]), reverse=True)
        )
        result: dict[str, Any] = {}
        for key, item in value.items():
            if _is_domain_property_path(owner_kind, _parent_key, key):
                result[key] = item
            elif _is_path_field(key):
                result[key] = _sanitize_path_item(item, repo_root)
            else:
                result[key] = _contain_paths(
                    item,
                    repo_root,
                    _owner_kind=owner_kind,
                    _parent_key=key,
                    _path_replacements=replacement_tuple,
                )
        return result
    if isinstance(value, list):
        return [
            _contain_paths(
                item,
                repo_root,
                _owner_kind=_owner_kind,
                _parent_key=_parent_key,
                _path_replacements=_path_replacements,
            )
            for item in value
        ]
    if isinstance(value, str):
        return _sanitize_free_text(value, repo_root, _path_replacements)
    return value


def _path_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


_WINDOWS_ABSOLUTE_TEXT = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:[A-Z]:[\\/][^\s\"'<>]+|"
    r"\\\\[^\\\s\"'<>]+\\[^\s\"'<>]+)"
)
_EXISTING_REDACTION_SENTINEL = "[REDACTED]"


def _sanitize_free_text(
    value: str,
    repo_root: Path,
    path_replacements: tuple[tuple[str, str], ...],
) -> str:
    sanitized = value
    for raw, replacement in path_replacements:
        sanitized = sanitized.replace(raw, replacement)
    sanitized = _WINDOWS_ABSOLUTE_TEXT.sub(
        lambda match: _sanitize_path_value(match.group(0), repo_root),
        sanitized,
    )
    return _EXISTING_REDACTION_SENTINEL.join(
        redact_sensitive_text(part, redact_paths=True)[0]
        for part in sanitized.split(_EXISTING_REDACTION_SENTINEL)
    )


def _dict_kind(value: dict[str, Any]) -> str | None:
    kind = value.get("kind")
    return kind if isinstance(kind, str) and kind else None


def _is_domain_property_path(
    owner_kind: str | None, parent_key: str | None, key: str
) -> bool:
    return (
        parent_key == "properties"
        and owner_kind is not None
        and (owner_kind, key) in _DOMAIN_PROPERTY_PATH_FIELDS
    )


def _is_path_field(key: str) -> bool:
    return (
        key in _PATH_FIELDS
        or key in _PATH_LIST_FIELDS
        or key.endswith("_path")
        or key.endswith("_paths")
    )


def _sanitize_path_item(value: Any, repo_root: Path) -> Any:
    if isinstance(value, str):
        return _sanitize_path_value(value, repo_root)
    if isinstance(value, list):
        return [
            _sanitize_path_value(item, repo_root) if isinstance(item, str) else item
            for item in value
        ]
    return _contain_paths(value, repo_root)


def _sanitize_path_value(raw: str, repo_root: Path) -> str:
    """Return *raw* unchanged if it's within *repo_root*, else ``<external>``."""
    if not raw:
        return raw
    # Relative project paths (e.g. "backend/src/foo.py") are fine.
    # Use cross-platform detection so POSIX paths don't leak on Windows.
    if not _is_any_os_absolute(raw):
        candidate = Path(raw)
        # A relative path can't escape repo_root unless it has ..
        if ".." not in candidate.parts:
            return raw
        candidate = (repo_root / candidate).resolve()
    else:
        # Cross-OS safety: a Windows path like C:\Users\... on POSIX would
        # be treated as relative by native Path(), potentially resolving
        # under repo_root and leaking through. Detect the mismatch and
        # reject immediately: if the path is absolute for *some* OS but
        # the native Path doesn't consider it absolute, it's foreign.
        native = Path(raw)
        if not native.is_absolute():
            return "<external>"
        candidate = native.resolve()
    if _is_relative_to(candidate, repo_root):
        return raw
    return "<external>"
