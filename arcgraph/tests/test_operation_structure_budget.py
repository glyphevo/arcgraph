"""Preflight, parsing and cache writes share one failed-read budget per operation."""

import ast
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from arcgraph.tests.project_environment_support import runtime_environment

from arcgraph.core import structure
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.ops import watch_index
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer


@pytest.fixture
def workload(tmp_path, monkeypatch):
    root = tmp_path / "project"
    runtime_environment(root)
    for number in range(1281):
        (root / f"m{number:04d}.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(structure, "PROJECT_FAILURE_BUDGET", 0.2)
    mode = ["good"]
    attempts = []

    def read(view, paths):
        if (
            not paths
            or mode[0] == "good"
            or (mode[0] == "partial" and all(name < "m0256.py" for name in paths))
        ):
            view.version = sys.version.split()[0]
            return {
                name: (
                    ast.parse(Path(path).read_text(encoding="utf-8"), filename=name),
                    None,
                )
                for name, path in paths.items()
            }
        started = time.monotonic()
        time.sleep(view._project_timeout)
        attempts.append((time.monotonic() - started, view._project_failure_seconds))
        raise subprocess.TimeoutExpired([str(view.interpreter)], view._project_timeout)

    monkeypatch.setattr(structure.StructureView, "_read_project", read)
    return root, tmp_path / "out", mode, attempts


def build(workload, mode):
    root, output, selected, _ = workload
    selected[0] = mode
    return ArcGraphIndexer(root, output, [SourceRoot(".")]).build()


def assert_one_budget(attempts):
    assert sum(seconds for seconds, _ in attempts) <= 1.10 * (
        structure.PROJECT_FAILURE_BUDGET
    ), "One operation spent multiple failed-read budgets"
    assert attempts and all(budget is attempts[0][1] for _, budget in attempts)


def test_partial_exhaustion_reason_preserves_only_completed_project_blocks(workload):
    metadata, _ = build(workload, "partial")
    state = metadata.toolchain_status["python_structure"]
    assert state["status"] == "partial_fallback"
    assert "failed-read budget is exhausted" in state["reason"]
    assert "other blocks use the project interpreter" not in state["reason"]
    assert "completed blocks retain project interpreter results" in state["reason"]
    assert "blocks not completed by the project interpreter" in state["reason"]


def test_exhausted_build_keeps_probed_version_and_no_change_sync_is_unchanged(workload):
    root, output, _, attempts = workload
    build(workload, "hang")
    cached = json.loads(GraphStoreReader.from_current(output).python_source(""))
    attempts.clear()
    result = ArcGraphReindexer(root, output).reindex_changed()
    assert result["status"] == "unchanged", result
    assert cached["version"] == sys.version.split()[0]
    assert_one_budget(attempts)
    assert structure._build_failure_seconds.get() is None


@pytest.mark.parametrize("operation", ["sync", "watch", "full_rebuild"])
def test_sync_or_watch_preflight_and_analysis_share_one_budget(workload, operation):
    root, output, selected, attempts = workload
    # A healthy stored cache is used for the full-rebuild case; the interpreter's
    # external behavior then changes without changing its executable identity.
    build(workload, "good" if operation == "full_rebuild" else "hang")
    selected[0] = "hang"
    if operation != "full_rebuild":
        (root / "m0000.py").write_text("x = 2\n", encoding="utf-8")
    attempts.clear()
    if operation == "watch":
        watch_index(
            repo_root=root,
            output_dir=output,
            poll_interval_seconds=0.01,
            debounce_seconds=0,
            max_cycles=2,
        )
    else:
        result = ArcGraphReindexer(root, output).reindex_changed()
        if operation == "full_rebuild":
            assert result["status"] == "reindexed"
    assert_one_budget(attempts)
    assert structure._build_failure_seconds.get() is None


def test_enabled_sync_full_build_uses_operation_budget(workload, monkeypatch):
    from arcgraph.semantic import runtime

    root, output, selected, attempts = workload
    build(workload, "good")
    store = GraphStoreReader.from_current(output)
    monkeypatch.setattr(runtime, "semantic_core_enabled", lambda root: True)
    selected[0] = "hang"

    def full(indexer):
        # Exercise the semantic_full branch with repeated structural views;
        # transport and graph publication are independent of the budget boundary.
        for _ in range(2):
            view = structure.StructureView(indexer.repo_root)
            view.read({"m0000.py": root / "m0000.py"})
        return SimpleNamespace(**store.metadata), store.sqlite_path.parent

    monkeypatch.setattr(ArcGraphIndexer, "_build_inner", full)
    result = ArcGraphReindexer(root, output).reindex_changed(
        cleanup_after_publish=False
    )
    # The full-build stub never collects or reuses semantic responses.
    assert result["reason"] == "semantic_core_full_build"
    assert_one_budget(attempts)
    assert structure._build_failure_seconds.get() is None
