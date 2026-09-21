"""Runtime trace import operations for ArcGraph."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from arcgraph.analyzers.har_trace import HarNetworkImporter
from arcgraph.analyzers.otel_trace import OtelSpanConverter
from arcgraph.analyzers.runtime_trace import RuntimeTraceImporter
from arcgraph.pipeline.frontends import python_compat_pass_dispatcher
from arcgraph.core.evidence_manifest import build_evidence_manifest
from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.pipeline.indexer import ArcGraphIndexer, IndexContent
from arcgraph.core.merge import EvidenceMergeEngine
from arcgraph.core.metadata import current_commit, make_index_version
from arcgraph.core.operation_lock import arcgraph_operation_lock
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import SCHEMA_VERSION, IndexMetadata


def import_runtime_trace(
    *,
    repo_root: Path,
    output_dir: Path,
    trace_path: str | Path,
    max_events: int = 50_000,
    max_file_bytes: int = 10 * 1024 * 1024,
    max_seconds: float = 120.0,
) -> dict[str, Any]:
    """Import runtime trace evidence into a new current ArcGraph build."""
    with arcgraph_operation_lock(output_dir):
        return _import_runtime_trace_inner(
            repo_root=repo_root,
            output_dir=output_dir,
            trace_path=trace_path,
            max_events=max_events,
            max_file_bytes=max_file_bytes,
            max_seconds=max_seconds,
        )


def _import_runtime_trace_inner(
    *,
    repo_root: Path,
    output_dir: Path,
    trace_path: str | Path,
    max_events: int,
    max_file_bytes: int,
    max_seconds: float,
) -> dict[str, Any]:

    store = GraphStoreReader.from_current(output_dir)
    if store.metadata.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            f"ArcGraph schema mismatch: index has {store.metadata.get('schema_version')}, "
            f"runtime expects {SCHEMA_VERSION}. Rebuild required before trace import."
        )

    files = store.read_files()
    nodes = store.read_nodes()
    edges = store.read_edges()
    warnings = store.read_warnings()
    commit_sha = current_commit(repo_root)
    analysis = RuntimeTraceImporter(
        repo_root,
        trace_path,
        commit_sha=commit_sha,
        index_version=str(store.metadata.get("index_version") or ""),
        max_events=max_events,
        max_file_bytes=max_file_bytes,
        max_seconds=max_seconds,
    ).analyze(nodes)
    return _write_runtime_analysis(
        repo_root=repo_root,
        output_dir=output_dir,
        trace_path=trace_path,
        action="runtime_trace_imported",
        store=store,
        files=files,
        nodes=nodes,
        edges=edges,
        warnings=warnings,
        commit_sha=commit_sha,
        analysis=analysis,
    )


def import_otel_spans(
    *,
    repo_root: Path,
    output_dir: Path,
    span_path: str | Path,
    max_spans: int = 50_000,
    max_file_bytes: int = 10 * 1024 * 1024,
    max_seconds: float = 120.0,
) -> dict[str, Any]:
    """Import offline OpenTelemetry spans as runtime-only evidence."""
    with arcgraph_operation_lock(output_dir):
        return _import_otel_spans_inner(
            repo_root=repo_root,
            output_dir=output_dir,
            span_path=span_path,
            max_spans=max_spans,
            max_file_bytes=max_file_bytes,
            max_seconds=max_seconds,
        )


def import_har_network(
    *,
    repo_root: Path,
    output_dir: Path,
    har_path: str | Path,
    max_entries: int = 50_000,
    max_file_bytes: int = 10 * 1024 * 1024,
    max_seconds: float = 120.0,
) -> dict[str, Any]:
    """Import offline HAR network entries as runtime-only evidence."""
    with arcgraph_operation_lock(output_dir):
        return _import_har_network_inner(
            repo_root=repo_root,
            output_dir=output_dir,
            har_path=har_path,
            max_entries=max_entries,
            max_file_bytes=max_file_bytes,
            max_seconds=max_seconds,
        )


def _import_har_network_inner(
    *,
    repo_root: Path,
    output_dir: Path,
    har_path: str | Path,
    max_entries: int,
    max_file_bytes: int,
    max_seconds: float,
) -> dict[str, Any]:
    store = GraphStoreReader.from_current(output_dir)
    if store.metadata.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            f"ArcGraph schema mismatch: index has {store.metadata.get('schema_version')}, "
            f"runtime expects {SCHEMA_VERSION}. Rebuild required before trace import."
        )

    files = store.read_files()
    nodes = store.read_nodes()
    edges = store.read_edges()
    warnings = store.read_warnings()
    commit_sha = current_commit(repo_root)
    index_version = str(store.metadata.get("index_version") or "")
    analysis = HarNetworkImporter(
        repo_root=repo_root,
        har_path=har_path,
        commit_sha=commit_sha,
        index_version=index_version,
        max_entries=max_entries,
        max_file_bytes=max_file_bytes,
        max_seconds=max_seconds,
    ).analyze(nodes)
    return _write_runtime_analysis(
        repo_root=repo_root,
        output_dir=output_dir,
        trace_path=har_path,
        action="har_network_imported",
        store=store,
        files=files,
        nodes=nodes,
        edges=edges,
        warnings=warnings,
        commit_sha=commit_sha,
        analysis=analysis,
    )


def _import_otel_spans_inner(
    *,
    repo_root: Path,
    output_dir: Path,
    span_path: str | Path,
    max_spans: int,
    max_file_bytes: int,
    max_seconds: float,
) -> dict[str, Any]:
    store = GraphStoreReader.from_current(output_dir)
    if store.metadata.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            f"ArcGraph schema mismatch: index has {store.metadata.get('schema_version')}, "
            f"runtime expects {SCHEMA_VERSION}. Rebuild required before trace import."
        )

    files = store.read_files()
    nodes = store.read_nodes()
    edges = store.read_edges()
    warnings = store.read_warnings()
    commit_sha = current_commit(repo_root)
    index_version = str(store.metadata.get("index_version") or "")
    conversion = OtelSpanConverter(
        repo_root=repo_root,
        span_path=span_path,
        commit_sha=commit_sha,
        index_version=index_version,
        max_spans=max_spans,
        max_file_bytes=max_file_bytes,
        max_seconds=max_seconds,
    ).convert()
    analysis = RuntimeTraceImporter(
        repo_root,
        span_path,
        commit_sha=commit_sha,
        index_version=index_version,
        max_events=max_spans,
        max_file_bytes=max_file_bytes,
        max_seconds=max_seconds,
    ).analyze_payload(conversion.payload, nodes, trace_path=span_path)
    analysis.metrics.update(conversion.metrics)
    analysis.metrics["status"] = analysis.status
    return _write_runtime_analysis(
        repo_root=repo_root,
        output_dir=output_dir,
        trace_path=span_path,
        action="otel_span_imported",
        store=store,
        files=files,
        nodes=nodes,
        edges=edges,
        warnings=warnings,
        commit_sha=commit_sha,
        analysis=analysis,
    )


def _write_runtime_analysis(
    *,
    repo_root: Path,
    output_dir: Path,
    trace_path: str | Path,
    action: str,
    store: GraphStoreReader,
    files: list[Any],
    nodes: list[Any],
    edges: list[Any],
    warnings: list[Any],
    commit_sha: str | None,
    analysis: Any,
) -> dict[str, Any]:

    content = IndexContent(
        files=files,
        nodes=[*nodes, *analysis.nodes],
        edges=[*edges, *analysis.edges],
        warnings=[*warnings, *analysis.warnings],
        adapter_metrics=store.metadata.get("adapter_metrics", {}),
        precision_metrics=store.metadata.get("precision_metrics", {}),
        coverage_metrics=store.metadata.get("coverage_metrics", {}),
        runtime_metrics=analysis.metrics,
    )
    content.nodes = ArcGraphIndexer.dedupe_nodes(content.nodes)
    content.edges = ArcGraphIndexer.dedupe_edges(content.edges)

    metadata = IndexMetadata(
        index_version=make_index_version(commit_sha),
        repo_root=str(repo_root),
        source_roots=list(store.metadata.get("source_roots", [])),
        source_root_specs=list(store.metadata.get("source_root_specs", [])),
        source_root_detection=store.metadata.get("source_root_detection"),
        project_name=store.metadata.get("project_name"),
        project_name_source=store.metadata.get("project_name_source"),
        project_version=store.metadata.get("project_version"),
        project_version_source=store.metadata.get("project_version_source"),
        commit_sha=commit_sha,
        evidence_manifest=build_evidence_manifest(
            repo_root=repo_root,
            output_dir=output_dir,
            commit_sha=commit_sha,
            source_roots=list(store.metadata.get("source_roots", [])),
            precision_metrics=store.metadata.get("precision_metrics", {}),
            coverage_metrics=store.metadata.get("coverage_metrics", {}),
            runtime_metrics=analysis.metrics,
            runtime_trace_path=trace_path,
        ),
        file_count=len(content.files),
        node_count=len(content.nodes),
        edge_count=len(content.edges),
        warning_count=len(content.warnings),
    )
    capabilities = store.metadata.get("capabilities", {})
    if isinstance(capabilities, dict):
        metadata.capabilities.update(capabilities)
    ArcGraphIndexer.update_metadata_counts(metadata, content)

    artifacts = EvidenceMergeEngine(
        repo_root,
        dispatcher=python_compat_pass_dispatcher(),
    ).merge_graph(
        metadata,
        content.nodes,
        content.edges,
        content.warnings,
        full_rebuild=False,
    )
    content.nodes = artifacts.nodes
    content.edges = artifacts.edges
    ArcGraphIndexer.update_metadata_counts(metadata, content)

    build_dir = GraphStoreWriter(output_dir).write(
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
    )
    return {
        "schema_version": metadata.schema_version,
        "index_version": metadata.index_version,
        "status": analysis.status,
        "action": action,
        "build_dir": str(build_dir),
        "runtime_metrics": analysis.metrics,
        "warnings": [warning.model_dump(mode="json") for warning in analysis.warnings],
    }


def run_runtime_trace(
    *,
    repo_root: Path,
    output_path: str | Path,
    pytest_args: list[str],
    source_roots: list[SourceRoot],
    max_events: int = 50_000,
    max_seconds: float = 120.0,
) -> dict[str, Any]:
    """Run pytest under a lightweight project-code call tracer."""

    try:
        import pytest
    except ImportError as exc:
        raise RuntimeError("pytest is required for ArcGraph trace run") from exc

    repo_root = repo_root.resolve()
    output = (
        (repo_root / output_path).resolve()
        if not Path(output_path).is_absolute()
        else Path(output_path)
    )

    class _PytestRuntimeTraceRecorder(_RuntimeTraceRecorder):
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(self, item: Any) -> Any:
            yield from self.trace_test_call(item)

    recorder = _PytestRuntimeTraceRecorder(
        repo_root=repo_root,
        source_roots=source_roots,
        max_events=max_events,
        max_seconds=max_seconds,
    )
    started = time.monotonic()
    previous_cwd = Path.cwd()
    previous_modules = set(sys.modules)
    added_paths: list[str] = []
    try:
        os.chdir(repo_root)
        for source_root in reversed(source_roots):
            source_path = str((repo_root / source_root.path).resolve())
            if source_path not in sys.path:
                sys.path.insert(0, source_path)
                added_paths.append(source_path)
        exit_code = pytest.main(pytest_args, plugins=[recorder])
    finally:
        for source_path in added_paths:
            try:
                sys.path.remove(source_path)
            except ValueError:
                pass
        os.chdir(previous_cwd)
        _purge_new_repo_modules(repo_root, previous_modules)
    duration_seconds = time.monotonic() - started
    trace_run = {
        "trace_run_id": f"pytest-{int(time.time() * 1000)}",
        "source": "pytest",
        "commit_sha": current_commit(repo_root),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "frontend_version": "ArcGraph-runtime-trace-v1",
        "duration_seconds": round(duration_seconds, 4),
    }
    payload = {
        "trace_run": trace_run,
        "events": recorder.events,
        "diagnostics": recorder.diagnostics,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if exit_code == 0 else "fail",
        "pytest_exit_code": int(exit_code),
        "path": str(output),
        "events": len(recorder.events),
        "truncated_events": recorder.truncated_events,
        "duration_seconds": round(duration_seconds, 4),
    }


class _RuntimeTraceRecorder:
    def __init__(
        self,
        *,
        repo_root: Path,
        source_roots: list[SourceRoot],
        max_events: int,
        max_seconds: float,
    ) -> None:
        self.repo_root = repo_root
        self.source_roots = source_roots
        self.max_events = max(1, max_events)
        self.max_seconds = max_seconds
        self.events: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.truncated_events = 0
        self._current_test_id: str | None = None
        self._current_test_symbol: str | None = None
        self._started = 0.0

    def trace_test_call(self, item: Any) -> Any:
        self._current_test_id = str(item.nodeid)
        self._current_test_symbol = self._test_symbol(item)
        self._started = time.monotonic()
        previous_sys_profile = sys.getprofile()
        previous_thread_profile = threading.getprofile()
        sys.setprofile(self._profile)
        threading.setprofile(self._profile)
        try:
            yield
        finally:
            sys.setprofile(previous_sys_profile)
            threading.setprofile(previous_thread_profile)
            self._current_test_id = None
            self._current_test_symbol = None

    def _profile(self, frame: Any, event: str, arg: Any) -> None:
        if event != "call":
            return
        if len(self.events) >= self.max_events:
            self.truncated_events += 1
            return
        if time.monotonic() - self._started > self.max_seconds:
            self.diagnostics.append(
                {
                    "kind": "wall_time_limit_exceeded",
                    "message": f"Runtime trace exceeded max_seconds={self.max_seconds}.",
                    "path": self._frame_path(frame),
                }
            )
            sys.setprofile(None)
            return
        source = self._frame_symbol(frame.f_back) if frame.f_back is not None else None
        target = self._frame_symbol(frame)
        if target == self._current_test_symbol:
            return
        if source is None and target is not None:
            source = self._current_test_symbol
        if source is None or target is None or source == target:
            return
        path = self._frame_path(frame)
        if path is None:
            return
        self.events.append(
            {
                "kind": "call",
                "source": source,
                "target": target,
                "path": path,
                "line": frame.f_lineno,
                "test_id": self._current_test_id,
            }
        )

    def _frame_symbol(self, frame: Any) -> str | None:
        path = self._frame_path(frame)
        if path is None:
            return None
        module = self._module_for_path(path)
        if module is None:
            return None
        function_name = frame.f_code.co_name
        qualname = getattr(frame.f_code, "co_qualname", function_name)
        if function_name == "<module>":
            return f"mod:{module}"
        if function_name.startswith("<") or "<locals>" in qualname:
            return None
        if "." in qualname:
            return f"method:{module}.{qualname}"
        return f"fn:{module}.{function_name}"

    def _frame_path(self, frame: Any) -> str | None:
        filename = frame.f_code.co_filename
        try:
            resolved = Path(filename).resolve()
            relative = resolved.relative_to(self.repo_root)
        except (OSError, ValueError):
            return None
        if self._is_ignored_runtime_path(relative):
            return None
        return relative.as_posix()

    def _test_symbol(self, item: Any) -> str | None:
        test_obj = getattr(item, "obj", None)
        item_path = getattr(item, "path", None) or getattr(item, "fspath", None)
        if item_path is None:
            return None
        try:
            relative_path = Path(item_path).resolve().relative_to(self.repo_root)
        except (OSError, ValueError):
            return None
        module = self._module_for_path(relative_path.as_posix())
        qualname = getattr(test_obj, "__qualname__", None)
        if not isinstance(module, str) or not module:
            return None
        if not isinstance(qualname, str) or not qualname:
            return None
        if "<locals>" in qualname or qualname.startswith("<"):
            return None
        return (
            f"method:{module}.{qualname}"
            if "." in qualname
            else f"fn:{module}.{qualname}"
        )

    def _module_for_path(self, path: str) -> str | None:
        for source_root in self.source_roots:
            root = Path(source_root.path)
            try:
                relative = Path(path).relative_to(root)
            except ValueError:
                continue
            if relative.suffix != ".py":
                return None
            parts = list(relative.parts)
            parts[-1] = relative.stem
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if source_root.module_prefix:
                parts.insert(0, source_root.module_prefix)
            return ".".join(parts)
        return None

    @staticmethod
    def _is_ignored_runtime_path(path: Path) -> bool:
        ignored_parts = {
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "__pycache__",
            "node_modules",
            "site-packages",
            "venv",
        }
        return any(part in ignored_parts for part in path.parts)


def _purge_new_repo_modules(repo_root: Path, previous_modules: set[str]) -> None:
    """Remove modules pytest imported from the traced repo during this run."""
    for module_name, module in list(sys.modules.items()):
        if module_name in previous_modules:
            continue
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str) or not module_file:
            continue
        try:
            Path(module_file).resolve().relative_to(repo_root)
        except (OSError, ValueError):
            continue
        sys.modules.pop(module_name, None)
