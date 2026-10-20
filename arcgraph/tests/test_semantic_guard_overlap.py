"""Full builds can overlap preparation; reuse must wait for closed evidence."""

from threading import Event

import pytest

from arcgraph.tests.time_limits import WAIT_HANG_SECONDS
from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.semantic import build
from arcgraph.semantic.journal import load
from arcgraph.tests.test_semantic_incremental import (
    active_build,
    configure,
    generation_directory,
    write_files,
)
from arcgraph.tests.test_semantic_isolation import prepared as prepared


@pytest.mark.parametrize(
    "mode",
    [
        "full",
        "sync",
        "collect_failure",
        "guard_failure",
        "guard_exception",
        "worker_unavailable",
    ],
)
def test_external_guard_completion_and_collection_order(
    prepared, tmp_path, monkeypatch, mode
):
    project, _, _, _ = prepared
    configure(project)
    write_files(
        project,
        {"demo.py": "def changed(): return 1\n", "keep.py": "def keep(): return 1\n"},
    )
    output = tmp_path / "out"
    if mode == "sync":
        active_build(project, output)
        write_files(project, {"demo.py": "def changed(): return 2\n"})
    collect_started, guard_done = Event(), Event()
    overlaps, collected_after_guard, counts = [], [], []
    real_guard, real_collect = build.guard_external_dependencies, build.collect

    def guard(*args, **kwargs):
        try:
            if mode != "sync":
                # Without a worker, collection runs only after this guard, so the
                # wait can only time out; elsewhere it returns as soon as
                # collection starts.
                overlaps.append(
                    collect_started.wait(
                        2 if mode == "worker_unavailable" else WAIT_HANG_SECONDS
                    )
                )
            if mode == "guard_failure":
                raise ValueError("probe-guard-failed")
            if mode == "guard_exception":
                raise TypeError("probe-guard-exception")
            return real_guard(*args, **kwargs)
        finally:
            guard_done.set()

    def collect(*args, **kwargs):
        collected_after_guard.append(guard_done.is_set())
        collect_started.set()
        if mode == "collect_failure":
            raise ValueError("probe-collect-failed")
        detail = real_collect(*args, **kwargs)
        counts.append((detail.get("reuse") or {}).get("definitions", 0))
        return detail

    def unavailable(*args, **kwargs):
        raise RuntimeError("probe-no-worker")

    monkeypatch.setattr(build, "guard_external_dependencies", guard)
    monkeypatch.setattr(build, "collect", collect)
    if mode == "worker_unavailable":
        monkeypatch.setattr(build, "ThreadPoolExecutor", unavailable, raising=False)
    if mode == "guard_exception":
        indexer = ArcGraphIndexer(project, output)
        with pytest.raises(TypeError, match="probe-guard-exception"):
            indexer.build()
        assert guard_done.is_set()
        assert indexer._semantic_reused_definitions == 0
        assert not (output / "current.json").exists()
        return
    if mode == "sync":
        result = ArcGraphReindexer(project, output).reindex_changed()
        assert result["reason"] == "semantic_core_incremental"
        assert collected_after_guard == [True]
        assert counts == [1]
    else:
        ArcGraphIndexer(project, output).build()
        assert overlaps == [
            mode != "worker_unavailable"
        ], "A full build must start collection while guard preparation runs"
        assert collected_after_guard == [mode == "worker_unavailable"]
    assert guard_done.is_set(), "No guard may outlive its captured inputs"
    store = GraphStoreReader.from_current(output)
    status = store.metadata["toolchain_status"]["semantic_core"]["status"]
    if mode == "collect_failure":
        assert status == "unavailable"
        assert store.metadata["evidence_manifest"].get("semantic_generation") is None
    else:
        assert status == "active"
        proof = load(generation_directory(store.sqlite_path.parent) / "reuse.json")
        assert (proof["dependencies"] is None) == (mode == "guard_failure")
