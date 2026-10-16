"""Sync reasons describe recorded semantic reuse, including zero-reuse runs."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.semantic.journal import load
from arcgraph.tests.test_semantic_incremental import (
    active_build,
    configure,
    generation_directory,
    write_files,
)
from arcgraph.tests.test_semantic_isolation import prepared as prepared


@pytest.mark.parametrize(
    "mode", ["checked", "all_affected", "artifact", "configuration"]
)
def test_semantic_sync_reason_matches_recorded_reuse(prepared, tmp_path, mode):
    project, _, _, _ = prepared
    configure(project)
    write_files(project, {"demo.py": "def changed(): return 1\n"})
    if mode != "all_affected":
        write_files(project, {"unrelated.py": "def keep(): return 1\n"})
    output = tmp_path / "sync"
    initial = active_build(project, output)
    write_files(project, {"demo.py": "def changed(): return 2\n"})
    if mode == "artifact":
        path = generation_directory(initial) / "responses.jsonl"
        path.write_bytes(path.read_bytes() + b"tampered")
    elif mode == "configuration":
        path = project / "pyproject.toml"
        path.write_text(
            path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8"
        )

    result = ArcGraphReindexer(project, output).reindex_changed()
    current = GraphStoreReader.from_current(output).sqlite_path.parent
    detail = load(generation_directory(current) / "transport.json")
    assert result["status"] == "reindexed"
    if mode == "checked":
        assert detail["reuse"]["definitions"] == 1
        assert result["reason"] == "semantic_core_incremental"
    else:
        if mode == "all_affected":
            # A reuse object exists, but every definition needed a fresh answer.
            assert detail["reuse"]["definitions"] == 0
        else:
            assert detail.get("reuse") is None
        assert result["reason"] == "semantic_core_full_build"


def test_semantic_unchanged_sync_omits_reason(prepared, tmp_path):
    project, _, _, _ = prepared
    configure(project)
    output = tmp_path / "sync"
    active_build(project, output)
    result = ArcGraphReindexer(project, output).reindex_changed()
    assert result["status"] == "unchanged"
    assert "reason" not in result
