"""Runtime trace import operations for ArcGraph."""

from __future__ import annotations

import json
import os
import sys
import time
import subprocess
import tempfile
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
from arcgraph.core.project_environment import project_interpreter_path
from arcgraph.interfaces.trace_recorder import _RuntimeTraceRecorder
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
    python: str | Path | None = None,
) -> dict[str, Any]:
    """Run pytest under a lightweight project-code call tracer."""

    interpreter = project_interpreter_path(repo_root.resolve(), python)
    if interpreter is not None:
        return _run_project_trace(
            repo_root=repo_root.resolve(),
            output_path=output_path,
            pytest_args=pytest_args,
            source_roots=source_roots,
            max_events=max_events,
            max_seconds=max_seconds,
            interpreter=interpreter,
            interpreter_source="explicit" if python else "project_environment",
        )

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
        "python_executable": sys.executable,
        "python_prefix": sys.prefix,
        "python_version": sys.version.split()[0],
        "interpreter_source": "host_fallback",
        "environment_diagnostics": [
            {
                "kind": "project_interpreter_missing",
                "message": "No project interpreter found; used the ArcGraph host environment.",
            }
        ],
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
        "interpreter_source": "host_fallback",
        "python_executable": sys.executable,
        "environment_diagnostics": trace_run["environment_diagnostics"],
    }


def _run_project_trace(
    *,
    repo_root,
    output_path,
    pytest_args,
    source_roots,
    max_events,
    max_seconds,
    interpreter,
    interpreter_source,
):
    output = Path(output_path)
    if not output.is_absolute():
        output = repo_root / output
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="arcgraph-project-trace-") as temporary:
        root = Path(temporary)
        result_path = root / "result.json"
        config = {
            "repo_root": str(repo_root),
            "output": str(output),
            "result": str(result_path),
            "pytest_args": pytest_args,
            "max_events": max_events,
            "max_seconds": max_seconds,
            "source_roots": [
                {"path": r.path, "module_prefix": r.module_prefix} for r in source_roots
            ],
            "commit_sha": current_commit(repo_root),
            "interpreter_source": interpreter_source,
        }
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["PYTHONNOUSERSITE"] = "1"
        runner = Path(__file__).with_name("trace_recorder.py")
        result = subprocess.run(
            [str(interpreter), "-B", str(runner), str(config_path)],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode or not result_path.is_file():
            raise RuntimeError(
                "Project pytest tracer failed: "
                + (result.stderr or result.stdout)[-2000:]
            )
        summary = json.loads(result_path.read_text(encoding="utf-8"))
    return {
        "schema_version": SCHEMA_VERSION,
        **summary,
        "path": str(output),
        "interpreter_source": interpreter_source,
        "python_executable": str(interpreter),
    }


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
