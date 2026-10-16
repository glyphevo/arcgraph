"""Discarded or unpublished semantic answers cannot claim incremental reuse."""

from contextlib import contextmanager

import pytest

from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer
from arcgraph.semantic import build
from arcgraph.tests.test_semantic_incremental import (
    active_build,
    configure,
    write_files,
)
from arcgraph.tests.test_semantic_isolation import prepared as prepared


@pytest.mark.parametrize(
    "mode",
    [
        "checked",
        "request_error",
        "timeout",
        "admission",
        "artifact_hashes",
        "unexpected_exception",
        "publish",
        "cas",
        "graph_publish",
        "after_publish",
        "close",
    ],
)
def test_semantic_reason_requires_adopted_published_reuse(
    prepared, tmp_path, monkeypatch, mode
):
    project, _, _, _ = prepared
    configure(project)
    write_files(
        project,
        {
            "demo.py": "def changed(): return 1\n",
            "keep.py": "def keep(): return 1\n",
        },
    )
    output = tmp_path / "sync"
    previous = active_build(project, output)
    write_files(project, {"demo.py": "def changed(): return 2\n"})
    reused = []
    indexers = []
    before_publish_counts = []
    real_collect = build.collect
    real_build = ArcGraphIndexer._build_inner
    real_publish = build.SemanticBuild.publish

    def collect(*args, **kwargs):
        detail = real_collect(*args, **kwargs)
        reused.append(detail["reuse"]["definitions"])
        assert reused[-1] == 1, "Exercise a genuinely reused definition"
        if mode in {"request_error", "timeout"}:
            detail["requests"]["error" if mode == "request_error" else "timeout"] = 1
        return detail

    def indexing(self):
        try:
            return real_build(self)
        finally:
            indexers.append(self)

    def publishing(self, directory):
        before_publish_counts.append(self.indexer._semantic_reused_definitions)
        if mode == "publish":
            raise OSError("probe-publish")
        if mode == "cas":
            # Exercise the real expected-revision comparison without changing
            # the authoritative pointer or simulating another writer's graph.
            self.previous_pointer = b"probe-other-revision"
        return real_publish(self, directory)

    monkeypatch.setattr(build, "collect", collect)
    monkeypatch.setattr(ArcGraphIndexer, "_build_inner", indexing)
    monkeypatch.setattr(build.SemanticBuild, "publish", publishing)
    if mode in {"admission", "unexpected_exception"}:
        real_project = build.project

        def project_answers(*args, **kwargs):
            if mode == "unexpected_exception":
                raise KeyError("probe-project")
            decisions = real_project(*args, **kwargs)
            decisions["admission"][0].update(allowed=False, reason="probe-admission")
            return decisions

        monkeypatch.setattr(build, "project", project_answers)
    elif mode == "artifact_hashes":
        real_phase = build.SemanticBuild._phase

        @contextmanager
        def phase(self, name):
            with real_phase(self, name):
                if name == "artifact_hashes":
                    raise ValueError("probe-artifact-hashes")
                yield

        monkeypatch.setattr(build.SemanticBuild, "_phase", phase)
    elif mode == "graph_publish":

        def publish_graph(*args, **kwargs):
            raise OSError("probe-graph-publish")

        monkeypatch.setattr(GraphStoreWriter, "_publish_current", publish_graph)
    elif mode in {"after_publish", "close"}:
        real_method = getattr(build.SemanticBuild, mode)

        def finish(self):
            real_method(self)
            raise OSError("probe-" + mode)

        monkeypatch.setattr(build.SemanticBuild, mode, finish)

    reindexer = ArcGraphReindexer(project, output)
    errors = {
        "unexpected_exception": (KeyError, "probe-project"),
        "publish": (OSError, "probe-publish"),
        "cas": (RuntimeError, "Semantic publish revision changed"),
        "graph_publish": (OSError, "probe-graph-publish"),
        "after_publish": (OSError, "probe-after_publish"),
        "close": (OSError, "probe-close"),
    }
    if mode in errors:
        error, message = errors[mode]
        with pytest.raises(error, match=message):
            reindexer.reindex_changed()
        assert indexers[0]._semantic_reused_definitions == 0
        current = GraphStoreReader.from_current(output).sqlite_path.parent
        assert (current == previous) == (mode not in {"after_publish", "close"})
    else:
        result = reindexer.reindex_changed()
        metadata = GraphStoreReader.from_current(output).metadata
        assert result["status"] == "reindexed"
        if mode == "checked":
            assert result["reason"] == "semantic_core_incremental"
            assert indexers[0]._semantic_reused_definitions == 1
            assert metadata["toolchain_status"]["semantic_core"]["status"] == "active"
            assert metadata["evidence_manifest"]["semantic_generation"]
        else:
            assert result["reason"] == "semantic_core_full_build"
            assert indexers[0]._semantic_reused_definitions == 0
            assert (
                metadata["toolchain_status"]["semantic_core"]["status"] == "unavailable"
            )
            assert metadata["evidence_manifest"].get("semantic_generation") is None
    assert reused == [1]
    assert all(count == 0 for count in before_publish_counts)
