"""Read-only multi-repository workspace status helpers."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcgraph.core.query_engine import QueryEngine, SchemaVersionError
from arcgraph.core.schemas import ContextRequest, SCHEMA_VERSION
from arcgraph.interfaces.ci import run_ci_checks
from arcgraph.providers.context_provider import ContextProvider

WORKSPACE_STATUS_SCHEMA = "ArcGraphWorkspaceStatus"
WORKSPACE_STATUS_VERSION = 1
WORKSPACE_RESOLVE_SCHEMA = "ArcGraphWorkspaceResolve"
WORKSPACE_RESOLVE_VERSION = 1
WORKSPACE_CONTEXT_SCHEMA = "ArcGraphWorkspaceContext"
WORKSPACE_CONTEXT_VERSION = 1
WORKSPACE_MANIFEST_SCHEMA_VERSION = "1.0"
DEFAULT_WORKSPACE_CONFIG = "arcgraph.workspace.toml"
WORKSPACE_RESOLVE_KINDS = (
    "auto",
    "repo",
    "project",
    "package",
    "module",
    "path",
    "dependency",
)
WORKSPACE_CONTEXT_DETAIL_LEVELS = ("summary", "standard")
_DEPENDENCY_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")


@dataclass(frozen=True)
class WorkspaceRepository:
    id: str
    path: Path
    output_dir: Path
    role: str


@dataclass(frozen=True)
class WorkspaceManifest:
    name: str
    config_path: Path
    repositories: tuple[WorkspaceRepository, ...]


def workspace_status(
    config_path: str | Path, *, include_dependency_hints: bool = True
) -> dict[str, Any]:
    """Return a read-only status summary for repositories in a workspace."""

    manifest = load_workspace_manifest(config_path)
    repos = [_repository_status(repo) for repo in manifest.repositories]
    catalog = _workspace_catalog(repos)
    dependency_hints = (
        _dependency_hints(catalog.get("repositories", []))
        if include_dependency_hints
        else []
    )
    warnings = [
        *_workspace_warnings(repos),
        *(
            _dependency_hint_warnings(dependency_hints)
            if include_dependency_hints
            else []
        ),
    ]
    payload = {
        "schema": WORKSPACE_STATUS_SCHEMA,
        "version": WORKSPACE_STATUS_VERSION,
        "schema_version": SCHEMA_VERSION,
        "workspace_manifest_version": WORKSPACE_MANIFEST_SCHEMA_VERSION,
        "status": _workspace_status(repos),
        "name": manifest.name,
        "config_path": str(manifest.config_path),
        "root_dir": str(manifest.config_path.parent),
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": _workspace_summary(repos),
        "capability_summary": _capability_summary(repos),
        "catalog": catalog,
        "repositories": repos,
        "warnings": warnings,
    }
    if include_dependency_hints:
        payload["dependency_hints"] = dependency_hints
    return payload


def workspace_resolve(
    config_path: str | Path,
    target: str,
    *,
    kind: str = "auto",
    limit: int = 10,
    include_dependency_hints: bool = True,
) -> dict[str, Any]:
    """Resolve a workspace-level routing target to candidate repositories."""

    if kind not in WORKSPACE_RESOLVE_KINDS:
        raise RuntimeError(
            "ArcGraph workspace resolve kind must be one of "
            f"{', '.join(WORKSPACE_RESOLVE_KINDS)}, got {kind!r}."
        )
    query = target.strip()
    if not query:
        raise RuntimeError("ArcGraph workspace resolve requires a non-empty target.")
    capped_limit = max(1, int(limit))
    status_payload = workspace_status(config_path, include_dependency_hints=True)
    matches, resolve_warnings = _workspace_resolve_matches(status_payload, query, kind)
    matches = _sort_resolve_matches(matches)[:capped_limit]
    warnings = [
        *status_payload.get("warnings", []),
        *resolve_warnings,
    ]
    payload = {
        "schema": WORKSPACE_RESOLVE_SCHEMA,
        "version": WORKSPACE_RESOLVE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "workspace_manifest_version": WORKSPACE_MANIFEST_SCHEMA_VERSION,
        "status": _workspace_resolve_status(matches),
        "name": status_payload.get("name"),
        "config_path": status_payload.get("config_path"),
        "root_dir": status_payload.get("root_dir"),
        "generated_at": datetime.now(UTC).isoformat(),
        "query": {
            "target": query,
            "kind": kind,
            "limit": capped_limit,
        },
        "matches": matches,
        "suggested_commands": _suggested_workspace_commands(matches, query),
        "warnings": warnings,
    }
    if include_dependency_hints:
        payload["dependency_hints"] = _relevant_dependency_hints(
            status_payload.get("dependency_hints", []), query, matches
        )
    return payload


def workspace_context(
    config_path: str | Path,
    target: str,
    *,
    kind: str = "auto",
    limit: int = 10,
    detail_level: str = "summary",
    include_dependency_hints: bool = True,
) -> dict[str, Any]:
    """Return a read-only workspace context pack built from per-repo contexts."""

    if detail_level not in WORKSPACE_CONTEXT_DETAIL_LEVELS:
        raise RuntimeError(
            "ArcGraph workspace context detail level must be one of "
            f"{', '.join(WORKSPACE_CONTEXT_DETAIL_LEVELS)}, got {detail_level!r}."
        )
    query = target.strip()
    if not query:
        raise RuntimeError("ArcGraph workspace context requires a non-empty target.")

    capped_limit = max(1, int(limit))
    manifest = load_workspace_manifest(config_path)
    repo_by_id = {repo.id: repo for repo in manifest.repositories}
    resolve_payload = workspace_resolve(
        manifest.config_path,
        query,
        kind=kind,
        limit=capped_limit,
        include_dependency_hints=include_dependency_hints,
    )
    candidates, total_candidates = _workspace_context_candidates(resolve_payload)
    limited_candidates = candidates[:capped_limit]
    repo_contexts = [
        _workspace_repo_context(
            candidate,
            repo_by_id=repo_by_id,
            target=query,
            detail_level=detail_level,
        )
        for candidate in limited_candidates
    ]
    warnings = _workspace_context_warnings(resolve_payload, repo_contexts)
    return {
        "schema": WORKSPACE_CONTEXT_SCHEMA,
        "version": WORKSPACE_CONTEXT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "workspace_manifest_version": WORKSPACE_MANIFEST_SCHEMA_VERSION,
        "status": _workspace_context_status(repo_contexts, resolve_payload),
        "name": resolve_payload.get("name"),
        "config_path": resolve_payload.get("config_path"),
        "root_dir": resolve_payload.get("root_dir"),
        "generated_at": datetime.now(UTC).isoformat(),
        "query": {
            "target": query,
            "kind": kind,
            "limit": capped_limit,
            "detail_level": detail_level,
        },
        "resolve": resolve_payload,
        "repo_contexts": repo_contexts,
        "warnings": warnings,
        "truncation": {
            "repo_limit": capped_limit,
            "candidate_count": total_candidates,
            "returned_repo_contexts": len(repo_contexts),
            "truncated": total_candidates > len(repo_contexts),
        },
    }


def load_workspace_manifest(config_path: str | Path) -> WorkspaceManifest:
    """Load and validate an ``arcgraph.workspace.toml`` manifest."""

    path = Path(config_path).resolve()
    if not path.exists():
        raise RuntimeError(f"ArcGraph workspace manifest not found: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"Unable to parse workspace manifest {path}: {exc}") from exc

    schema_version = data.get("schema_version")
    if schema_version != WORKSPACE_MANIFEST_SCHEMA_VERSION:
        raise RuntimeError(
            "ArcGraph workspace manifest schema_version must be "
            f"{WORKSPACE_MANIFEST_SCHEMA_VERSION!r}, got {schema_version!r}."
        )
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuntimeError("ArcGraph workspace manifest requires a non-empty name.")

    raw_repos = data.get("repositories")
    if not isinstance(raw_repos, list) or not raw_repos:
        raise RuntimeError(
            "ArcGraph workspace manifest requires at least one [[repositories]] entry."
        )

    seen: set[str] = set()
    repos: list[WorkspaceRepository] = []
    for index, item in enumerate(raw_repos, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Workspace repository #{index} must be a table.")
        repo_id = _required_string(item, "id", index)
        if repo_id in seen:
            raise RuntimeError(f"Duplicate workspace repository id: {repo_id!r}.")
        seen.add(repo_id)
        repo_path = _resolve_manifest_path(
            path.parent, _required_string(item, "path", index)
        )
        output_raw = item.get("output_dir")
        output_dir = (
            _resolve_manifest_path(path.parent, output_raw)
            if isinstance(output_raw, str) and output_raw.strip()
            else repo_path / "output" / "arcgraph"
        )
        role_raw = item.get("role", "repository")
        role = (
            role_raw.strip()
            if isinstance(role_raw, str) and role_raw.strip()
            else "repository"
        )
        repos.append(
            WorkspaceRepository(
                id=repo_id,
                path=repo_path,
                output_dir=output_dir,
                role=role,
            )
        )
    return WorkspaceManifest(
        name=name.strip(), config_path=path, repositories=tuple(repos)
    )


def _repository_status(repo: WorkspaceRepository) -> dict[str, Any]:
    base = {
        "id": repo.id,
        "path": str(repo.path),
        "output_dir": str(repo.output_dir),
        "role": repo.role,
        "current_index": False,
        "status": "unavailable",
        "warnings": [],
    }
    if not repo.path.exists():
        return {
            **base,
            "reason": "path_missing",
            "warnings": [f"Repository path does not exist: {repo.path}"],
        }
    if not repo.path.is_dir():
        return {
            **base,
            "reason": "path_not_directory",
            "warnings": [f"Repository path is not a directory: {repo.path}"],
        }

    try:
        engine = QueryEngine(repo.output_dir)
        current = engine.current()
        stats = engine.stats()
        catalog = _repository_catalog(repo, engine, current)
    except FileNotFoundError as exc:
        return {
            **base,
            "status": "missing_index",
            "reason": "current_index_missing",
            "warnings": [str(exc)],
        }
    except (RuntimeError, SchemaVersionError, ValueError) as exc:
        return {
            **base,
            "reason": "index_unavailable",
            "warnings": [str(exc)],
        }

    freshness = current.get("freshness", {})
    freshness_status = str(freshness.get("status") or "unknown")
    status = "available" if freshness_status == "fresh" else "stale"
    repo_payload = {
        **base,
        "current_index": True,
        "status": status,
        "reason": None,
        "index_version": current.get("index_version"),
        "commit_sha": current.get("commit_sha"),
        "freshness": freshness,
        "capabilities": current.get("capabilities", {}),
        "counts": {
            "files": current.get("file_count", 0),
            "nodes": current.get("node_count", 0),
            "edges": current.get("edge_count", 0),
            "warnings": current.get("warning_count", 0),
            "semantic_facts": stats.get("counts", {}).get("semantic_facts", 0),
        },
        "catalog": catalog,
        "ci": _ci_summary(engine),
        "warnings": list(current.get("warnings", [])),
    }
    return repo_payload


def _repository_catalog(
    repo: WorkspaceRepository, engine: QueryEngine, current: dict[str, Any]
) -> dict[str, Any]:
    project_name = _string_or_none(engine.store.metadata.get("project_name"))
    project_version = _string_or_none(engine.store.metadata.get("project_version"))
    top_level_modules = _top_level_modules(engine)
    declared_dependencies = _declared_dependencies(repo.path)
    indexed_imports = _indexed_imports(engine)
    package_names = set(_name_aliases(project_name))
    for module in top_level_modules:
        package_names.update(_name_aliases(module))
    return {
        "repo_id": repo.id,
        "role": repo.role,
        "project_name": project_name,
        "project_version": project_version,
        "commit_sha": current.get("commit_sha"),
        "index_version": current.get("index_version"),
        "source_roots": current.get("source_roots", []),
        "capabilities": current.get("capabilities", {}),
        "top_level_modules": top_level_modules,
        "package_names": sorted(package_names),
        "declared_dependencies": declared_dependencies,
        "indexed_imports": indexed_imports,
    }


def _workspace_catalog(repos: list[dict[str, Any]]) -> dict[str, Any]:
    catalogs = [
        catalog
        for repo in repos
        for catalog in [repo.get("catalog")]
        if isinstance(catalog, dict)
    ]
    return {
        "repositories": catalogs,
        "summary": {
            "repositories": len(catalogs),
            "declared_dependencies": sum(
                len(repo.get("declared_dependencies", [])) for repo in catalogs
            ),
            "indexed_imports": sum(
                len(repo.get("indexed_imports", [])) for repo in catalogs
            ),
        },
    }


def _top_level_modules(engine: QueryEngine) -> list[str]:
    names: set[str] = set()
    with engine.store.connect() as conn:
        rows = conn.execute("""
            SELECT id, qualname, name FROM nodes
            WHERE kind = 'module'
            ORDER BY id
            """)
        for row in rows:
            module = _module_name_from_node(row["id"], row["qualname"], row["name"])
            top = _top_level_name(module)
            if top:
                names.add(top)
    return sorted(names)


def _indexed_imports(engine: QueryEngine) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    with engine.store.connect() as conn:
        rows = conn.execute("""
            SELECT source, target, confidence
            FROM edges
            WHERE kind = 'imports'
            ORDER BY source, target
            """)
        for row in rows:
            if not isinstance(row["target"], str) or not row["target"].startswith(
                "ext:"
            ):
                continue
            name = _top_level_import_name(row["target"])
            if not name:
                continue
            item = by_name.setdefault(
                name,
                {
                    "name": name,
                    "targets": set(),
                    "source_modules": set(),
                    "confidence": row["confidence"],
                },
            )
            item["targets"].add(row["target"])
            source = _top_level_import_name(row["source"])
            if source:
                item["source_modules"].add(source)
    return [
        {
            "name": name,
            "targets": sorted(item["targets"]),
            "source_modules": sorted(item["source_modules"]),
            "confidence": item["confidence"],
        }
        for name, item in sorted(by_name.items())
    ]


def _declared_dependencies(repo_path: Path) -> list[dict[str, str]]:
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists():
        return []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []

    dependencies: dict[str, dict[str, str]] = {}
    project = data.get("project")
    if isinstance(project, dict):
        for requirement in _string_list(project.get("dependencies")):
            _add_dependency(dependencies, requirement, "project.dependencies")
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group, values in optional.items():
                for requirement in _string_list(values):
                    _add_dependency(
                        dependencies,
                        requirement,
                        f"project.optional-dependencies.{group}",
                    )

    poetry = data.get("tool")
    poetry = poetry.get("poetry") if isinstance(poetry, dict) else None
    poetry_deps = poetry.get("dependencies") if isinstance(poetry, dict) else None
    if isinstance(poetry_deps, dict):
        for name in poetry_deps:
            if str(name).lower() != "python":
                _add_dependency(dependencies, str(name), "tool.poetry.dependencies")

    return [dependencies[name] for name in sorted(dependencies)]


def _dependency_hints(catalogs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exposed = _exposed_name_index(catalogs)
    hints: list[dict[str, Any]] = []
    for source in catalogs:
        repo_id = source.get("repo_id")
        if not isinstance(repo_id, str):
            continue
        for dependency in source.get("declared_dependencies", []):
            name = _string_or_none(dependency.get("name"))
            if not name:
                continue
            candidates = _matching_repos(exposed, name, exclude_repo_id=repo_id)
            hints.extend(
                _hint_for_candidates(
                    source_repo_id=repo_id,
                    candidates=candidates,
                    kind="declared_dependency",
                    evidence={
                        "type": "declared_dependency",
                        "dependency": name,
                        "source": dependency.get("source"),
                    },
                    confidence="confirmed",
                    reason=f"Declared dependency {name!r} matches a workspace repository.",
                )
            )
        for indexed_import in source.get("indexed_imports", []):
            name = _string_or_none(indexed_import.get("name"))
            if not name:
                continue
            candidates = _matching_repos(exposed, name, exclude_repo_id=repo_id)
            hints.extend(
                _hint_for_candidates(
                    source_repo_id=repo_id,
                    candidates=candidates,
                    kind="import_candidate",
                    evidence={
                        "type": "indexed_import",
                        "module": name,
                        "targets": indexed_import.get("targets", []),
                    },
                    confidence="heuristic",
                    reason=f"Indexed import {name!r} matches a workspace repository.",
                )
            )
    return sorted(
        hints,
        key=lambda item: (
            item["source_repo_id"],
            item["kind"],
            str(item.get("target_repo_id") or item.get("candidates")),
        ),
    )


def _hint_for_candidates(
    *,
    source_repo_id: str,
    candidates: list[str],
    kind: str,
    evidence: dict[str, Any],
    confidence: str,
    reason: str,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    if len(candidates) == 1:
        return [
            {
                "source_repo_id": source_repo_id,
                "target_repo_id": candidates[0],
                "kind": kind,
                "evidence": evidence,
                "confidence": confidence,
                "reason": reason,
            }
        ]
    return [
        {
            "source_repo_id": source_repo_id,
            "candidates": candidates,
            "kind": "ambiguous",
            "evidence": evidence,
            "confidence": "heuristic",
            "reason": f"{reason} Multiple workspace repositories match.",
        }
    ]


def _exposed_name_index(catalogs: list[dict[str, Any]]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for catalog in catalogs:
        repo_id = catalog.get("repo_id")
        if not isinstance(repo_id, str):
            continue
        names: set[str] = set()
        names.update(_name_aliases(_string_or_none(catalog.get("project_name"))))
        for module in catalog.get("top_level_modules", []):
            names.update(_name_aliases(_string_or_none(module)))
        for package in catalog.get("package_names", []):
            names.update(_name_aliases(_string_or_none(package)))
        for name in names:
            index.setdefault(name, set()).add(repo_id)
    return index


def _matching_repos(
    exposed: dict[str, set[str]], name: str, *, exclude_repo_id: str
) -> list[str]:
    matches: set[str] = set()
    for alias in _name_aliases(name):
        matches.update(exposed.get(alias, set()))
    matches.discard(exclude_repo_id)
    return sorted(matches)


def _dependency_hint_warnings(hints: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for hint in hints:
        if hint.get("kind") == "ambiguous":
            warnings.append(
                "workspace dependency hint is ambiguous: "
                f"{hint.get('source_repo_id')} -> {hint.get('candidates')}"
            )
    return warnings


def _workspace_resolve_matches(
    status_payload: dict[str, Any], target: str, kind: str
) -> tuple[list[dict[str, Any]], list[str]]:
    catalogs = _status_catalogs(status_payload)
    repos = _status_repositories(status_payload)
    hints = status_payload.get("dependency_hints", [])
    matches: list[dict[str, Any]] = []
    warnings: list[str] = []
    requested = {kind}
    if kind == "auto":
        requested = {"repo", "project", "package", "module", "path", "dependency"}

    if "repo" in requested:
        matches.extend(_resolve_repo_matches(repos, target))
    if "path" in requested:
        path_matches, path_warnings = _resolve_path_matches(
            repos,
            target,
            root_dir=_string_or_none(status_payload.get("root_dir")),
            warn_on_missing=kind == "path" or _looks_like_path(target),
        )
        matches.extend(path_matches)
        warnings.extend(path_warnings)
    if "project" in requested:
        matches.extend(
            _resolve_catalog_name_matches(
                catalogs,
                target,
                field="project_name",
                matched_field="project_name",
                confidence="confirmed",
                reason_template="{name!r} matches repository project name.",
            )
        )
    if "package" in requested:
        matches.extend(
            _resolve_catalog_list_matches(
                catalogs,
                target,
                field="package_names",
                matched_field="package_names",
                confidence="heuristic",
                reason_template="{name!r} matches repository package aliases.",
            )
        )
    if "module" in requested:
        matches.extend(
            _resolve_catalog_list_matches(
                catalogs,
                target,
                field="top_level_modules",
                matched_field="top_level_modules",
                confidence="heuristic",
                reason_template="{name!r} matches repository top-level modules.",
            )
        )
    if "dependency" in requested:
        matches.extend(_resolve_dependency_matches(catalogs, hints, target))

    merged = _merge_resolve_matches(matches)
    return merged, warnings


def _status_catalogs(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    catalog = status_payload.get("catalog")
    repositories = catalog.get("repositories") if isinstance(catalog, dict) else None
    if not isinstance(repositories, list):
        return []
    repo_by_id = {
        repo.get("id"): repo
        for repo in _status_repositories(status_payload)
        if isinstance(repo.get("id"), str)
    }
    catalogs = []
    for item in repositories:
        if not isinstance(item, dict):
            continue
        catalog_item = dict(item)
        repo = repo_by_id.get(item.get("repo_id"))
        if isinstance(repo, dict):
            catalog_item["_path"] = repo.get("path")
            catalog_item["_repo_status"] = repo.get("status")
            catalog_item["_warnings"] = repo.get("warnings", [])
        catalogs.append(catalog_item)
    return catalogs


def _status_repositories(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    repositories = status_payload.get("repositories")
    if not isinstance(repositories, list):
        return []
    return [item for item in repositories if isinstance(item, dict)]


def _resolve_repo_matches(
    repositories: list[dict[str, Any]], target: str
) -> list[dict[str, Any]]:
    matches = []
    for repo in repositories:
        repo_id = _string_or_none(repo.get("id"))
        if repo_id and repo_id.lower() == target.lower():
            matches.append(
                _repo_match(
                    repo,
                    matched_field="repo_id",
                    confidence="confirmed",
                    reason=f"{target!r} matches workspace repository id.",
                    evidence={"type": "repo_id", "value": repo_id},
                )
            )
    return matches


def _resolve_path_matches(
    repositories: list[dict[str, Any]],
    target: str,
    *,
    root_dir: str | None,
    warn_on_missing: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not root_dir:
        return [], ["Workspace root_dir is unavailable; path matching was skipped."]
    query_path = _resolve_manifest_path(Path(root_dir), target)
    matches: list[dict[str, Any]] = []
    for repo in repositories:
        repo_path_raw = _string_or_none(repo.get("path"))
        if not repo_path_raw:
            continue
        repo_path = Path(repo_path_raw).resolve()
        if _path_contains(repo_path, query_path):
            matches.append(
                _repo_match(
                    repo,
                    matched_field="path",
                    confidence="confirmed",
                    reason=f"{str(query_path)!r} is inside workspace repository.",
                    evidence={"type": "path", "path": str(query_path)},
                )
            )
    warnings = []
    if not matches and warn_on_missing:
        warnings.append(
            f"Workspace path target {target!r} did not match any repository path."
        )
    return matches, warnings


def _resolve_catalog_name_matches(
    catalogs: list[dict[str, Any]],
    target: str,
    *,
    field: str,
    matched_field: str,
    confidence: str,
    reason_template: str,
) -> list[dict[str, Any]]:
    candidates = [
        catalog
        for catalog in catalogs
        if _aliases_intersect(target, _string_or_none(catalog.get(field)))
    ]
    return _catalog_candidate_matches(
        candidates,
        target,
        matched_field=matched_field,
        confidence=confidence,
        reason=reason_template.format(name=target),
        evidence={"type": matched_field, "value": target},
    )


def _resolve_catalog_list_matches(
    catalogs: list[dict[str, Any]],
    target: str,
    *,
    field: str,
    matched_field: str,
    confidence: str,
    reason_template: str,
) -> list[dict[str, Any]]:
    candidates = []
    for catalog in catalogs:
        values = [
            value
            for value in catalog.get(field, [])
            if isinstance(value, str) and _aliases_intersect(target, value)
        ]
        if values:
            item = dict(catalog)
            item["_matched_values"] = sorted(set(values))
            candidates.append(item)
    return _catalog_candidate_matches(
        candidates,
        target,
        matched_field=matched_field,
        confidence=confidence,
        reason=reason_template.format(name=target),
        evidence={
            "type": matched_field,
            "value": target,
            "matched_values": sorted(
                {
                    value
                    for candidate in candidates
                    for value in candidate.get("_matched_values", [])
                }
            ),
        },
    )


def _resolve_dependency_matches(
    catalogs: list[dict[str, Any]], hints: list[dict[str, Any]], target: str
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    catalog_by_repo_id = {
        catalog["repo_id"]: catalog
        for catalog in catalogs
        if isinstance(catalog.get("repo_id"), str)
    }
    for hint in hints:
        evidence = hint.get("evidence")
        if not isinstance(evidence, dict):
            continue
        dependency_name = _string_or_none(evidence.get("dependency"))
        module_name = _string_or_none(evidence.get("module"))
        if not (
            _aliases_intersect(target, dependency_name)
            or _aliases_intersect(target, module_name)
        ):
            continue
        target_repo_id = _string_or_none(hint.get("target_repo_id"))
        if target_repo_id and target_repo_id in catalog_by_repo_id:
            matches.extend(
                _catalog_candidate_matches(
                    [catalog_by_repo_id[target_repo_id]],
                    target,
                    matched_field=(
                        "declared_dependencies"
                        if dependency_name
                        else "indexed_imports"
                    ),
                    confidence=str(hint.get("confidence") or "heuristic"),
                    reason=str(hint.get("reason") or "Dependency hint matches."),
                    evidence={
                        "type": "dependency_hint",
                        "hint": hint,
                    },
                )
            )
            continue
        candidates = [
            catalog_by_repo_id[repo_id]
            for repo_id in hint.get("candidates", [])
            if isinstance(repo_id, str) and repo_id in catalog_by_repo_id
        ]
        matches.extend(
            _catalog_candidate_matches(
                candidates,
                target,
                matched_field="dependency_hints",
                confidence="heuristic",
                reason=str(hint.get("reason") or "Dependency hint is ambiguous."),
                evidence={
                    "type": "dependency_hint",
                    "hint": hint,
                },
            )
        )
    return matches


def _catalog_candidate_matches(
    candidates: list[dict[str, Any]],
    target: str,
    *,
    matched_field: str,
    confidence: str,
    reason: str,
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    if len(candidates) == 1:
        return [
            _catalog_match(
                candidates[0],
                matched_field=matched_field,
                confidence=confidence,
                reason=reason,
                evidence=evidence,
            )
        ]
    return [
        {
            "repo_id": None,
            "role": None,
            "path": None,
            "project_name": None,
            "matched_fields": [matched_field],
            "confidence": "heuristic",
            "resolution_status": "ambiguous",
            "candidates": [_catalog_candidate_summary(item) for item in candidates],
            "reason": f"{reason} Multiple workspace repositories match.",
            "evidence": [evidence],
            "query": target,
        }
    ]


def _repo_match(
    repo: dict[str, Any],
    *,
    matched_field: str,
    confidence: str,
    reason: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "repo_id": repo.get("id"),
        "role": repo.get("role"),
        "path": repo.get("path"),
        "project_name": None,
        "matched_fields": [matched_field],
        "confidence": confidence,
        "resolution_status": "matched",
        "reason": reason,
        "evidence": [evidence],
        "repo_status": repo.get("status"),
        "warnings": repo.get("warnings", []),
    }


def _catalog_match(
    catalog: dict[str, Any],
    *,
    matched_field: str,
    confidence: str,
    reason: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "repo_id": catalog.get("repo_id"),
        "role": catalog.get("role"),
        "path": catalog.get("_path"),
        "project_name": catalog.get("project_name"),
        "matched_fields": [matched_field],
        "confidence": confidence,
        "resolution_status": "matched",
        "reason": reason,
        "evidence": [evidence],
        "commit_sha": catalog.get("commit_sha"),
        "index_version": catalog.get("index_version"),
        "repo_status": catalog.get("_repo_status"),
        "warnings": catalog.get("_warnings", []),
    }


def _catalog_candidate_summary(catalog: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo_id": catalog.get("repo_id"),
        "role": catalog.get("role"),
        "path": catalog.get("_path"),
        "project_name": catalog.get("project_name"),
        "commit_sha": catalog.get("commit_sha"),
        "index_version": catalog.get("index_version"),
    }


def _merge_resolve_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for match in matches:
        key: tuple[Any, ...]
        if match.get("resolution_status") == "ambiguous":
            key = (
                "ambiguous",
                tuple(
                    candidate.get("repo_id")
                    for candidate in match.get("candidates", [])
                ),
            )
        else:
            key = ("repo", match.get("repo_id"))
        existing = merged.get(key)
        if existing is None:
            item = dict(match)
            item["matched_fields"] = sorted(set(match.get("matched_fields", [])))
            item["evidence"] = list(match.get("evidence", []))
            merged[key] = item
            continue
        existing["matched_fields"] = sorted(
            set(existing.get("matched_fields", []))
            | set(match.get("matched_fields", []))
        )
        existing["evidence"].extend(match.get("evidence", []))
        existing["confidence"] = _stronger_confidence(
            str(existing.get("confidence") or "heuristic"),
            str(match.get("confidence") or "heuristic"),
        )
        for field in (
            "role",
            "path",
            "project_name",
            "commit_sha",
            "index_version",
            "repo_status",
        ):
            if existing.get(field) in (None, "") and match.get(field) not in (
                None,
                "",
            ):
                existing[field] = match[field]
        existing["warnings"] = sorted(
            set(existing.get("warnings", [])) | set(match.get("warnings", []))
        )
        if match.get("reason") and match.get("reason") not in existing["reason"]:
            existing["reason"] = f"{existing['reason']} {match['reason']}"
    return list(merged.values())


def _sort_resolve_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        matches,
        key=lambda item: (
            _confidence_rank(str(item.get("confidence") or "heuristic")),
            item.get("resolution_status") == "ambiguous",
            str(item.get("repo_id") or item.get("candidates") or ""),
        ),
    )


def _workspace_resolve_status(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return "not_found"
    if all(match.get("resolution_status") == "ambiguous" for match in matches):
        return "ambiguous"
    return "matched"


def _relevant_dependency_hints(
    hints: Any, target: str, matches: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(hints, list):
        return []
    repo_ids = {
        repo_id
        for match in matches
        for repo_id in _match_repo_ids(match)
        if isinstance(repo_id, str)
    }
    relevant: list[dict[str, Any]] = []
    for hint in hints:
        if not isinstance(hint, dict):
            continue
        evidence = hint.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        if (
            _aliases_intersect(target, _string_or_none(evidence.get("dependency")))
            or _aliases_intersect(target, _string_or_none(evidence.get("module")))
            or _string_or_none(hint.get("target_repo_id")) in repo_ids
            or any(candidate in repo_ids for candidate in hint.get("candidates", []))
        ):
            relevant.append(hint)
    return relevant


def _suggested_workspace_commands(
    matches: list[dict[str, Any]], target: str
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in matches:
        for repo in _match_repo_summaries(match):
            repo_id = _string_or_none(repo.get("repo_id"))
            if not repo_id or repo_id in seen:
                continue
            seen.add(repo_id)
            quoted = _quote_command_arg(target)
            suggestions.append(
                {
                    "repo_id": repo_id,
                    "cwd": repo.get("path"),
                    "commands": [
                        "arcgraph current",
                        f"arcgraph symbol {quoted}",
                        f"arcgraph context {quoted}",
                        f"arcgraph explain {quoted}",
                    ],
                }
            )
    return suggestions


def _workspace_context_candidates(
    resolve_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in resolve_payload.get("matches", []):
        if not isinstance(match, dict):
            continue
        if match.get("resolution_status") == "ambiguous":
            for candidate in match.get("candidates", []):
                if not isinstance(candidate, dict):
                    continue
                repo_id = _string_or_none(candidate.get("repo_id"))
                if not repo_id or repo_id in seen:
                    continue
                seen.add(repo_id)
                candidates.append(
                    {
                        "repo_id": repo_id,
                        "role": candidate.get("role"),
                        "path": candidate.get("path"),
                        "project_name": candidate.get("project_name"),
                        "commit_sha": candidate.get("commit_sha"),
                        "index_version": candidate.get("index_version"),
                        "match_status": "ambiguous_candidate",
                        "matched_fields": match.get("matched_fields", []),
                        "confidence": match.get("confidence"),
                        "reason": match.get("reason"),
                        "evidence": match.get("evidence", []),
                        "warnings": match.get("warnings", []),
                    }
                )
            continue

        repo_id = _string_or_none(match.get("repo_id"))
        if not repo_id or repo_id in seen:
            continue
        seen.add(repo_id)
        candidates.append(
            {
                "repo_id": repo_id,
                "role": match.get("role"),
                "path": match.get("path"),
                "project_name": match.get("project_name"),
                "commit_sha": match.get("commit_sha"),
                "index_version": match.get("index_version"),
                "match_status": str(match.get("resolution_status") or "matched"),
                "matched_fields": match.get("matched_fields", []),
                "confidence": match.get("confidence"),
                "reason": match.get("reason"),
                "evidence": match.get("evidence", []),
                "warnings": match.get("warnings", []),
            }
        )
    return candidates, len(candidates)


def _workspace_repo_context(
    candidate: dict[str, Any],
    *,
    repo_by_id: dict[str, WorkspaceRepository],
    target: str,
    detail_level: str,
) -> dict[str, Any]:
    repo_id = _string_or_none(candidate.get("repo_id"))
    base = {
        "repo_id": repo_id,
        "repo_path": candidate.get("path"),
        "role": candidate.get("role"),
        "project_name": candidate.get("project_name"),
        "match_status": candidate.get("match_status") or "matched",
        "match": {
            "matched_fields": candidate.get("matched_fields", []),
            "confidence": candidate.get("confidence"),
            "reason": candidate.get("reason"),
            "evidence": candidate.get("evidence", []),
        },
        "warnings": list(candidate.get("warnings", [])),
    }
    if not repo_id:
        return {
            **base,
            "status": "unavailable",
            "error": "Workspace context candidate is missing repo_id.",
        }
    repo = repo_by_id.get(repo_id)
    if repo is None:
        return {
            **base,
            "status": "unavailable",
            "error": f"Workspace repository {repo_id!r} is not present in manifest.",
        }
    if not repo.path.exists():
        return {
            **base,
            "repo_path": str(repo.path),
            "status": "unavailable",
            "error": f"Repository path does not exist: {repo.path}",
        }

    try:
        engine = QueryEngine(repo.output_dir)
        request = ContextRequest(
            targets=[target],
            detail_level=detail_level,  # type: ignore[arg-type]
            include_source=False,
        )
        context = ContextProvider(engine).get_context(request)
    except FileNotFoundError as exc:
        return {
            **base,
            "repo_path": str(repo.path),
            "status": "unavailable",
            "error": str(exc),
        }
    except (RuntimeError, SchemaVersionError, ValueError) as exc:
        return {
            **base,
            "repo_path": str(repo.path),
            "status": "unavailable",
            "error": str(exc),
        }
    return {
        **base,
        "repo_path": str(repo.path),
        "status": "available",
        "context": _redact_workspace_context_payload(context),
    }


def _workspace_context_status(
    repo_contexts: list[dict[str, Any]], resolve_payload: dict[str, Any]
) -> str:
    if not repo_contexts:
        return (
            "not_found" if resolve_payload.get("status") == "not_found" else "partial"
        )
    available = [
        item for item in repo_contexts if isinstance(item.get("context"), dict)
    ]
    if len(available) == len(repo_contexts):
        return "available"
    return "partial"


def _workspace_context_warnings(
    resolve_payload: dict[str, Any], repo_contexts: list[dict[str, Any]]
) -> list[str]:
    warnings = [
        warning
        for warning in resolve_payload.get("warnings", [])
        if isinstance(warning, str)
    ]
    if not repo_contexts:
        warnings.append("Workspace context found no repositories for the target.")
    for item in repo_contexts:
        repo_id = item.get("repo_id") or "unknown"
        for warning in item.get("warnings", []):
            if isinstance(warning, str):
                warnings.append(f"{repo_id}: {warning}")
        error = item.get("error")
        if isinstance(error, str):
            warnings.append(f"{repo_id}: {error}")
    return warnings


def _redact_workspace_context_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_workspace_context_payload(item)
            for key, item in value.items()
            if key not in {"properties", "snippet", "source_snippet"}
        }
    if isinstance(value, list):
        return [_redact_workspace_context_payload(item) for item in value]
    return value


def _match_repo_ids(match: dict[str, Any]) -> list[str]:
    repo_id = _string_or_none(match.get("repo_id"))
    if repo_id:
        return [repo_id]
    return [
        repo_id
        for candidate in match.get("candidates", [])
        for repo_id in [_string_or_none(candidate.get("repo_id"))]
        if repo_id
    ]


def _match_repo_summaries(match: dict[str, Any]) -> list[dict[str, Any]]:
    repo_id = _string_or_none(match.get("repo_id"))
    if repo_id:
        return [
            {
                "repo_id": repo_id,
                "path": match.get("path"),
            }
        ]
    return [
        candidate
        for candidate in match.get("candidates", [])
        if isinstance(candidate, dict)
    ]


def _aliases_intersect(query: str, value: str | None) -> bool:
    if not value:
        return False
    return bool(_name_aliases(query) & _name_aliases(value))


def _path_contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _looks_like_path(value: str) -> bool:
    return any(separator in value for separator in ("/", "\\")) or value.endswith(".py")


def _stronger_confidence(left: str, right: str) -> str:
    return left if _confidence_rank(left) <= _confidence_rank(right) else right


def _confidence_rank(confidence: str) -> int:
    ranks = {
        "confirmed": 0,
        "inferred": 1,
        "heuristic": 2,
        "runtime-only": 3,
        "unresolved": 4,
    }
    return ranks.get(confidence, 5)


def _quote_command_arg(value: str) -> str:
    escaped = value.replace('"', '\\"')
    return f'"{escaped}"'


def _add_dependency(
    dependencies: dict[str, dict[str, str]], requirement: str, source: str
) -> None:
    name = _dependency_name(requirement)
    if not name:
        return
    dependencies.setdefault(
        _canonical_package_name(name),
        {
            "name": name,
            "normalized_name": _canonical_package_name(name),
            "requirement": requirement,
            "source": source,
        },
    )


def _dependency_name(requirement: str) -> str | None:
    match = _DEPENDENCY_NAME_RE.match(requirement)
    if not match:
        return None
    return match.group(1)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _module_name_from_node(
    node_id: str | None, qualname: str | None, name: str | None
) -> str | None:
    candidate = _string_or_none(qualname) or _string_or_none(name)
    if candidate:
        return candidate
    if isinstance(node_id, str) and node_id.startswith("mod:"):
        return node_id.removeprefix("mod:")
    return None


def _top_level_import_name(target: Any) -> str | None:
    if not isinstance(target, str) or not target.strip():
        return None
    if target.startswith("mod:"):
        return _top_level_name(target.removeprefix("mod:"))
    if target.startswith("ext:"):
        return _top_level_name(target.removeprefix("ext:"))
    return None


def _top_level_name(name: str | None) -> str | None:
    if not name:
        return None
    head = name.split(".", 1)[0].strip()
    return head or None


def _name_aliases(name: str | None) -> set[str]:
    if not name:
        return set()
    normalized = _canonical_package_name(name)
    module_form = normalized.replace("-", "_")
    raw = name.strip().lower()
    return {value for value in {raw, normalized, module_form} if value}


def _canonical_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _ci_summary(engine: QueryEngine) -> dict[str, Any]:
    try:
        ci = run_ci_checks(engine)
    except Exception as exc:  # pragma: no cover - defensive status payload
        return {
            "status": "unavailable",
            "summary": {},
            "warning": f"Unable to compute CI summary: {exc}",
        }
    return {
        "status": ci.get("status"),
        "summary": ci.get("summary", {}),
    }


def _workspace_status(repos: list[dict[str, Any]]) -> str:
    if not repos or all(repo.get("status") == "unavailable" for repo in repos):
        return "unavailable"
    return (
        "available"
        if all(repo.get("status") == "available" for repo in repos)
        else "partial"
    )


def _workspace_summary(repos: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "repositories": len(repos),
        "available": 0,
        "stale": 0,
        "missing_index": 0,
        "unavailable": 0,
        "warnings": 0,
    }
    for repo in repos:
        status = str(repo.get("status") or "unavailable")
        if status in counts:
            counts[status] += 1
        else:
            counts["unavailable"] += 1
        counts["warnings"] += len(repo.get("warnings", []))
        ci = repo.get("ci")
        if isinstance(ci, dict) and ci.get("warning"):
            counts["warnings"] += 1
    return counts


def _capability_summary(repos: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for repo in repos:
        capabilities = repo.get("capabilities")
        if not isinstance(capabilities, dict):
            continue
        for name, value in capabilities.items():
            bucket = summary.setdefault(str(name), {})
            status = str(value)
            bucket[status] = bucket.get(status, 0) + 1
    return summary


def _workspace_warnings(repos: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for repo in repos:
        repo_id = repo.get("id", "unknown")
        for warning in repo.get("warnings", []):
            warnings.append(f"{repo_id}: {warning}")
        ci = repo.get("ci")
        if isinstance(ci, dict) and ci.get("warning"):
            warnings.append(f"{repo_id}: {ci['warning']}")
    return warnings


def _required_string(item: dict[str, Any], key: str, index: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Workspace repository #{index} requires non-empty {key}.")
    return value.strip()


def _resolve_manifest_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()
