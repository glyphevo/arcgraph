"""Python-specific graph analyzer extracted from ArcGraphIndexer.

This module encapsulates all Python AST parsing and analysis logic that was
previously embedded in ``ArcGraphIndexer.analyze_files()``.  The indexer
now delegates to ``PythonGraphAnalyzer.analyze()`` for Python-specific work,
keeping the indexer as a pure orchestration layer.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import Any

from arcgraph.adapters.common import AdapterRegistry
from arcgraph.adapters.pytest_adapter import PytestAdapter
from arcgraph.adapters.pytest_types import PytestTypeAnalyzer
from arcgraph.analyzers.bindings import BindingAnalyzer
from arcgraph.core.project_environment import (
    ProjectEnvironment,
)
from arcgraph.core.structure import StructureView
from arcgraph.analyzers.calls import CallAnalyzer
from arcgraph.analyzers.imports import ImportAnalyzer
from arcgraph.analyzers.similarity import SimilarityAnalyzer
from arcgraph.analyzers.symbols import SymbolAnalyzer
from arcgraph.analyzers.types import TypeRefAnalyzer
from arcgraph.core.ids import external_package_id, module_id
from arcgraph.core.merge import EvidenceMergeEngine
from arcgraph.core.schemas import (
    BuildWarning,
    Edge,
    FileRecord,
    Node,
)
from arcgraph.pipeline.contracts import FrontendGraphFragment


class PythonGraphAnalyzer:
    """Encapsulates the Python AST analysis pipeline.

    This is the concrete implementation behind ``PythonSemanticFrontend``.
    It owns the full chain:  parse → imports → symbols → bindings → types
    → calls → adapters → similarity.

    The indexer delegates here for all Python-specific work, then handles
    the language-agnostic post-processing (precision, coverage, runtime)
    itself.
    """

    def __init__(
        self,
        *,
        enable_v2_call_resolution: bool = True,
        adapter_registry: AdapterRegistry | None = None,
        repo_root: Path | None = None,
        project_environment: Path | None = None,
    ) -> None:
        self.enable_v2_call_resolution = enable_v2_call_resolution
        self.adapter_registry = adapter_registry
        self.repo_root = repo_root
        self.project_environment = project_environment
        self.semantic_context = None

    def analyze(
        self,
        files: list[FileRecord],
        warnings: list[BuildWarning] | None = None,
        module_names: set[str] | None = None,
        call_context_nodes: list[Node] | None = None,
        incremental_similarity_buckets: set[str] | None = None,
        parsed_files_override: dict[str, ast.Module] | None = None,
    ) -> FrontendGraphFragment:
        """Run the full Python analysis pipeline.

        Parameters
        ----------
        files
            File records to analyze (already scanned).
        warnings
            Mutable warning list; parse errors are appended here.
        module_names
            Known module names for import resolution.
        call_context_nodes
            Additional surviving nodes for type and call resolution
            (e.g. unchanged files). Current files replace their old nodes.

        Returns
        -------
        FrontendGraphFragment
            The raw graph fragment with nodes, edges, warnings and metrics.
        """
        warnings = list(warnings or [])
        module_names = module_names or {file.module for file in files if file.module}
        if call_context_nodes is not None and parsed_files_override is None:
            current_paths = {file.path for file in files}
            call_context_nodes = [
                node for node in call_context_nodes if node.path not in current_paths
            ]
        phase_timings: dict[str, float] = {}

        # --- Phase 1: Parse ---
        t0 = time.monotonic()
        parsed_files: dict[str, ast.Module] = {}
        if self.semantic_context is None:
            self._structure = StructureView(self.repo_root, self.project_environment)
        if parsed_files_override is None:
            paths = {f.path: Path(f.abs_path) for f in files}
            if self.semantic_context is None:
                parsed_files, notices = self._structure.read(paths)
            else:
                from arcgraph.semantic.structure_provider import project_read

                self.semantic_context.before_parse()
                parsed_files, notices, hosted = project_read(
                    self.semantic_context.parser, paths, continue_project=False
                )
                self.semantic_context.structure_from_trees(
                    parsed_files, notices, hosted
                )
            warnings.extend(notices)
        for file_record in files:
            if parsed_files_override is not None:
                tree = parsed_files_override.get(file_record.path)
                if tree is not None:
                    parsed_files[file_record.path] = tree
                    tree._arcgraph_module = file_record.module
                continue
            if file_record.path in parsed_files:
                parsed_files[file_record.path]._arcgraph_module = file_record.module
        phase_timings["parse"] = time.monotonic() - t0

        # --- Phase 2: Structural (imports + symbols) ---
        t0 = time.monotonic()
        nodes = self._module_nodes(files)
        edges: list[Edge] = []
        external_packages: set[str] = set()
        import_analyzer = ImportAnalyzer()
        symbol_analyzer = SymbolAnalyzer()
        binding_analyzer = BindingAnalyzer()
        type_ref_analyzer = TypeRefAnalyzer()
        dependency_environment = self.project_environment
        if (
            self.semantic_context is not None
            and self.semantic_context.snapshot is not None
        ):
            dependency_environment = self.semantic_context.directory / "environment"
        environment = ProjectEnvironment(self.repo_root, dependency_environment)
        environment_metadata = (
            ProjectEnvironment(self.repo_root, self.project_environment).metadata
            if self.semantic_context is not None
            else environment.metadata
        )
        if self.project_environment is not None:
            environment_metadata["explicit_path"] = str(
                self.project_environment.resolve()
            )
        call_analyzer = CallAnalyzer(
            enable_v2=self.enable_v2_call_resolution, project_environment=environment
        )

        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            import_analysis = import_analyzer.analyze(file_record, tree, module_names)
            edges.extend(import_analysis.edges)
            external_packages.update(import_analysis.external_packages)

            symbol_analysis = symbol_analyzer.analyze(file_record, tree)
            self._attach_module_callsites(
                nodes, file_record.module, symbol_analysis.module_callsites
            )
            nodes.extend(symbol_analysis.nodes)
            edges.extend(symbol_analysis.edges)
        phase_timings["structural"] = time.monotonic() - t0

        # --- Phase 3: Scope / Type (bindings + type refs) ---
        t0 = time.monotonic()
        nodes = self._dedupe_nodes(nodes)
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            binding_analysis = binding_analyzer.analyze(file_record, tree, module_names)
            binding_analysis.attach_to_nodes(nodes)

        if call_context_nodes is not None:
            current_ids = {node.id for node in nodes}
            call_context_nodes = [
                node for node in call_context_nodes if node.id not in current_ids
            ]
        type_context = (
            self._dedupe_nodes(
                [
                    *(
                        n
                        for n in call_context_nodes
                        if parsed_files_override is None or n.path not in parsed_files
                    ),
                    *nodes,
                ]
            )
            if call_context_nodes is not None
            else nodes
        )
        return_decorator_cache = type_ref_analyzer.prepare_return_analysis(
            type_context,
            parsed_files,
            nominal_guard_trees=(
                parsed_files
                if parsed_files_override is not None
                else self._nominal_guard_trees(type_context, parsed_files)
            ),
        )
        for file_record in files:
            tree = parsed_files.get(file_record.path)
            if tree is None:
                continue
            type_ref_analysis = type_ref_analyzer.analyze(
                file_record, tree, type_context, module_names
            )
            type_ref_analysis.attach_to_nodes(nodes)
        if self.adapter_registry is not None and any(
            isinstance(adapter, PytestAdapter)
            for adapter in self.adapter_registry.adapters
        ):
            PytestTypeAnalyzer().attach(
                files, parsed_files, type_context, module_names, return_decorator_cache
            )
        phase_timings["scope_type"] = time.monotonic() - t0

        # --- Phase 4: Call resolution ---
        t0 = time.monotonic()
        nodes = self._dedupe_nodes(nodes)
        call_context = (
            self._dedupe_nodes([*call_context_nodes, *nodes])
            if call_context_nodes is not None
            else nodes
        )
        call_analysis = call_analyzer.analyze(call_context, source_nodes=nodes)
        nodes.extend(call_analysis.nodes)
        edges.extend(call_analysis.edges)
        phase_timings["calls"] = time.monotonic() - t0

        # --- Phase 5: Framework adapters ---
        t0 = time.monotonic()
        adapter_metrics: dict[str, Any] = {}
        if self.adapter_registry is not None:
            adapter_context = self._dedupe_nodes([*call_context, *call_analysis.nodes])
            adapter_analysis = self.adapter_registry.analyze(
                files, parsed_files, adapter_context, module_names
            )
            nodes.extend(adapter_analysis.nodes)
            edges.extend(adapter_analysis.edges)
            warnings.extend(adapter_analysis.warnings)
            adapter_metrics = adapter_analysis.metrics
        else:
            adapter_context = call_context
        phase_timings["adapters"] = time.monotonic() - t0

        # --- Phase 6: Similarity ---
        t0 = time.monotonic()
        similarity_context = self._dedupe_nodes(
            [*adapter_context, *call_analysis.nodes, *nodes]
        )
        similarity_analysis = SimilarityAnalyzer().analyze(
            parsed_files,
            similarity_context,
            edges,
            source_node_ids=(
                {node.id for node in nodes} if call_context_nodes is not None else None
            ),
            refresh_buckets=incremental_similarity_buckets,
        )
        edges.extend(similarity_analysis.edges)
        warnings.extend(similarity_analysis.warnings)
        phase_timings["similarity"] = time.monotonic() - t0

        # --- Finalize ---
        t0 = time.monotonic()
        nodes.extend(self._external_package_nodes(external_packages))
        nodes = self._dedupe_nodes(nodes)
        edges = self._dedupe_edges(edges)
        phase_timings["finalize"] = time.monotonic() - t0
        phase_timings["total"] = sum(phase_timings.values())
        if self.semantic_context is None:
            structure_status = self._structure.status()
        else:
            structure_status = self.semantic_context.parser.status()

        return FrontendGraphFragment(
            nodes=nodes,
            edges=edges,
            warnings=warnings,
            adapter_metrics=adapter_metrics,
            external_packages=external_packages,
            parsed_files=parsed_files,
            phase_timings=phase_timings,
            extractor_metadata={"dependency_environment": environment_metadata},
            toolchain_status={"python_structure": structure_status},
        )

    def _nominal_guard_trees(
        self, nodes: list[Node], parsed_files: dict[str, ast.Module]
    ) -> dict[str, ast.Module]:
        """Read unchanged class syntax for the same nominal proof as a full pass.

        Node summaries omit class keywords (including metaclass), so treating
        a surviving class node as proof would be unsafe. These ASTs are used
        only for the proof; their files are not reanalyzed or republished.
        """
        trees = dict(parsed_files)
        if self.repo_root is None:
            return trees
        paths = {
            node.path
            for node in nodes
            if node.kind == "class" and node.path and node.path not in trees
        }
        if paths:
            source_paths = {path: self.repo_root / path for path in sorted(paths)}
            if self.semantic_context is None:
                extra, _ = self._structure.read(source_paths)
            else:
                extra, _ = self.semantic_context.parser.read(source_paths)
            trees.update(extra)
        return trees

    # ------------------------------------------------------------------
    # Helpers (delegated from ArcGraphIndexer)
    # ------------------------------------------------------------------

    @staticmethod
    def _module_nodes(files: list[FileRecord]) -> list[Node]:
        nodes: list[Node] = []
        for file_record in files:
            nodes.append(
                Node(
                    id=module_id(file_record.module),
                    kind="module",
                    name=file_record.module.rsplit(".", 1)[-1],
                    qualname=file_record.module,
                    path=file_record.path,
                    properties={
                        "line_count": file_record.line_count,
                        "source_root": file_record.source_root,
                        "is_package": file_record.is_package,
                    },
                )
            )
        return nodes

    @staticmethod
    def _attach_module_callsites(
        nodes: list[Node], module: str, callsites: list[dict[str, object]]
    ) -> None:
        if not callsites:
            return
        node_id = module_id(module)
        for node in nodes:
            if node.id == node_id:
                node.properties["callsites"] = callsites
                return

    @staticmethod
    def _external_package_nodes(packages: set[str]) -> list[Node]:
        return [
            Node(
                id=external_package_id(package),
                kind="external_package",
                name=package,
                qualname=package,
                properties={"package": package},
            )
            for package in sorted(packages)
        ]

    @staticmethod
    def _dedupe_nodes(nodes: list[Node]) -> list[Node]:
        return EvidenceMergeEngine.dedupe_nodes(nodes)

    @staticmethod
    def _dedupe_edges(edges: list[Edge]) -> list[Edge]:
        return EvidenceMergeEngine.dedupe_edges(edges)
