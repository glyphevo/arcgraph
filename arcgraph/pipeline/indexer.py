"""ArcGraph index builder."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcgraph.core.cleanup import prune_arcgraph_output_after_publish
from arcgraph.core.evidence_manifest import (
    artifact_status,
    build_evidence_manifest,
    precision_health_status,
)
from arcgraph.core.language_tiers import build_language_tiers
from arcgraph.core.operation_lock import arcgraph_operation_lock

from arcgraph.adapters.common import AdapterRegistry
from arcgraph.adapters.registry import default_adapter_registry
from arcgraph.analyzers.coverage import CoverageCollector
from arcgraph.analyzers.precise_references import PreciseReferenceAnalyzer
from arcgraph.analyzers.runtime_trace import RuntimeTraceImporter
from arcgraph.pipeline.frontends import (
    FrontendRegistry,
    LanguageFrontend,
    PythonSemanticFrontend,
    default_frontend_registry,
    python_compat_pass_dispatcher,
)
from arcgraph.pipeline.contracts import validate_frontend_graph_fragment
from arcgraph.pipeline.openapi_frontend import OpenApiProtocolFrontend
from arcgraph.pipeline.typescript_frontend import (
    TYPESCRIPT_FRONTEND_NAME,
    typescript_similarity_profile_count,
)
from arcgraph.pipeline.scip_frontend import ScipProtocolFrontend
from arcgraph.core.graph_store import GraphStoreWriter
from arcgraph.core.merge import EvidenceMergeEngine
from arcgraph.core.metadata import current_commit, make_index_version
from arcgraph.core.structural import generate_structural_hierarchy
from arcgraph.core.scanner import (
    FileScanner,
    SourceRoot,
    SourceRootDetection,
    detect_source_roots,
)
from arcgraph.core.schemas import (
    BuildWarning,
    Edge,
    Evidence,
    FileRecord,
    FrontendAnalyzeRequest,
    IndexMetadata,
    Node,
)


class IndexContent:
    def __init__(
        self,
        files: list[FileRecord],
        nodes: list[Node],
        edges: list[Edge],
        warnings: list[BuildWarning],
        adapter_metrics: dict[str, Any] | None = None,
        precision_metrics: dict[str, Any] | None = None,
        coverage_metrics: dict[str, Any] | None = None,
        runtime_metrics: dict[str, Any] | None = None,
        phase_timings: dict[str, Any] | None = None,
        frontend_capabilities: dict[str, Any] | None = None,
        extractor_metadata: dict[str, Any] | None = None,
        toolchain_status: dict[str, Any] | None = None,
    ) -> None:
        self.files = files
        self.nodes = nodes
        self.edges = edges
        self.warnings = warnings
        self.adapter_metrics = adapter_metrics or {}
        self.precision_metrics = precision_metrics or {}
        self.coverage_metrics = coverage_metrics or {}
        self.runtime_metrics = runtime_metrics or {}
        self.phase_timings = phase_timings or {}
        self.frontend_capabilities = frontend_capabilities or {}
        self.extractor_metadata = extractor_metadata or {}
        self.toolchain_status = toolchain_status or {}


class ArcGraphIndexer:
    def __init__(
        self,
        repo_root: Path,
        output_dir: Path | None = None,
        source_roots: list[SourceRoot] | tuple[SourceRoot, ...] | None = None,
        ignore_rules: list[str] | None = None,
        scip_index_path: str | Path | None = None,
        scip_graph_index_path: str | Path | None = None,
        openapi_path: str | Path | None = None,
        pyright_export_path: str | Path | None = None,
        runtime_trace_path: str | Path | None = None,
        coverage_path: str | Path | None = None,
        enable_v2_call_resolution: bool = True,
        adapter_registry: AdapterRegistry | None = None,
        frontend: PythonSemanticFrontend | None = None,
        frontend_registry: FrontendRegistry | None = None,
        source_root_detection: SourceRootDetection | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.output_dir = (
            output_dir or self.repo_root / "output" / "arcgraph"
        ).resolve()
        if source_roots is not None:
            self.source_roots = tuple(source_roots)
            self.source_root_detection = source_root_detection
        else:
            resolved = detect_source_roots(self.repo_root)
            self.source_roots = resolved.roots
            self.source_root_detection = resolved.detection
        self.ignore_rules = ignore_rules
        self.scip_index_path = scip_index_path
        self.scip_graph_index_path = scip_graph_index_path
        self.openapi_path = openapi_path
        self.pyright_export_path = pyright_export_path
        self.runtime_trace_path = runtime_trace_path
        self.coverage_path = coverage_path
        self.enable_v2_call_resolution = enable_v2_call_resolution
        self.last_cleanup_result: dict[str, Any] | None = None
        self.adapter_registry = adapter_registry or default_adapter_registry()
        self.frontend = frontend or PythonSemanticFrontend(
            enable_v2_call_resolution=self.enable_v2_call_resolution,
            adapter_registry=self.adapter_registry,
        )
        if frontend_registry is not None:
            self.frontend_registry = frontend_registry
        elif frontend is not None:
            self.frontend_registry = FrontendRegistry([frontend])
        else:
            self.frontend_registry = default_frontend_registry(
                repo_root=self.repo_root,
                enable_v2_call_resolution=self.enable_v2_call_resolution,
                adapter_registry=self.adapter_registry,
            )
        self.scip_protocol_frontend = (
            ScipProtocolFrontend(self.repo_root, self.scip_graph_index_path)
            if self.scip_graph_index_path
            else None
        )
        self.openapi_protocol_frontend = (
            OpenApiProtocolFrontend(self.repo_root, self.openapi_path)
            if self.openapi_path
            else None
        )

    def build(self) -> tuple[IndexMetadata, Path]:
        with arcgraph_operation_lock(self.output_dir):
            result = self._build_inner()
            self.last_cleanup_result = prune_arcgraph_output_after_publish(
                self.output_dir
            )
            return result

    def _build_inner(self) -> tuple[IndexMetadata, Path]:
        commit_sha = current_commit(self.repo_root)
        index_version = make_index_version(commit_sha)
        active_frontends = self._build_plan(index_version)
        scanner = FileScanner(
            self.repo_root,
            self.source_roots,
            self.ignore_rules,
            file_extensions=self._file_extensions_for(active_frontends),
        )
        files = scanner.scan()
        warnings = list(scanner.warnings)
        content = self.analyze_files(
            files,
            warnings,
            frontends=active_frontends,
            trace_commit_sha=commit_sha,
            trace_index_version=index_version,
        )

        metadata = self._metadata_for(commit_sha, content, index_version=index_version)
        metadata.evidence_manifest = build_evidence_manifest(
            repo_root=self.repo_root,
            output_dir=self.output_dir,
            commit_sha=commit_sha,
            source_roots=[root.path for root in self.source_roots],
            precision_metrics=content.precision_metrics,
            coverage_metrics=content.coverage_metrics,
            runtime_metrics=content.runtime_metrics,
            scip_index_path=self.scip_index_path,
            pyright_export_path=self.pyright_export_path,
            coverage_path=self.coverage_path,
            runtime_trace_path=self.runtime_trace_path,
        )

        # Generate structural hierarchy BEFORE merge so structural nodes
        # go through the same pipeline (canonical identity, semantic facts,
        # merge metrics) — invariant: written graph and artifacts are consistent.
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
            self.source_roots,
            files=file_dicts,
            existing_nodes=content.nodes,
        )
        content.nodes.extend(struct_nodes)
        content.edges.extend(struct_edges)

        artifacts = EvidenceMergeEngine(
            self.repo_root,
            dispatcher=python_compat_pass_dispatcher(),
        ).merge_graph(
            metadata,
            content.nodes,
            content.edges,
            content.warnings,
        )
        content.nodes = artifacts.nodes
        content.edges = artifacts.edges

        self.update_metadata_counts(metadata, content)

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
        return metadata, build_dir

    def analyze_files(
        self,
        files: list[FileRecord],
        warnings: list[BuildWarning] | None = None,
        module_names: set[str] | None = None,
        call_context_nodes: list[Node] | None = None,
        frontends: list[LanguageFrontend] | tuple[LanguageFrontend, ...] | None = None,
        force_frontend_names: set[str] | frozenset[str] | None = None,
        incremental_similarity_buckets_by_frontend: (
            dict[str, set[str] | None] | None
        ) = None,
        trace_commit_sha: str | None = None,
        trace_index_version: str | None = None,
    ) -> IndexContent:
        """Analyze files and return graph content.

        Delegates Python-specific analysis to ``PythonGraphAnalyzer``,
        then runs language-agnostic post-processing (precision references,
        coverage, runtime trace).
        """
        active_frontends = list(frontends or self._frontends_for_files(files))
        combined_warnings = list(warnings or [])
        nodes: list[Node] = []
        edges: list[Edge] = []
        adapter_metrics: dict[str, Any] = {}
        phase_timings: dict[str, Any] = {}
        frontend_capabilities: dict[str, Any] = {}
        extractor_metadata: dict[str, Any] = {}
        toolchain_status: dict[str, Any] = {}
        module_names = module_names or {file.module for file in files if file.module}
        forced_frontends = set(force_frontend_names or ())

        for frontend in active_frontends:
            capabilities = frontend.capabilities()
            frontend_capabilities[frontend.name] = capabilities
            frontend_files = self._files_for_frontend(files, frontend)
            if (
                not frontend_files
                and not getattr(frontend, "artifact_only", False)
                and frontend.name not in forced_frontends
            ):
                continue
            analyze_kwargs: dict[str, Any] = {
                "warnings": [],
                "module_names": module_names,
                "call_context_nodes": [*(call_context_nodes or []), *nodes],
            }
            buckets_by_frontend = incremental_similarity_buckets_by_frontend or {}
            if frontend.name in buckets_by_frontend:
                analyze_kwargs["incremental_similarity_buckets"] = buckets_by_frontend[
                    frontend.name
                ]
            fragment = frontend.analyze_to_graph(frontend_files, **analyze_kwargs)
            fragment = validate_frontend_graph_fragment(
                fragment,
                capabilities,
            )
            nodes.extend(fragment.nodes)
            edges.extend(fragment.edges)
            combined_warnings.extend(fragment.warnings)
            if fragment.adapter_metrics:
                if frontend.name == "python-v1-compat-shim":
                    adapter_metrics.update(fragment.adapter_metrics)
                else:
                    adapter_metrics[frontend.name] = fragment.adapter_metrics
            if fragment.phase_timings:
                phase_timings[frontend.name] = fragment.phase_timings
            if fragment.extractor_metadata:
                extractor_metadata[frontend.name] = fragment.extractor_metadata
            if fragment.toolchain_status:
                toolchain_status[frontend.name] = fragment.toolchain_status

        warnings = combined_warnings

        # --- Language-agnostic post-processing ---
        nodes = self.dedupe_nodes(nodes)
        precise_analysis = PreciseReferenceAnalyzer(
            self.repo_root,
            self.scip_index_path,
            pyright_export_path=self.pyright_export_path,
            source_roots=tuple(root.path for root in self.source_roots),
            package=self.repo_root.name,
        ).analyze(nodes)
        edges.extend(precise_analysis.edges)
        warnings.extend(precise_analysis.warnings)

        coverage_analysis = CoverageCollector(
            self.repo_root,
            self.coverage_path,
        ).analyze(nodes)
        nodes.extend(coverage_analysis.nodes)
        edges.extend(coverage_analysis.edges)
        warnings.extend(coverage_analysis.warnings)

        runtime_analysis = RuntimeTraceImporter(
            self.repo_root,
            self.runtime_trace_path,
            commit_sha=trace_commit_sha,
            index_version=trace_index_version,
        ).analyze(nodes)
        nodes.extend(runtime_analysis.nodes)
        edges.extend(runtime_analysis.edges)
        warnings.extend(runtime_analysis.warnings)

        nodes = self.dedupe_nodes(nodes)
        edges = self.dedupe_edges(edges)
        # Frontend fragments record profile counts before cross-frontend
        # dedupe. A colliding Python/TS callable can drop the TypeScript node
        # while leaving an inflated count that later fails the incremental
        # strip guard. Recount over the merged graph, matching reindex.
        typescript_metadata = extractor_metadata.get(TYPESCRIPT_FRONTEND_NAME)
        if isinstance(typescript_metadata, dict):
            extractor_metadata[TYPESCRIPT_FRONTEND_NAME] = {
                **typescript_metadata,
                "similarity_profile_count": typescript_similarity_profile_count(nodes),
            }
        return IndexContent(
            files=files,
            nodes=nodes,
            edges=edges,
            warnings=warnings,
            adapter_metrics=adapter_metrics,
            precision_metrics=precise_analysis.metrics,
            coverage_metrics=coverage_analysis.metrics,
            runtime_metrics=runtime_analysis.metrics,
            phase_timings=phase_timings,
            frontend_capabilities=frontend_capabilities,
            extractor_metadata=extractor_metadata,
            toolchain_status=toolchain_status,
        )

    def _build_plan(self, index_version: str | None = None) -> list[LanguageFrontend]:
        plan = list(self.frontend_registry.frontends)
        if plan:
            return self._with_protocol_frontends(plan)
        request = FrontendAnalyzeRequest(
            repo_root=str(self.repo_root),
            index_version=index_version,
            source_roots=[root.path for root in self.source_roots],
        )
        plan = self.frontend_registry.build_plan(request) or [self.frontend]
        return self._with_protocol_frontends(list(plan))

    def _frontends_for_files(self, files: list[FileRecord]) -> list[LanguageFrontend]:
        file_paths = [file.path for file in files]
        request = FrontendAnalyzeRequest(
            repo_root=str(self.repo_root),
            source_roots=[root.path for root in self.source_roots],
            changed_files=file_paths,
        )
        plan = self.frontend_registry.build_plan(request)
        plan = plan or [self.frontend]
        return self._with_protocol_frontends(list(plan))

    def _with_protocol_frontends(
        self, frontends: list[LanguageFrontend]
    ) -> list[LanguageFrontend]:
        for protocol_frontend in (
            self.scip_protocol_frontend,
            self.openapi_protocol_frontend,
        ):
            if protocol_frontend is None:
                continue
            if any(frontend.name == protocol_frontend.name for frontend in frontends):
                continue
            frontends = [*frontends, protocol_frontend]
        return frontends

    @staticmethod
    def _file_extensions_for(frontends: list[LanguageFrontend]) -> tuple[str, ...]:
        extensions: list[str] = []
        for frontend in frontends:
            for extension in frontend.file_extensions:
                if extension not in extensions:
                    extensions.append(extension)
        return tuple(extensions) or (".py",)

    @staticmethod
    def _files_for_frontend(
        files: list[FileRecord], frontend: LanguageFrontend
    ) -> list[FileRecord]:
        return [file for file in files if frontend.accepts(file)]

    def _metadata_for(
        self,
        commit_sha: str | None,
        content: IndexContent,
        *,
        index_version: str | None = None,
    ) -> IndexMetadata:
        # Read project metadata from pyproject.toml
        project_name, project_name_source = self._read_project_name()
        project_version, project_version_source = self._read_project_version()

        # Build source_root_specs with full identity
        source_root_specs = [
            {"path": root.path, "module_prefix": root.module_prefix}
            for root in self.source_roots
        ]

        metadata = IndexMetadata(
            index_version=index_version or make_index_version(commit_sha),
            repo_root=str(self.repo_root),
            source_roots=[root.path for root in self.source_roots],
            source_root_specs=source_root_specs,
            source_root_detection=(
                {
                    "strategy": self.source_root_detection.strategy,
                    "roots": list(self.source_root_detection.roots),
                    "non_package_dirs": list(
                        self.source_root_detection.non_package_dirs
                    ),
                }
                if self.source_root_detection is not None
                else None
            ),
            project_name=project_name,
            project_name_source=project_name_source,
            project_version=project_version,
            project_version_source=project_version_source,
            commit_sha=commit_sha,
            file_count=len(content.files),
            node_count=len(content.nodes),
            edge_count=len(content.edges),
            warning_count=len(content.warnings),
        )
        if self._has_precision_evidence(content.edges):
            metadata.capabilities["precise_references"] = "available"
        metadata.language_tiers = build_language_tiers(
            content.frontend_capabilities.values(),
            content.adapter_metrics,
        )
        metadata.extractor_metadata = content.extractor_metadata
        metadata.toolchain_status = content.toolchain_status
        metadata.capabilities["language_tiers"] = "available"
        if content.toolchain_status:
            metadata.capabilities["external_toolchains"] = "available"
        self._update_precision_capability(metadata, content)
        self._update_coverage_capability(metadata, content)
        self._update_runtime_capability(metadata, content)
        metadata.capabilities["similarity"] = "available"
        metadata.capabilities["receiver_resolution"] = (
            "available" if self.enable_v2_call_resolution else "legacy"
        )
        self._update_adapter_capability(metadata, content)
        return metadata

    def _read_project_name(self) -> tuple[str | None, str | None]:
        """Read project name from pyproject.toml, or fallback to repo name."""
        pyproject_path = self.repo_root / "pyproject.toml"
        if pyproject_path.exists():
            try:
                import tomllib
            except ModuleNotFoundError:
                try:
                    import tomli as tomllib  # type: ignore[no-redef]
                except ModuleNotFoundError:
                    return self.repo_root.name, "repo-name-fallback"

            try:
                data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
                name = data.get("project", {}).get("name")
                if name and isinstance(name, str):
                    return name, "pyproject"
            except Exception:
                pass
        return None, "repo-name-fallback"

    def _read_project_version(self) -> tuple[str | None, str | None]:
        """Read project version from pyproject.toml; never uses commit_sha."""
        pyproject_path = self.repo_root / "pyproject.toml"
        if pyproject_path.exists():
            try:
                import tomllib
            except ModuleNotFoundError:
                try:
                    import tomli as tomllib  # type: ignore[no-redef]
                except ModuleNotFoundError:
                    return None, "working-tree"

            try:
                data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
                project = data.get("project", {})
                version = project.get("version")
                # dynamic version means we can't read it statically
                dynamic = project.get("dynamic", [])
                if isinstance(version, str) and version and "version" not in dynamic:
                    return version, "pyproject"
            except Exception:
                pass
        return None, "working-tree"

    @staticmethod
    def update_metadata_counts(metadata: IndexMetadata, content: IndexContent) -> None:
        metadata.file_count = len(content.files)
        metadata.node_count = len(content.nodes)
        metadata.edge_count = len(content.edges)
        metadata.warning_count = len(content.warnings)
        ArcGraphIndexer._update_adapter_capability(metadata, content)
        ArcGraphIndexer._update_precision_capability(metadata, content)
        ArcGraphIndexer._update_coverage_capability(metadata, content)
        ArcGraphIndexer._update_runtime_capability(metadata, content)

    @staticmethod
    def _update_metadata_counts(metadata: IndexMetadata, content: IndexContent) -> None:
        ArcGraphIndexer.update_metadata_counts(metadata, content)

    @staticmethod
    def _update_adapter_capability(
        metadata: IndexMetadata, content: IndexContent
    ) -> None:
        if any(warning.kind.startswith("adapter_") for warning in content.warnings):
            metadata.capabilities["framework_adapters"] = "partial"

    @staticmethod
    def _update_precision_capability(
        metadata: IndexMetadata, content: IndexContent
    ) -> None:
        if ArcGraphIndexer._has_precision_evidence(content.edges):
            metadata.capabilities["precise_references"] = "available"
        status = precision_health_status(metadata.evidence_manifest) or (
            content.precision_metrics.get("status")
        )
        if status == "available":
            metadata.capabilities["precision"] = "precision_available"
        elif status == "partial":
            metadata.capabilities["precision"] = "precision_partial"
        else:
            metadata.capabilities["precision"] = "ast_fallback_only"

    @staticmethod
    def _update_coverage_capability(
        metadata: IndexMetadata, content: IndexContent
    ) -> None:
        status = artifact_status(metadata.evidence_manifest, "coverage") or (
            content.coverage_metrics.get("status")
        )
        has_coverage_edges = any(edge.kind == "covers" for edge in content.edges)
        if status == "available" and has_coverage_edges:
            metadata.capabilities["coverage"] = "available"
            metadata.capabilities["test_recommendations"] = "heuristic-and-coverage"
        elif (
            status in {"partial", "stale", "invalid", "out_of_scope"}
            and has_coverage_edges
        ):
            metadata.capabilities["coverage"] = "partial"
            metadata.capabilities["test_recommendations"] = "heuristic-and-coverage"
        else:
            metadata.capabilities["coverage"] = "unavailable"
            metadata.capabilities["test_recommendations"] = "heuristic"

    @staticmethod
    def _update_runtime_capability(
        metadata: IndexMetadata, content: IndexContent
    ) -> None:
        status = artifact_status(metadata.evidence_manifest, "runtime_trace") or (
            content.runtime_metrics.get("status")
        )
        if status == "available":
            metadata.capabilities["runtime_trace"] = "available"
        elif status in {"partial", "stale", "invalid", "out_of_scope"}:
            metadata.capabilities["runtime_trace"] = "partial"
        else:
            metadata.capabilities["runtime_trace"] = "unavailable"

    @staticmethod
    def _module_nodes(files: list[FileRecord]) -> list[Node]:
        """Delegate to PythonGraphAnalyzer for backward compatibility."""
        from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer

        return PythonGraphAnalyzer._module_nodes(files)

    @staticmethod
    def _attach_module_callsites(
        nodes: list[Node], module: str, callsites: list[dict[str, object]]
    ) -> None:
        """Delegate to PythonGraphAnalyzer for backward compatibility."""
        from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer

        PythonGraphAnalyzer._attach_module_callsites(nodes, module, callsites)

    @staticmethod
    def _external_package_nodes(packages: set[str]) -> list[Node]:
        """Delegate to PythonGraphAnalyzer for backward compatibility."""
        from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer

        return PythonGraphAnalyzer._external_package_nodes(packages)

    @staticmethod
    def dedupe_nodes(nodes: list[Node]) -> list[Node]:
        return EvidenceMergeEngine.dedupe_nodes(nodes)

    @staticmethod
    def _dedupe_nodes(nodes: list[Node]) -> list[Node]:
        return ArcGraphIndexer.dedupe_nodes(nodes)

    @staticmethod
    def dedupe_edges(edges: list[Edge]) -> list[Edge]:
        return EvidenceMergeEngine.dedupe_edges(edges)

    @staticmethod
    def _dedupe_edges(edges: list[Edge]) -> list[Edge]:
        return ArcGraphIndexer.dedupe_edges(edges)

    @staticmethod
    def _has_evidence(edge: Edge, evidence: Evidence) -> bool:
        return EvidenceMergeEngine.has_evidence(edge, evidence)

    @staticmethod
    def _has_precision_evidence(edges: list[Edge]) -> bool:
        return any(
            evidence.kind.startswith(("scip_", "pyright_"))
            for edge in edges
            for evidence in edge.evidence
        )
