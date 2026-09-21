"""Target-driven, deterministic planner with no LLM or free-text discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid
from typing import Any, Iterable

from arcgraph.change.contracts import (
    SOURCE_NORMALIZATION_PROJECTION_VERSION,
    BaselineReference,
    ChangeIntent,
    ChangePlanRevision,
    ChangeTarget,
    ProtectedSurface,
    ScopeItem,
    canonical_digest,
    stable_projection,
)
from arcgraph.change.errors import ChangeSafetyError, RepositoryPathError
from arcgraph.change.paths import normalize_repository_path
from arcgraph.change.protected_surfaces import ProtectedSurfaceDetector
from arcgraph.change.source_provider import (
    BaselineSourceProvider,
    normalize_implementation_region,
)
from arcgraph.change.verdicts import VerdictFinding, decide_planning_verdict
from arcgraph.change.verification_plan import build_verification_plan
from arcgraph.core.schemas import Node


@dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    target: ChangeTarget
    scopes: tuple[ScopeItem, ...]


class ChangePlanner:
    """Create a plan exclusively from explicit, resolved ChangeTargets."""

    def __init__(
        self,
        repo_root: Path,
        *,
        repo_id: str,
        protected_surface_detector: ProtectedSurfaceDetector | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.repo_id = repo_id
        self.surface_detector = protected_surface_detector or ProtectedSurfaceDetector(
            self.repo_root,
            repo_id=repo_id,
        )

    def plan(
        self,
        intent: ChangeIntent,
        baseline: BaselineReference,
        nodes: Iterable[Node],
        *,
        plan_id: str | None = None,
        revision: int = 1,
        openapi_input: str | Path | None = None,
    ) -> ChangePlanRevision:
        if intent.repo_id != self.repo_id or baseline.repo_id != self.repo_id:
            raise ValueError("planner inputs must belong to the configured repo_id")
        node_list = list(nodes)
        target_findings: list[VerdictFinding] = []
        baseline_source: BaselineSourceProvider | None = None
        try:
            baseline_source = BaselineSourceProvider(self.repo_root, baseline)
        except ChangeSafetyError as exc:
            target_findings.append(
                VerdictFinding(
                    code="BASELINE_UNAVAILABLE",
                    message="baseline cannot provide an immutable Git source snapshot",
                    severity="blocking",
                    category="baseline",
                    details={"cause": exc.code},
                )
            )
        resolved: list[_ResolvedTarget] = []
        for target in intent.targets:
            try:
                scopes = self._resolve_target(target, node_list, baseline_source)
            except ChangeSafetyError as exc:
                target_findings.append(
                    VerdictFinding(
                        code="BASELINE_UNAVAILABLE",
                        message=(
                            "baseline cannot provide source for the resolved "
                            f"target: {target.value}"
                        ),
                        severity="blocking",
                        category="baseline",
                        details={"cause": exc.code},
                    )
                )
                scopes = []
            if scopes:
                resolved.append(_ResolvedTarget(target=target, scopes=tuple(scopes)))
                continue
            severity = (
                "blocking"
                if target.resolution_policy == "must_resolve"
                else "manual_review"
            )
            target_findings.append(
                VerdictFinding(
                    code="TARGET_UNRESOLVED",
                    message=f"target could not be resolved exactly: {target.value}",
                    severity=severity,
                    category="target_resolution",
                )
            )
        if not resolved and not any(
            finding.code == "TARGET_UNRESOLVED" and finding.severity == "blocking"
            for finding in target_findings
        ):
            target_findings.append(
                VerdictFinding(
                    code="PLAN_INVALID_INTENT",
                    message="at least one valid ChangeTarget is required",
                    severity="blocking",
                    category="target_resolution",
                )
            )

        surfaces = self.surface_detector.detect(
            node_list,
            user_declarations=intent.user_declared_protected_surfaces,
            openapi_input=openapi_input,
        )
        primary = _dedupe_scopes(
            scope.model_copy(update={"policy": "candidate"})
            for item in resolved
            for scope in item.scopes
        )
        allowed = _dedupe_scopes(
            scope.model_copy(update={"policy": "allowed"})
            for item in resolved
            for scope in item.scopes
        )
        (
            protected,
            review_only,
            matched_surfaces,
            surface_findings,
        ) = self._protected_scope(
            allowed,
            surfaces,
        )
        matched_ids = {surface.surface_id for surface in matched_surfaces}
        for surface in surfaces:
            if (
                surface.detection_status != "declared"
                or surface.surface_id in matched_ids
            ):
                continue
            matched_surfaces.append(surface)
            declared_scopes = _declared_surface_scopes(surface)
            if surface.policy == "review_only":
                review_only.extend(declared_scopes)
            else:
                protected.extend(declared_scopes)
            matched_ids.add(surface.surface_id)
        target_findings.extend(surface_findings)
        planning_verdict = decide_planning_verdict(target_findings)
        actual_plan_id = plan_id or f"plan-{uuid.uuid4().hex}"
        input_digest = canonical_digest(
            {
                "intent": stable_projection(intent),
                "baseline": stable_projection(baseline),
                "targets": [stable_projection(item.target) for item in resolved],
                "allowed_edit_scope": [stable_projection(scope) for scope in allowed],
                "protected_surfaces": [
                    stable_projection(surface) for surface in matched_surfaces
                ],
            }
        )
        verification_plan = build_verification_plan(
            repo_id=self.repo_id,
            plan_id=actual_plan_id,
            plan_revision=revision,
            input_digest=input_digest,
            allowed_edit_scope=allowed,
            protected_surfaces=matched_surfaces,
            acceptance_criteria=intent.acceptance_criteria,
        )
        return ChangePlanRevision(
            repo_id=self.repo_id,
            plan_id=actual_plan_id,
            revision=revision,
            intent=intent,
            baseline=baseline,
            planning_verdict=planning_verdict,
            primary_edit_candidates=primary,
            allowed_edit_scope=allowed,
            review_only_scope=_dedupe_scopes(review_only),
            protected_scope=_dedupe_scopes(protected),
            protected_surfaces=matched_surfaces,
            affected_entrypoints=_surface_projection(
                matched_surfaces, "route", "cli", "mcp"
            ),
            affected_resources=_surface_projection(matched_surfaces, "table", "queue"),
            affected_contracts=_surface_projection(
                matched_surfaces,
                "openapi",
                "event_contract",
                "typescript_export",
            ),
            verification_plan=verification_plan,
            unknowns=[
                finding.to_payload()
                for finding in target_findings
                if finding.severity != "blocking"
            ],
            stop_conditions=[
                finding.to_payload()
                for finding in target_findings
                if finding.severity == "blocking"
            ],
            residual_risk=[
                finding.to_payload()
                for finding in target_findings
                if finding.severity in {"warning", "manual_review"}
            ],
            input_digest=input_digest,
        )

    def _resolve_target(
        self,
        target: ChangeTarget,
        nodes: list[Node],
        baseline_source: BaselineSourceProvider | None,
    ) -> list[ScopeItem]:
        if target.repo_id != self.repo_id:
            return []
        if target.kind == "path":
            return self._resolve_path_target(target, nodes, baseline_source)
        matched = [node for node in nodes if self._target_matches_node(target, node)]
        return [
            self._scope_from_node(target, node, baseline_source)
            for node in matched
            if node.path
        ]

    def _resolve_path_target(
        self,
        target: ChangeTarget,
        nodes: list[Node],
        baseline_source: BaselineSourceProvider | None,
    ) -> list[ScopeItem]:
        try:
            normalized = normalize_repository_path(target.value, self.repo_root)
        except RepositoryPathError:
            return []
        matching = [
            node
            for node in nodes
            if node.path
            and normalize_repository_path(node.path, self.repo_root).comparison_key
            == normalized.comparison_key
        ]
        if matching:
            return [
                self._scope_from_node(target, node, baseline_source)
                for node in matching
            ]
        disk_path = self.repo_root / Path(*normalized.display_path.split("/"))
        if disk_path.is_file() and not disk_path.is_symlink():
            return [
                ScopeItem(
                    repo_id=self.repo_id,
                    scope_item_id=_scope_id(normalized.display_path, None),
                    path=normalized.display_path,
                    path_comparison_key=normalized.comparison_key,
                    scope_kind="path",
                    policy="allowed",
                    reason=f"explicit path target: {target.value}",
                    confidence="confirmed",
                )
            ]
        return []

    def _target_matches_node(self, target: ChangeTarget, node: Node) -> bool:
        if target.kind == "symbol":
            return target.value in {node.id, node.qualname}
        expected_types = {
            "route": {"route", "endpoint", "api_route", "openapi_operation"},
            "resource": {"table", "database_table", "queue", "resource"},
            "contract": {"openapi_operation", "event_contract", "mcp_tool", "export"},
        }
        if node.kind not in expected_types.get(target.kind, set()):
            return False
        values = {node.id, node.qualname}
        values.update(
            value
            for value in (
                node.properties.get(target.kind),
                node.properties.get("route"),
                node.properties.get("resource"),
                node.properties.get("contract"),
            )
            if isinstance(value, str)
        )
        return target.value in values

    def _scope_from_node(
        self,
        target: ChangeTarget,
        node: Node,
        baseline_source: BaselineSourceProvider | None,
    ) -> ScopeItem:
        if not node.path:  # pragma: no cover - guarded by caller
            raise ValueError("a resolved node must have a repository path")
        normalized = normalize_repository_path(node.path, self.repo_root)
        normalization = None
        if baseline_source is not None:
            document = baseline_source.read(normalized.display_path)
            if node.start_line is not None and node.end_line is not None:
                normalization = normalize_implementation_region(
                    normalized.display_path,
                    document.content,
                    start_line=node.start_line,
                    end_line=node.end_line,
                )
            else:
                normalization = document.normalization
        return ScopeItem(
            repo_id=self.repo_id,
            scope_item_id=_scope_id(normalized.display_path, node.id),
            path=normalized.display_path,
            path_comparison_key=normalized.comparison_key,
            symbol_id=node.id,
            baseline_symbol_id=node.id,
            baseline_start_line=node.start_line,
            baseline_end_line=node.end_line,
            baseline_raw_source_digest=(
                normalization.raw_source_digest if normalization else None
            ),
            baseline_normalized_implementation_digest=(
                normalization.normalized_implementation_digest
                if normalization
                else None
            ),
            source_normalization_projection_version=(
                normalization.projection_version
                if normalization
                else SOURCE_NORMALIZATION_PROJECTION_VERSION
            ),
            scope_kind=(
                "symbol"
                if target.kind == "symbol"
                else "path" if target.kind == "path" else "surface"
            ),
            policy="allowed",
            reason=f"resolved {target.kind} target: {target.value}",
            evidence=[{"source": "graph_target_resolution", "node_id": node.id}],
            confidence="confirmed",
        )

    def _protected_scope(
        self,
        allowed: list[ScopeItem],
        surfaces: list[ProtectedSurface],
    ) -> tuple[
        list[ScopeItem],
        list[ScopeItem],
        list[ProtectedSurface],
        list[VerdictFinding],
    ]:
        protected: list[ScopeItem] = []
        review_only: list[ScopeItem] = []
        matched: list[ProtectedSurface] = []
        findings: list[VerdictFinding] = []
        for scope in allowed:
            for surface in surfaces:
                if not _surface_matches_scope(surface, scope):
                    continue
                matched.append(surface)
                target_scopes = (
                    review_only if surface.policy == "review_only" else protected
                )
                policy = (
                    "review_only" if surface.policy == "review_only" else "protected"
                )
                target_scopes.append(
                    scope.model_copy(
                        update={
                            "policy": policy,
                            "reason": (
                                f"{policy.replace('_', '-')} surface "
                                f"{surface.surface_id}: {scope.reason}"
                            ),
                        }
                    )
                )
                if surface.detection_status == "unknown":
                    findings.append(
                        VerdictFinding(
                            code="CAPABILITY_UNKNOWN",
                            message=f"surface detection is unknown: {surface.surface_id}",
                            severity="blocking",
                            category="protected_surface",
                        )
                    )
                elif surface.detection_status == "candidate":
                    findings.append(
                        VerdictFinding(
                            code="CAPABILITY_CANDIDATE",
                            message=(
                                "protected-surface capability requires manual "
                                f"review: {surface.surface_id}"
                            ),
                            severity="manual_review",
                            category="protected_surface",
                        )
                    )
        return (
            _dedupe_scopes(protected),
            _dedupe_scopes(review_only),
            _dedupe_surfaces(matched),
            findings,
        )


def _scope_id(path: str, symbol_id: str | None) -> str:
    return "scope-" + canonical_digest({"path": path, "symbol_id": symbol_id})[:20]


def _dedupe_scopes(scopes: Iterable[ScopeItem]) -> list[ScopeItem]:
    unique: dict[tuple[str, str], ScopeItem] = {}
    for scope in scopes:
        unique[(scope.scope_item_id, scope.policy)] = scope
    return [unique[key] for key in sorted(unique)]


def _dedupe_surfaces(surfaces: Iterable[ProtectedSurface]) -> list[ProtectedSurface]:
    unique = {surface.surface_id: surface for surface in surfaces}
    return [unique[key] for key in sorted(unique)]


def _surface_matches_scope(surface: ProtectedSurface, scope: ScopeItem) -> bool:
    return (
        (
            surface.symbol_id is not None
            and scope.symbol_id is not None
            and surface.symbol_id == scope.symbol_id
        )
        or (
            surface.symbol_id is None
            and surface.path_comparison_key is not None
            and surface.path_comparison_key == scope.path_comparison_key
        )
        or (surface.symbol_id is None and surface.path_comparison_key is None)
    )


def _declared_surface_scopes(
    surface: ProtectedSurface,
) -> list[ScopeItem]:
    """Keep a declaration in its explicit policy scope even when off-target."""

    policy = "review_only" if surface.policy == "review_only" else "protected"
    description = policy.replace("_", "-")

    if not surface.path or not surface.path_comparison_key:
        # Pathless declarations already match every explicit allowed scope in
        # ``_protected_scope``.  Reaching this helper means there is no concrete
        # off-target scope to persist.
        return []
    return [
        ScopeItem(
            repo_id=surface.repo_id,
            scope_item_id=_scope_id(surface.path, surface.symbol_id),
            path=surface.path,
            path_comparison_key=surface.path_comparison_key,
            symbol_id=surface.symbol_id,
            scope_kind="surface",
            policy=policy,  # type: ignore[arg-type]
            reason=f"user-declared {description} surface: {surface.surface_id}",
            evidence=surface.evidence,
            confidence="confirmed",
        )
    ]


def _surface_projection(
    surfaces: Iterable[ProtectedSurface],
    *surface_types: str,
) -> list[dict[str, Any]]:
    return [
        {
            "surface_id": surface.surface_id,
            "surface_kind": surface.surface_kind,
            "path": surface.path,
            "symbol_id": surface.symbol_id,
            "detection_status": surface.detection_status,
        }
        for surface in surfaces
        if surface.surface_kind in surface_types
    ]
