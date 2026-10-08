"""Incremental reindexing for ArcGraph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcgraph.adapters.pytest_collection import PytestCollection
from arcgraph.analyzers.similarity import (
    PROFILE_ALGORITHM as PYTHON_SIMILARITY_PROFILE_ALGORITHM,
)
from arcgraph.core.cleanup import prune_arcgraph_output_after_publish
from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.core.ids import diagnostic_id
from arcgraph.core.language_tiers import build_language_tiers
from arcgraph.core.merge import EvidenceMergeEngine
from arcgraph.core.metadata import current_commit, make_index_version
from arcgraph.core.operation_lock import arcgraph_operation_lock
from arcgraph.core.scanner import (
    TYPESCRIPT_EMIT_SKIP_WARNING_KIND,
    FileScanner,
    SourceRoot,
    infer_source_roots,
)
from arcgraph.core.semantic import COMPAT_FRONTEND_NAME
from arcgraph.core.schemas import (
    SCHEMA_VERSION,
    BuildWarning,
    Edge,
    FileRecord,
    IndexMetadata,
    Node,
    SemanticDiagnostic,
)
from arcgraph.core.structural import generate_structural_hierarchy
from arcgraph.pipeline.frontends import python_compat_pass_dispatcher
from arcgraph.pipeline.indexer import ArcGraphIndexer, IndexContent
from arcgraph.pipeline.typescript_frontend import (
    TYPESCRIPT_FILE_EXTENSIONS,
    TYPESCRIPT_FRONTEND_NAME,
    TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM,
    typescript_similarity_profile_count,
)

CURRENT_SIMILARITY_ALGORITHMS = frozenset(
    {
        PYTHON_SIMILARITY_PROFILE_ALGORITHM,
        TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM,
    }
)

TYPESCRIPT_SIMILARITY_WARNING_KINDS = frozenset(
    {
        "similarity_bucket_approximated",
        "similarity_edges_capped",
    }
)
PYTHON_SIMILARITY_WARNING_KINDS = frozenset({"similarity_bucket_skipped"})


class FullBuildRequired(RuntimeError):
    """Incremental publication is unsafe and must be replaced by a full build."""


# Frontend stamps republished onto a rescued config node. Prefer the surviving
# edge (TypeScript stamps them there) and fall back to the source node.
_SHARED_CONFIG_FRONTEND_PROPERTY_KEYS = (
    "frontend_name",
    "frontend_version",
    "language",
)


def recover_source_roots_from_metadata(
    metadata: dict[str, Any],
) -> tuple[SourceRoot, ...]:
    """Recover SourceRoot(path, module_prefix) records from stored metadata.

    Prefers ``source_root_specs`` (which preserves module_prefix) over legacy
    ``source_roots`` (path-only). If neither exists, returns an empty tuple so
    callers can choose their own fallback strategy.
    """
    specs = metadata.get("source_root_specs")
    if specs and isinstance(specs, list):
        return tuple(
            SourceRoot(
                path=entry.get("path", ""),
                module_prefix=entry.get("module_prefix", ""),
            )
            for entry in specs
            if isinstance(entry, dict) and entry.get("path") is not None
        )

    legacy = metadata.get("source_roots")
    if legacy and isinstance(legacy, list):
        return tuple(infer_source_roots(list(legacy)))

    return ()


class ArcGraphReindexer:
    def __init__(
        self,
        repo_root: Path,
        output_dir: Path | None = None,
        source_roots: list[SourceRoot] | tuple[SourceRoot, ...] | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.output_dir = (
            output_dir or self.repo_root / "output" / "arcgraph"
        ).resolve()
        self.source_roots = tuple(source_roots) if source_roots else None

    def reindex_changed(self, *, cleanup_after_publish: bool = True) -> dict[str, Any]:
        with arcgraph_operation_lock(self.output_dir):
            result = self._reindex_changed_inner()
            if result.get("status") == "reindexed" and cleanup_after_publish:
                result["cleanup"] = prune_arcgraph_output_after_publish(self.output_dir)
            return result

    def _reindex_changed_inner(self) -> dict[str, Any]:
        store = GraphStoreReader.from_current(self.output_dir)
        if store.metadata.get("schema_version") != SCHEMA_VERSION:
            raise FullBuildRequired(
                f"ArcGraph schema mismatch: index has {store.metadata.get('schema_version')}, "
                f"runtime expects {SCHEMA_VERSION}. Run ArcGraph build before incremental reindex."
            )
        source_roots = self.source_roots or self._recover_source_roots(store.metadata)

        enable_v2_call_resolution = self._receiver_resolution_enabled(store.metadata)
        indexer = ArcGraphIndexer(
            self.repo_root,
            self.output_dir,
            source_roots,
            enable_v2_call_resolution=enable_v2_call_resolution,
        )
        active_frontends = indexer._build_plan()
        scanner = FileScanner(
            self.repo_root,
            source_roots,
            file_extensions=indexer._file_extensions_for(active_frontends),
        )
        current_files = scanner.scan()
        old_files = store.read_files()

        delta = self._delta(old_files, current_files)
        changed_paths = set(delta["added"] + delta["modified"] + delta["deleted"])
        old_warnings = store.read_warnings()
        # Skipped emit artifacts never enter the scanned file set, so adding
        # or removing one produces no file delta; the scanner's summary
        # warning is the only signal and counts as a change of its own.
        if not changed_paths and not self._emit_skip_summary_changed(
            scanner.warnings, old_warnings
        ):
            return {
                "schema_version": store.metadata.get("schema_version"),
                "index_version": store.metadata.get("index_version"),
                "status": "unchanged",
                "changed": delta,
                "build_dir": str(store.sqlite_path.parent),
            }
        self._assert_incremental_module_identities_stable(old_files, current_files)

        old_nodes = store.read_nodes()
        old_edges = store.read_edges()
        if any(path.endswith(".py") for path in changed_paths):
            # Conditional constructor/type evidence may change in a provider
            # while the consumer text stays identical. Include unresolved
            # inputs too, so a newly available class can recover the join.
            changed_paths.update(
                node.path
                for node in old_nodes
                if node.path
                and (
                    node.properties.get("conditional_type_input")
                    or node.properties.get("union_type_input")
                    or node.properties.get("inherited_type_input")
                    or node.properties.get("comprehension_type_input")
                    or node.properties.get("imported_type_input")
                )
            )
        self._assert_similarity_algorithms_compatible(
            nodes=old_nodes,
            edges=old_edges,
        )

        # Pytest providers do not require import edges. Refresh collected tests,
        # conftests and known provider files, rather than every Python file or any
        # source containing the word "fixture". Type-input refresh above remains
        # conservative and independent of pytest.
        if any(path.endswith(".py") for path in changed_paths):
            collection = PytestCollection.from_root(self.repo_root)
            provider_paths = {
                node.path for node in old_nodes if node.kind == "pytest_fixture"
            }
            changed_paths.update(
                file.path
                for file in [*old_files, *current_files]
                if file.path.endswith(".py")
                and (
                    collection.file_matches(file.path)
                    or Path(file.path).name == "conftest.py"
                    or file.path in provider_paths
                )
            )
        changed_existing_files = [
            file for file in current_files if file.path in changed_paths
        ]
        typescript_changed = any(
            path.endswith(TYPESCRIPT_FILE_EXTENSIONS) for path in changed_paths
        )
        python_changed = any(path.endswith(".py") for path in changed_paths)
        changed_old_node_ids = {
            node.id for node in old_nodes if node.path in changed_paths
        }
        previous_typescript_similarity_buckets: set[str] = set()
        previous_python_similarity_buckets: set[str] = set()
        for node in old_nodes:
            profile = node.properties.get("similarity")
            if not isinstance(profile, dict) or profile.get("bucket") is None:
                continue
            feature_targets = {
                value
                for key in ("call_targets", "resource_targets")
                for value in profile.get(key, [])
                if isinstance(value, str)
            }
            affected = (
                node.path in changed_paths or feature_targets & changed_old_node_ids
            )
            if not affected:
                continue
            frontend_name = node.properties.get("frontend_name")
            algorithm = profile.get("algorithm")
            if frontend_name == TYPESCRIPT_FRONTEND_NAME:
                previous_typescript_similarity_buckets.add(str(profile["bucket"]))
            elif (
                frontend_name == COMPAT_FRONTEND_NAME
                or algorithm == PYTHON_SIMILARITY_PROFILE_ALGORITHM
            ):
                previous_python_similarity_buckets.add(str(profile["bucket"]))
        refresh_typescript_similarity = typescript_changed or bool(
            previous_typescript_similarity_buckets
        )
        refresh_python_similarity = python_changed or bool(
            previous_python_similarity_buckets
        )
        if refresh_typescript_similarity:
            self._assert_typescript_incremental_compatibility(
                metadata=store.metadata,
                nodes=old_nodes,
                edges=old_edges,
                # Scorer-only refreshes reuse persisted profiles and never
                # rebuild Express mount context from a partial file set.
                check_express_mount_context=typescript_changed,
            )
        unchanged_context_nodes = [
            node
            for node in old_nodes
            if node.kind != "external_package"
            and (node.path is None or node.path not in changed_paths)
        ]
        # Shared config nodes are keyed by id, not by owning file, but the
        # extractor stamps each with the first referencing file's path. When
        # that file changes, the path-based merge would drop the node even if
        # unchanged files still reference it. Rescue those nodes up front so
        # both the similarity scorer's valid-target set and the merge treat
        # them as alive.
        rescued_shared_nodes = self._rescued_shared_config_nodes(
            old_nodes=old_nodes,
            old_edges=old_edges,
            changed_paths=changed_paths,
            surviving_node_ids={node.id for node in unchanged_context_nodes},
        )
        unchanged_context_nodes = [*unchanged_context_nodes, *rescued_shared_nodes]
        current_module_names = {file.module for file in current_files}
        # Candidate selection and strongest-neighbour capping depend on every
        # profile in a TypeScript bucket. Force the frontend even for a
        # deletion-only change so it can rebuild that derived state from the
        # unchanged profiles supplied as context. Cross-frontend feature
        # targets (for example a TS profile calling a Python route) must also
        # force a rescore when only the other language changed.
        changed_content = indexer.analyze_files(
            changed_existing_files,
            warnings=list(scanner.warnings),
            module_names=current_module_names,
            call_context_nodes=unchanged_context_nodes,
            frontends=active_frontends,
            force_frontend_names=(
                {
                    frontend_name
                    for frontend_name, refresh in (
                        (TYPESCRIPT_FRONTEND_NAME, refresh_typescript_similarity),
                        (COMPAT_FRONTEND_NAME, refresh_python_similarity),
                    )
                    if refresh
                }
                or None
            ),
            incremental_similarity_buckets_by_frontend={
                TYPESCRIPT_FRONTEND_NAME: (
                    previous_typescript_similarity_buckets
                    if refresh_typescript_similarity
                    else None
                ),
                COMPAT_FRONTEND_NAME: (
                    previous_python_similarity_buckets
                    if refresh_python_similarity
                    else None
                ),
            },
        )
        if refresh_typescript_similarity:
            extractor_failure = next(
                (
                    warning
                    for warning in changed_content.warnings
                    if warning.kind == "typescript_frontend_unavailable"
                    and warning.frontend_name == TYPESCRIPT_FRONTEND_NAME
                ),
                None,
            )
            if extractor_failure is not None:
                raise RuntimeError(
                    "Incremental TypeScript reindex aborted because the "
                    "TypeScript extractor was unavailable; the current index was "
                    "not modified. Restore the TypeScript toolchain and retry, or "
                    "run a full ArcGraph build for complete coverage. "
                    f"Extractor detail: {extractor_failure.message}"
                )

        # Only a frontend that actually ran with the refresh instruction above
        # can regenerate these buckets. Declaring a bucket refreshed when its
        # frontend was skipped makes `_merge_content` drop the old edges with
        # nothing to replace them, losing them from the index permanently.
        refreshed_typescript_similarity_buckets = {
            *(
                previous_typescript_similarity_buckets
                if refresh_typescript_similarity
                else frozenset()
            ),
            *(
                str(profile.get("bucket"))
                for node in changed_content.nodes
                if node.properties.get("frontend_name") == TYPESCRIPT_FRONTEND_NAME
                and isinstance((profile := node.properties.get("similarity")), dict)
                and profile.get("bucket") is not None
            ),
        }
        refreshed_python_similarity_buckets = {
            *(
                previous_python_similarity_buckets
                if refresh_python_similarity
                else frozenset()
            ),
            *(
                str(profile.get("bucket"))
                for node in changed_content.nodes
                if isinstance((profile := node.properties.get("similarity")), dict)
                and profile.get("bucket") is not None
                and (
                    node.properties.get("frontend_name") == COMPAT_FRONTEND_NAME
                    or profile.get("algorithm") == PYTHON_SIMILARITY_PROFILE_ALGORITHM
                )
            ),
        }
        content = self._merge_content(
            current_files=current_files,
            old_nodes=old_nodes,
            old_edges=old_edges,
            old_warnings=old_warnings,
            changed_content=changed_content,
            changed_paths=changed_paths,
            refreshed_typescript_similarity_buckets=(
                refreshed_typescript_similarity_buckets
            ),
            refreshed_python_similarity_buckets=refreshed_python_similarity_buckets,
            rescued_shared_nodes=rescued_shared_nodes,
        )
        content.adapter_metrics = self._preserve_incremental_summary_metrics(
            store.metadata.get("adapter_metrics"),
            content.adapter_metrics,
        )
        content.precision_metrics = self._preserve_incremental_summary_metrics(
            store.metadata.get("precision_metrics"),
            content.precision_metrics,
        )
        content.coverage_metrics = self._preserve_incremental_summary_metrics(
            store.metadata.get("coverage_metrics"),
            content.coverage_metrics,
        )
        content.runtime_metrics = self._preserve_incremental_summary_metrics(
            store.metadata.get("runtime_metrics"),
            content.runtime_metrics,
        )
        content.nodes = indexer.dedupe_nodes(content.nodes)
        content.edges = indexer.dedupe_edges(content.edges)

        # Strip old structural nodes/edges BEFORE merge so new structural
        # goes through the same pipeline as code nodes — invariant: written
        # nodes/edges, semantic_facts, merge_metrics come from the same graph.
        _STRUCTURAL_NODE_KINDS = frozenset({"source_root", "package"})
        content.nodes = [
            n for n in content.nodes if n.kind not in _STRUCTURAL_NODE_KINDS
        ]
        content.edges = [e for e in content.edges if e.kind != "contains"]

        # Regenerate structural hierarchy from current files
        file_dicts = [
            {
                "path": f.path,
                "source_root": f.source_root,
                "module": f.module,
                "is_package": f.is_package,
            }
            for f in content.files
        ]
        struct_nodes, struct_edges = generate_structural_hierarchy(
            source_roots,
            files=file_dicts,
            existing_nodes=content.nodes,
        )
        content.nodes.extend(struct_nodes)
        content.edges.extend(struct_edges)

        commit_sha = current_commit(self.repo_root)
        metadata = IndexMetadata(
            index_version=make_index_version(commit_sha),
            repo_root=str(self.repo_root),
            source_roots=[root.path for root in source_roots],
            source_root_specs=[
                {"path": root.path, "module_prefix": root.module_prefix}
                for root in source_roots
            ],
            # Inherit detection/project metadata from previous build
            source_root_detection=store.metadata.get("source_root_detection"),
            project_name=store.metadata.get("project_name"),
            project_name_source=store.metadata.get("project_name_source"),
            project_version=store.metadata.get("project_version"),
            project_version_source=store.metadata.get("project_version_source"),
            commit_sha=commit_sha,
            evidence_manifest=store.metadata.get("evidence_manifest"),
            file_count=len(content.files),
            node_count=len(content.nodes),
            edge_count=len(content.edges),
            warning_count=len(content.warnings),
        )
        # Incremental analysis only sees changed files and does not re-run
        # optional external extractors. Preserve the last complete frontend
        # declarations, while still being able to recover a legacy/partial
        # index that did not persist language tiers.
        previous_language_tiers = _mapping(store.metadata.get("language_tiers"))
        current_language_tiers = build_language_tiers(
            content.frontend_capabilities.values(),
            content.adapter_metrics,
        )
        metadata.language_tiers = {
            **current_language_tiers,
            **previous_language_tiers,
        }
        metadata.extractor_metadata = {
            **_mapping(store.metadata.get("extractor_metadata")),
            **content.extractor_metadata,
        }
        # The frontend counted only the batch of changed files it analyzed, so
        # a change touching no profiled file would record zero and disarm the
        # stripped-profile guard on the next run. The published graph is the
        # only authority for how many profiles this index actually carries.
        typescript_metadata = metadata.extractor_metadata.get(TYPESCRIPT_FRONTEND_NAME)
        if isinstance(typescript_metadata, dict):
            metadata.extractor_metadata[TYPESCRIPT_FRONTEND_NAME] = {
                **typescript_metadata,
                "similarity_profile_count": typescript_similarity_profile_count(
                    content.nodes
                ),
            }
        metadata.toolchain_status = {
            **_mapping(store.metadata.get("toolchain_status")),
            **content.toolchain_status,
        }
        if ArcGraphIndexer._has_precision_evidence(content.edges):
            metadata.capabilities["precise_references"] = "available"
        if metadata.language_tiers:
            metadata.capabilities["language_tiers"] = "available"
        if metadata.toolchain_status:
            metadata.capabilities["external_toolchains"] = "available"
        ArcGraphIndexer._update_precision_capability(metadata, content)
        ArcGraphIndexer._update_coverage_capability(metadata, content)
        ArcGraphIndexer._update_runtime_capability(metadata, content)
        ArcGraphIndexer._update_adapter_capability(metadata, content)
        metadata.capabilities["similarity"] = "available"
        metadata.capabilities["receiver_resolution"] = (
            "available" if enable_v2_call_resolution else "legacy"
        )
        stale_diagnostics = self._stale_target_diagnostics(
            metadata,
            old_nodes,
            old_edges,
            content.nodes,
            changed_paths,
            excluded_paths={
                path
                for path in delta["deleted"]
                if scanner._is_ignored(self.repo_root / path)
            },
        )
        artifacts = EvidenceMergeEngine(
            self.repo_root,
            dispatcher=python_compat_pass_dispatcher(),
        ).merge_graph(
            metadata,
            content.nodes,
            content.edges,
            content.warnings,
            changed_files=tuple(sorted(changed_paths)),
            full_rebuild=False,
            extra_diagnostics=stale_diagnostics,
        )
        content.nodes = artifacts.nodes
        content.edges = artifacts.edges

        ArcGraphIndexer.update_metadata_counts(metadata, content)
        build_dir = GraphStoreWriter(self.output_dir).write(
            metadata,
            content.files,
            content.nodes,
            content.edges,
            content.warnings,
            semantic_facts=artifacts.semantic_facts,
            diagnostics=artifacts.diagnostics,
            merge_metrics=artifacts.merge_metrics,
            schema_migrations=artifacts.schema_migrations,
            adapter_metrics=content.adapter_metrics,
            precision_metrics=content.precision_metrics,
            coverage_metrics=content.coverage_metrics,
            runtime_metrics=content.runtime_metrics,
            phase_timings=content.phase_timings,
        )
        return {
            "schema_version": metadata.schema_version,
            "index_version": metadata.index_version,
            "status": "reindexed",
            "changed": delta,
            "build_dir": str(build_dir),
            "file_count": metadata.file_count,
            "node_count": metadata.node_count,
            "edge_count": metadata.edge_count,
            "warning_count": metadata.warning_count,
        }

    @staticmethod
    def _emit_skip_summary_changed(
        current_warnings: list[BuildWarning],
        old_warnings: list[BuildWarning],
    ) -> bool:
        """True when the scanner's emit-skip summary differs from the index's."""

        def summary(warnings: list[BuildWarning]) -> list[str]:
            return sorted(
                warning.message
                for warning in warnings
                if warning.kind == TYPESCRIPT_EMIT_SKIP_WARNING_KIND
            )

        return summary(current_warnings) != summary(old_warnings)

    @staticmethod
    def _deduplicated_warnings(warnings: list[BuildWarning]) -> list[BuildWarning]:
        seen: set[tuple[str, str, str | None, str | None]] = set()
        unique: list[BuildWarning] = []
        for warning in warnings:
            key = (
                warning.kind,
                warning.message,
                warning.path,
                warning.frontend_name,
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(warning)
        return unique

    @staticmethod
    def _assert_typescript_incremental_compatibility(
        *,
        metadata: dict[str, Any],
        nodes: list[Node],
        edges: list[Edge],
        check_express_mount_context: bool = True,
    ) -> None:
        # Express mount relationships are currently derived from the complete
        # TypeScript file set rather than persisted as independently reusable
        # facts. Reusing a partial view can turn mounted routes into router-local
        # routes (or keep obsolete mount prefixes), so fail before publication.
        # A type-only import is erased at build time and registers no routes, so
        # it must not disable incremental reindex for the whole repository.
        # Scorer-only refreshes (no TypeScript sources changed) never rebuild
        # mounts, so skip this source-context gate for those paths.
        if check_express_mount_context:
            has_runtime_express_import = any(
                edge.kind == "imports"
                and edge.target == "ext:express"
                and edge.properties.get("import_kind") != "type"
                for edge in edges
            )
            # Route nodes are a second, provenance-independent signal. Besides
            # protecting legacy indexes whose duplicate import edge may have kept
            # only the type-only property, this ensures a future edge-merge change
            # cannot silently bypass the mount-context guard.
            has_express_routes = any(
                node.kind == "route"
                and node.properties.get("frontend_name") == TYPESCRIPT_FRONTEND_NAME
                and node.properties.get("framework") == "express"
                for node in nodes
            )
            if has_runtime_express_import or has_express_routes:
                raise FullBuildRequired(
                    "Incremental TypeScript reindex cannot safely preserve cross-file "
                    "Express mount context. Run a full ArcGraph build before reindexing "
                    "a repository that imports Express."
                )

        extractor_metadata = metadata.get("extractor_metadata")
        typescript_metadata = (
            extractor_metadata.get(TYPESCRIPT_FRONTEND_NAME)
            if isinstance(extractor_metadata, dict)
            else None
        )
        recorded_algorithm = (
            typescript_metadata.get("similarity_profile_algorithm")
            if isinstance(typescript_metadata, dict)
            else None
        )
        if (
            recorded_algorithm is not None
            and recorded_algorithm != TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM
        ):
            raise FullBuildRequired(
                "Incremental TypeScript reindex cannot verify the previous "
                f"similarity profile algorithm: the index recorded "
                f"{recorded_algorithm!r} but this build expects "
                f"{TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM!r}. Run a full "
                "ArcGraph build before reindexing changed TypeScript files."
            )

        typescript_callables = [
            node
            for node in nodes
            if node.kind in {"function", "method"}
            and node.properties.get("frontend_name") == TYPESCRIPT_FRONTEND_NAME
        ]
        recorded_profile_count = (
            typescript_metadata.get("similarity_profile_count")
            if isinstance(typescript_metadata, dict)
            else None
        )
        live_profile_count = sum(
            1
            for node in typescript_callables
            if isinstance(node.properties.get("similarity"), dict)
        )
        # When the index recorded an exact profile count, any partial strip must
        # fail closed -- not only the all-or-nothing case. Indexes written before
        # this field existed keep the legacy fallback: "no profiles at all" is
        # damage only when the absent algorithm signal says profiles were
        # expected; callables below the n-gram floor stay reindexable.
        if isinstance(recorded_profile_count, int):
            if live_profile_count != recorded_profile_count:
                raise FullBuildRequired(
                    "Incremental TypeScript reindex cannot verify the previous "
                    f"similarity profiles: {live_profile_count} present but the "
                    f"index recorded {recorded_profile_count}. Run a full "
                    "ArcGraph build before reindexing changed TypeScript files."
                )
        elif (
            typescript_callables
            and recorded_algorithm is None
            and live_profile_count == 0
        ):
            raise FullBuildRequired(
                "Incremental TypeScript reindex cannot verify the previous "
                "similarity profiles: TypeScript callables are present but "
                "every profile is missing and no algorithm was recorded. Run "
                "a full ArcGraph build before reindexing changed TypeScript "
                "files."
            )

        incompatible = []
        for node in typescript_callables:
            profile = node.properties.get("similarity")
            if not isinstance(profile, dict):
                continue
            if profile.get("algorithm") != TYPESCRIPT_SIMILARITY_PROFILE_ALGORITHM:
                incompatible.append(node.id)
        if incompatible:
            raise FullBuildRequired(
                "Incremental TypeScript reindex cannot reuse "
                f"{len(incompatible)} incompatible similarity profile(s). "
                "Run a full ArcGraph build before reindexing changed TypeScript files."
            )

    @staticmethod
    def _assert_similarity_algorithms_compatible(
        *,
        nodes: list[Node],
        edges: list[Edge],
    ) -> None:
        incompatible_edges = [
            edge
            for edge in edges
            if edge.kind == "similar_to"
            and edge.properties.get("algorithm") not in CURRENT_SIMILARITY_ALGORITHMS
        ]
        incompatible_profiles = [
            node
            for node in nodes
            if node.kind in {"function", "method"}
            and isinstance(node.properties.get("similarity"), dict)
            and node.properties["similarity"].get("algorithm")
            not in CURRENT_SIMILARITY_ALGORITHMS
        ]
        if incompatible_edges or incompatible_profiles:
            raise FullBuildRequired(
                "Incremental reindex cannot safely upgrade "
                f"{len(incompatible_edges)} incompatible similarity edge(s) and "
                f"{len(incompatible_profiles)} incompatible profile(s). Run a full "
                "ArcGraph build before reindexing changed files."
            )

    @staticmethod
    def _assert_incremental_module_identities_stable(
        old_files: list[FileRecord],
        current_files: list[FileRecord],
    ) -> None:
        old_by_path = {file.path: file for file in old_files}
        changed_identity_count = sum(
            1
            for current in current_files
            if current.path in old_by_path
            and current.module != old_by_path[current.path].module
        )
        if changed_identity_count:
            raise FullBuildRequired(
                "Incremental reindex cannot safely preserve import and call edges "
                f"after module identity changes in {changed_identity_count} existing "
                "file(s). Run a full ArcGraph build instead."
            )

    @staticmethod
    def _recover_source_roots(metadata: dict[str, Any]) -> tuple[SourceRoot, ...]:
        return recover_source_roots_from_metadata(metadata)

    @staticmethod
    def _delta(
        old_files: list[FileRecord], current_files: list[FileRecord]
    ) -> dict[str, list[str]]:
        old_by_path = {file.path: file for file in old_files}
        current_by_path = {file.path: file for file in current_files}

        added = sorted(path for path in current_by_path if path not in old_by_path)
        deleted = sorted(path for path in old_by_path if path not in current_by_path)
        modified = sorted(
            path
            for path, current in current_by_path.items()
            if path in old_by_path
            and (
                current.file_hash != old_by_path[path].file_hash
                or current.module != old_by_path[path].module
                or current.source_root != old_by_path[path].source_root
                or current.is_package != old_by_path[path].is_package
            )
        )

        return {"added": added, "modified": modified, "deleted": deleted}

    @staticmethod
    def _receiver_resolution_enabled(metadata: dict[str, Any]) -> bool:
        capabilities = metadata.get("capabilities", {})
        if not isinstance(capabilities, dict):
            return True
        return capabilities.get("receiver_resolution", "available") == "available"

    @staticmethod
    def _preserve_incremental_summary_metrics(
        previous: Any,
        current: dict[str, Any],
    ) -> dict[str, Any]:
        if isinstance(previous, dict) and previous:
            return previous
        return current

    def _stale_target_diagnostics(
        self,
        metadata: IndexMetadata,
        old_nodes: list[Node],
        old_edges: list[Edge],
        current_nodes: list[Node],
        changed_paths: set[str],
        *,
        excluded_paths: set[str] | None = None,
    ) -> list[SemanticDiagnostic]:
        if not changed_paths:
            return []

        current_node_ids = {node.id for node in current_nodes}
        old_nodes_by_id = {node.id: node for node in old_nodes}
        details: list[dict[str, Any]] = []
        reanalysis_paths: set[str] = set()
        for edge in old_edges:
            if edge.target in current_node_ids or edge.target.startswith("ext:"):
                continue
            source = old_nodes_by_id.get(edge.source)
            target = old_nodes_by_id.get(edge.target)
            if source is None or source.path in changed_paths:
                continue
            # Exclusion intentionally removes a hierarchy member. Its contains
            # relation was regenerated above, so it is not an unrefreshed
            # consumer. Preserve the legacy diagnostics for physical deletions
            # and for other relations whose source still needs reanalysis.
            if (
                edge.kind == "contains"
                and source.kind in {"source_root", "package"}
                and target is not None
                and target.path in (excluded_paths or set())
            ):
                continue
            if target is not None and target.path not in changed_paths:
                continue
            if edge.source not in current_node_ids:
                continue

            if source.path:
                reanalysis_paths.add(source.path)
            details.append(
                {
                    "source": edge.source,
                    "target": edge.target,
                    "kind": edge.kind,
                    "source_path": source.path,
                    "target_path": target.path if target is not None else None,
                    "stale_target": True,
                }
            )

        if not details:
            return []

        capped_details = sorted(
            details,
            key=lambda item: (
                str(item.get("source_path") or ""),
                str(item.get("source")),
                str(item.get("target")),
                str(item.get("kind")),
            ),
        )[:50]
        recommended_paths = sorted(reanalysis_paths)
        return [
            SemanticDiagnostic(
                diagnostic_id=diagnostic_id(
                    "default",
                    "stale_target",
                    metadata.index_version,
                    *sorted(changed_paths),
                ),
                repo_id="default",
                index_version=metadata.index_version,
                diagnostic_kind="stale_target",
                message=(
                    f"Incremental reindex dropped {len(details)} stale edge(s) "
                    "whose target disappeared or changed identity."
                ),
                severity="warning",
                frontend_name="platform-reindex",
                first_seen_index=metadata.index_version,
                last_seen_index=metadata.index_version,
                properties={
                    "stale_edges_count": len(details),
                    "stale_edges_detail": capped_details,
                    "stale_edges_truncated_count": max(0, len(details) - 50),
                    "reanalysis_recommended_paths": recommended_paths,
                    "changed_paths": sorted(changed_paths),
                    "target_stale": True,
                },
            )
        ]

    def _merge_content(
        self,
        current_files: list[FileRecord],
        old_nodes: list[Node],
        old_edges: list[Edge],
        old_warnings: list[BuildWarning],
        changed_content: IndexContent,
        changed_paths: set[str],
        refreshed_typescript_similarity_buckets: set[str],
        refreshed_python_similarity_buckets: set[str],
        rescued_shared_nodes: list[Node] | None = None,
    ) -> IndexContent:
        changed_content_node_ids = {node.id for node in changed_content.nodes}
        kept_nodes = [
            node
            for node in old_nodes
            if node.kind != "external_package"
            and (node.path is None or node.path not in changed_paths)
        ]
        kept_nodes.extend(
            node
            for node in rescued_shared_nodes or []
            if node.id not in changed_content_node_ids
        )
        nodes = kept_nodes + changed_content.nodes
        valid_node_ids = {node.id for node in nodes}
        changed_old_node_ids = {
            node.id for node in old_nodes if node.path and node.path in changed_paths
        }
        old_typescript_node_ids = {
            node.id
            for node in old_nodes
            if node.properties.get("frontend_name") == TYPESCRIPT_FRONTEND_NAME
        }
        old_python_similarity_node_ids = {
            node.id
            for node in old_nodes
            if node.kind in {"function", "method"}
            and (
                node.properties.get("frontend_name") == COMPAT_FRONTEND_NAME
                or (
                    isinstance(node.properties.get("similarity"), dict)
                    and node.properties["similarity"].get("algorithm")
                    == PYTHON_SIMILARITY_PROFILE_ALGORITHM
                )
            )
        }
        # A similarity refresh emits the complete derived edge set for the
        # affected buckets, including pairs between two unchanged profiles. Do
        # not merge it with the previous generation or membership-dependent
        # candidates, caps, and skip thresholds will leave stale edges behind.
        kept_edges = [
            edge
            for edge in old_edges
            if edge.source in valid_node_ids
            and (edge.target in valid_node_ids or edge.target.startswith("ext:"))
            and not self._has_changed_evidence(edge, changed_paths)
            and not (
                edge.kind == "similar_to"
                and (
                    edge.source in changed_old_node_ids
                    or edge.target in changed_old_node_ids
                    or (
                        edge.properties.get("bucket")
                        in refreshed_typescript_similarity_buckets
                        and (
                            edge.source in old_typescript_node_ids
                            or edge.target in old_typescript_node_ids
                        )
                    )
                    or (
                        edge.properties.get("bucket")
                        in refreshed_python_similarity_buckets
                        and (
                            edge.source in old_python_similarity_node_ids
                            or edge.target in old_python_similarity_node_ids
                        )
                    )
                )
            )
        ]
        edges = kept_edges + changed_content.edges
        nodes = self._without_unreferenced_pathless_synthetic_nodes(nodes, edges)
        nodes = self._with_referenced_external_nodes(
            nodes,
            edges,
            # `kept_nodes` drops every old external_package above, so without
            # these the only externals in scope are the ones this round's
            # frontends re-emitted. A package imported solely by an unchanged
            # file would otherwise be rebuilt as a bare stub.
            previous_external_nodes=[
                node for node in old_nodes if node.kind == "external_package"
            ],
        )

        warnings = [
            warning
            for warning in old_warnings
            if warning.path is None or warning.path not in changed_paths
            # The scanner's emit-artifact summary is path-less and re-emitted
            # by every full rescan with a current count; retaining the
            # previous copy accumulates stale counts that nothing can evict.
            if warning.kind != TYPESCRIPT_EMIT_SKIP_WARNING_KIND
            if not (
                self._typescript_similarity_warning_matches_buckets(
                    warning,
                    refreshed_typescript_similarity_buckets,
                )
                and warning.kind in TYPESCRIPT_SIMILARITY_WARNING_KINDS
                and warning.frontend_name == TYPESCRIPT_FRONTEND_NAME
            )
            if not (
                self._python_similarity_warning_matches_buckets(
                    warning,
                    refreshed_python_similarity_buckets,
                )
                and warning.kind in PYTHON_SIMILARITY_WARNING_KINDS
                and warning.frontend_name == COMPAT_FRONTEND_NAME
            )
        ]
        warnings.extend(changed_content.warnings)
        # A frontend that re-runs on every incremental build re-emits its
        # path-less warnings each time. They are not distinguishable from the
        # retained copies, so keep one of each rather than accumulating.
        warnings = self._deduplicated_warnings(warnings)

        return IndexContent(
            files=sorted(current_files, key=lambda file: file.path),
            nodes=nodes,
            edges=edges,
            warnings=warnings,
            adapter_metrics=changed_content.adapter_metrics,
            precision_metrics=changed_content.precision_metrics,
            coverage_metrics=changed_content.coverage_metrics,
            runtime_metrics=changed_content.runtime_metrics,
            phase_timings=changed_content.phase_timings,
            frontend_capabilities=changed_content.frontend_capabilities,
            extractor_metadata=changed_content.extractor_metadata,
            toolchain_status=changed_content.toolchain_status,
        )

    @staticmethod
    def _rescued_shared_config_nodes(
        *,
        old_nodes: list[Node],
        old_edges: list[Edge],
        changed_paths: set[str],
        surviving_node_ids: set[str],
    ) -> list[Node]:
        """Keep id-keyed shared config nodes alive while a survivor references them.

        Config nodes (``config:env:*``, ``config:browser_storage:*``, ...) are
        shared across every file that reads the same key, yet carry only the
        first referencing file's path. A path-based merge would delete the node
        — and every edge into it — when that file changes, even though a full
        build re-creates it from the surviving references. Rescue candidates
        are exactly the dropped config nodes that at least one surviving node
        still points at; references from changed files do not count, since the
        frontend re-derives those from fresh content.

        Location and owner-stamped properties (for example env ``syntax``) are
        taken from the earliest surviving witness so the rescued node matches a
        full build's first-writer-wins attribution rather than retaining a path
        or access syntax that no longer references the key.
        """
        dropped = {
            node.id: node
            for node in old_nodes
            if node.kind == "config"
            and node.id.startswith("config:")
            and node.path is not None
            and node.path in changed_paths
        }
        if not dropped:
            return []
        surviving_by_id = {
            node.id: node for node in old_nodes if node.id in surviving_node_ids
        }
        witnesses: dict[str, list[tuple[str, int, int, str, Edge]]] = {}
        for edge in old_edges:
            if edge.source not in surviving_node_ids or edge.target not in dropped:
                continue
            attributed = False
            for evidence in edge.evidence:
                if not evidence.path or evidence.path in changed_paths:
                    continue
                witnesses.setdefault(edge.target, []).append(
                    (
                        evidence.path,
                        (
                            evidence.start_line
                            if isinstance(evidence.start_line, int)
                            else 0
                        ),
                        evidence.end_line if isinstance(evidence.end_line, int) else 0,
                        edge.source,
                        edge,
                    )
                )
                attributed = True
            if attributed:
                continue
            source = surviving_by_id.get(edge.source)
            if source is None or not source.path or source.path in changed_paths:
                continue
            witnesses.setdefault(edge.target, []).append(
                (
                    source.path,
                    source.start_line if isinstance(source.start_line, int) else 0,
                    source.end_line if isinstance(source.end_line, int) else 0,
                    edge.source,
                    edge,
                )
            )
        rescued: list[Node] = []
        for node_id, node in sorted(dropped.items()):
            candidates = witnesses.get(node_id)
            if not candidates:
                continue
            path, start_line, end_line, source_id, edge = sorted(
                candidates, key=lambda item: item[:4]
            )[0]
            # Follow the surviving witness's location contract, not the dropped
            # producer's: Pydantic and Python access sites omit end_line while
            # TypeScript ones carry both ends, and the node now belongs to
            # whoever still references it.
            witness = surviving_by_id.get(source_id)
            rescued_end_line = (
                None
                if ArcGraphReindexer._witness_omits_end_line(edge, witness)
                else (end_line or None)
            )
            rescued.append(
                node.model_copy(
                    update={
                        "path": path,
                        "start_line": start_line or None,
                        "end_line": rescued_end_line,
                        "properties": ArcGraphReindexer._shared_config_properties_from_witness(
                            node,
                            edge,
                            source=witness,
                        ),
                    }
                )
            )
        return rescued

    @staticmethod
    def _witness_omits_end_line(edge: Edge, source: Node | None) -> bool:
        """True when the surviving producer's config nodes carry no end_line."""

        if isinstance(edge.properties.get("callsite"), dict):
            return True  # Python call-analyzer config access
        return source is not None and source.kind in {"class", "model_field"}

    @staticmethod
    def _shared_config_properties_from_witness(
        node: Node,
        edge: Edge,
        *,
        source: Node | None,
    ) -> dict[str, Any]:
        """Rebuild producer properties from the surviving witness only.

        Shared config identity lives in the node id. Everything else is
        producer-specific (``source=pydantic_settings``, ``config_kind=env``,
        callsite stamps, ...). Copying the dropped node's property bag and
        patching owner fields leaves cross-frontend rescues with stale producer
        metadata, so rebuild from the witness edge/source instead.
        """

        properties = ArcGraphReindexer._producer_properties_from_config_witness(
            node,
            edge,
            source,
        )
        for key in _SHARED_CONFIG_FRONTEND_PROPERTY_KEYS:
            if key in edge.properties:
                properties[key] = edge.properties[key]
                continue
            if source is None:
                continue
            value = source.properties.get(key)
            if value is not None:
                properties[key] = value
        return properties

    @staticmethod
    def _producer_properties_from_config_witness(
        node: Node,
        edge: Edge,
        source: Node | None,
    ) -> dict[str, Any]:
        """Derive the surviving producer's config property payload."""

        derived: dict[str, Any] = {}

        # CallAnalyzer stores the access expression under a nested callsite
        # object. Config nodes flatten that into ``call`` / ``raw_expression``.
        callsite = edge.properties.get("callsite")
        if isinstance(callsite, dict):
            raw_expression = callsite.get("raw_expression")
            if isinstance(raw_expression, str) and raw_expression:
                derived["raw_expression"] = raw_expression
                call_expression = callsite.get("call_expression")
                derived["call"] = (
                    call_expression
                    if isinstance(call_expression, str) and call_expression
                    else raw_expression
                )
                derived["reason"] = "config_access"
            return derived

        resource_kind = edge.properties.get("resource_kind")
        if resource_kind == "env" or (
            isinstance(edge.properties.get("syntax"), str)
            and isinstance(edge.properties.get("env_key"), str)
        ):
            derived["config_kind"] = "env"
            env_key = edge.properties.get("env_key")
            if isinstance(env_key, str) and env_key:
                derived["key"] = env_key
            elif isinstance(node.name, str) and node.name:
                derived["key"] = node.name
            syntax = edge.properties.get("syntax")
            if isinstance(syntax, str) and syntax:
                derived["syntax"] = syntax
            return derived

        if resource_kind == "browser_storage":
            derived["config_kind"] = "browser_storage"
            for key in ("storage", "key"):
                value = edge.properties.get(key)
                if isinstance(value, str) and value:
                    derived[key] = value
            return derived

        # Pydantic Settings: configures Class -> config, or maps_to Field -> config.
        # Evidence detail looks like ``SettingsB.beta -> SHARED``.
        if source is not None and source.kind in {"class", "model_field"}:
            derived["source"] = "pydantic_settings"
            if isinstance(node.name, str) and node.name:
                derived["key"] = node.name
            if source.kind == "class":
                derived["settings_class"] = source.name
                for evidence in edge.evidence:
                    detail = evidence.detail or ""
                    if " -> " not in detail:
                        continue
                    left, _separator, _key = detail.partition(" -> ")
                    _class_name, separator, field_name = left.rpartition(".")
                    if separator and field_name:
                        derived["field"] = field_name
                    break
            else:
                derived["field"] = source.name
                if source.qualname and "." in source.qualname:
                    derived["settings_class"] = source.qualname.rsplit(".", 1)[
                        0
                    ].rsplit(".", 1)[-1]
            return derived

        return derived

    @staticmethod
    def _typescript_similarity_warning_matches_buckets(
        warning: BuildWarning,
        buckets: set[str],
    ) -> bool:
        return any(
            f"bucket {bucket!r}" in warning.message
            or f"bucket {json.dumps(bucket, ensure_ascii=False)}" in warning.message
            for bucket in buckets
        )

    @staticmethod
    def _python_similarity_warning_matches_buckets(
        warning: BuildWarning,
        buckets: set[str],
    ) -> bool:
        # Python skip warnings use ``{bucket!r}``; only replace a warning when
        # its bucket was fully rescored. A blanket drop on any ``.py`` change
        # would erase a skip disclosure after a pure deletion that never emits
        # the replacement edges.
        return any(f"bucket {bucket!r}" in warning.message for bucket in buckets)

    @staticmethod
    def _has_changed_evidence(edge: Edge, changed_paths: set[str]) -> bool:
        return any(
            evidence.path in changed_paths
            for evidence in edge.evidence
            if evidence.path
        )

    @staticmethod
    def _with_referenced_external_nodes(
        nodes: list[Node],
        edges: list[Edge],
        previous_external_nodes: list[Node] | None = None,
    ) -> list[Node]:
        non_external_nodes = [node for node in nodes if node.kind != "external_package"]
        # Keep whatever the frontends actually produced. A regenerated stub can
        # only carry ``package``, and semantic.py defaults a node without
        # ``language``/``frontend_name`` to the Python compat frontend, so
        # rebuilding one changes its semantic fact id and misattributes the
        # package on every incremental reindex. This round's frontends only
        # re-emit the packages their own changed files import, so fall back to
        # the previous index for everything imported by an unchanged file.
        produced = {
            node.id: node
            for node in [*(previous_external_nodes or []), *nodes]
            if node.kind == "external_package"
        }
        existing = {node.id for node in non_external_nodes}
        external_ids = sorted(
            {
                edge.target
                for edge in edges
                if edge.target.startswith("ext:") and edge.target not in existing
            }
        )
        external_nodes = [
            (
                produced[node_id]
                if node_id in produced
                else Node(
                    id=node_id,
                    kind="external_package",
                    name=node_id.removeprefix("ext:"),
                    qualname=node_id.removeprefix("ext:"),
                    properties={"package": node_id.removeprefix("ext:")},
                )
            )
            for node_id in external_ids
        ]
        return non_external_nodes + external_nodes

    @staticmethod
    def _without_unreferenced_pathless_synthetic_nodes(
        nodes: list[Node], edges: list[Edge]
    ) -> list[Node]:
        referenced_node_ids = {edge.source for edge in edges} | {
            edge.target for edge in edges
        }
        return [
            node
            for node in nodes
            if not _is_pathless_synthetic_node(node) or node.id in referenced_node_ids
        ]


def _is_pathless_synthetic_node(node: Node) -> bool:
    return node.path is None and (
        node.kind in {"external_symbol", "protocol_symbol", "config"}
        or node.id.startswith(("extsym:", "protocol:"))
    )


def _mapping(value: Any) -> dict[str, Any]:
    """Return a shallow metadata mapping, rejecting malformed legacy values."""

    return dict(value) if isinstance(value, dict) else {}
