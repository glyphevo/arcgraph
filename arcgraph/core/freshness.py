"""Freshness helpers for query responses."""

from __future__ import annotations

from pathlib import Path

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import (
    AUTOMATIC_SOURCE_ROOT_STRATEGIES,
    TYPESCRIPT_FILE_EXTENSIONS,
    TYPESCRIPT_FRONTEND_NAME,
    FileScanner,
    SourceRoot,
    infer_source_roots,
    logical_module_name,
    detect_source_roots,
)
from arcgraph.core.project_environment import dependency_environment_changed
from arcgraph.core.schemas import Freshness
from arcgraph.core.semantic import COMPAT_FRONTEND_NAME


def fresh() -> Freshness:
    return Freshness(status="fresh", stale=False, stale_files=[])


def compute_freshness(store: GraphStoreReader) -> Freshness:
    try:
        repo_root = Path(str(store.metadata["repo_root"]))
        from arcgraph.semantic.runtime import semantic_core_enabled

        semantic = store.metadata.get("toolchain_status", {}).get("semantic_core", {})
        if semantic_core_enabled(repo_root) != (
            semantic.get("status", "disabled") != "disabled"
        ):
            return Freshness(
                status="stale", stale=True, reason="semantic_core_configuration_changed"
            )
        if semantic.get("status") in ("active", "partial", "unavailable"):
            from arcgraph.semantic.build import freshness_reason

            reason = freshness_reason(store, repo_root)
            if reason:
                return Freshness(status="stale", stale=True, reason=reason)
        if dependency_environment_changed(store.metadata, repo_root):
            return Freshness(
                status="stale", stale=True, reason="dependency_environment_changed"
            )
        source_roots = _source_roots_from_metadata(store.metadata)
        detection = store.metadata.get("source_root_detection") or {}
        if (
            detection.get("strategy") in AUTOMATIC_SOURCE_ROOT_STRATEGIES
            and detect_source_roots(repo_root).roots != source_roots
        ):
            # The configured search scope is part of the dependency environment.
            return Freshness(
                status="stale", stale=True, reason="dependency_environment_changed"
            )
        indexed_files = store.read_files()
        file_extensions = _indexed_file_extensions(
            indexed_files, store.metadata.get("language_tiers", {})
        )
        current_files = FileScanner(
            repo_root,
            source_roots,
            file_extensions=file_extensions,
        ).scan()
    except Exception as exc:
        return Freshness(
            status="unknown", stale=True, reason=f"freshness_check_failed:{exc}"
        )

    current_by_path = {file.path: file for file in current_files}
    indexed_by_path = {file.path: file for file in indexed_files}

    stale_files: list[str] = []
    stale_modules: list[str] = []
    reasons: set[str] = set()

    for path, indexed in indexed_by_path.items():
        current = current_by_path.get(path)
        if current is None:
            stale_files.append(path)
            stale_modules.append(logical_module_name(indexed.module))
            reasons.add("deleted")
        elif current.file_hash != indexed.file_hash:
            stale_files.append(path)
            stale_modules.append(logical_module_name(indexed.module))
            reasons.add("modified")

    for path, current in current_by_path.items():
        if path not in indexed_by_path:
            stale_files.append(path)
            stale_modules.append(logical_module_name(current.module))
            reasons.add("added")

    if stale_files:
        return Freshness(
            status="stale",
            stale=True,
            stale_files=sorted(set(stale_files)),
            stale_modules=sorted(set(stale_modules)),
            reason=",".join(sorted(reasons)),
        )

    return fresh()


def _indexed_file_extensions(
    indexed_files: list[object], language_tiers: dict[str, dict[str, str]]
) -> tuple[str, ...]:
    extensions: list[str] = []
    for file in indexed_files:
        path = getattr(file, "path", "")
        suffix = Path(str(path)).suffix
        if suffix and suffix not in extensions:
            extensions.append(suffix)
    # Exclude can hide every file of a language (or one extension lane).
    # Recover the complete built-in frontend scope from its persisted
    # declaration, rather than concluding that absent files are unsupported.
    for tier in language_tiers.values():
        frontend = tier.get("frontend")
        if tier.get("status") != "available":
            continue
        declared = (
            (".py",)
            if frontend == COMPAT_FRONTEND_NAME
            else (
                TYPESCRIPT_FILE_EXTENSIONS
                if frontend == TYPESCRIPT_FRONTEND_NAME
                else ()
            )
        )
        for extension in declared:
            if extension not in extensions:
                extensions.append(extension)
    return tuple(extensions) or (".py",)


def _source_roots_from_metadata(metadata: dict[str, object]) -> tuple[SourceRoot, ...]:
    specs = metadata.get("source_root_specs")
    if isinstance(specs, list) and specs:
        return tuple(
            SourceRoot(
                path=str(entry.get("path", "")),
                module_prefix=str(entry.get("module_prefix", "")),
            )
            for entry in specs
            if isinstance(entry, dict) and entry.get("path") is not None
        )
    return tuple(infer_source_roots(list(metadata.get("source_roots", []))))
