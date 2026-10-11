"""Storage and dependency reuse contracts for batch 27."""

import json
import sqlite3
from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import IndexMetadata, Node
from arcgraph.interfaces.cli import main
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer


def test_default_build_and_sync_keep_jsonl_on_demand(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def f(): return 1\n", encoding="utf-8")
    out = tmp_path / "out"
    _, build = ArcGraphIndexer(repo, out, [SourceRoot(".")]).build()
    assert (build / "diagnostics.jsonl").exists()
    assert not (build / "nodes.jsonl").exists()
    source.write_text("def f(): return 2\n", encoding="utf-8")
    result = ArcGraphReindexer(repo, out).reindex_changed()
    assert not (Path(result["build_dir"]) / "edges.jsonl").exists()


def test_export_round_trips_original_records(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def f(): return str(1)\n", encoding="utf-8")
    out = tmp_path / "out"
    _, build = ArcGraphIndexer(repo, out, [SourceRoot(".")]).build()
    store = GraphStoreReader.from_current(out)
    expected_nodes = [
        n.model_dump(mode="json", exclude_none=True) for n in store.read_nodes()
    ]
    expected_edges = [
        e.model_dump(mode="json", exclude_none=True) for e in store.read_edges()
    ]
    # Parser errors count as assertion failures here, rather than import failures.
    try:
        rc = main(["--output-dir", str(out), "export"])
    except SystemExit as exc:
        rc = exc.code
    assert rc == 0

    def actual(name):
        return [
            json.loads(s)
            for s in (build / name).read_text(encoding="utf-8").splitlines()
        ]

    assert actual("nodes.jsonl") == expected_nodes
    assert actual("edges.jsonl") == expected_edges
    facts = actual("semantic_facts.jsonl")
    assert len(facts) == len(expected_nodes) + len(expected_edges)
    for fact in facts:
        if fact["fact_kind"] == "node":
            node = next(n for n in expected_nodes if n["id"] == fact["node_id"])
            assert fact["properties"]["node_properties"] == node["properties"]


def test_old_storage_requires_rebuild(tmp_path):
    out = tmp_path / "out"
    build = GraphStoreWriter(out).write(
        IndexMetadata(index_version="old", repo_root=str(tmp_path), source_roots=["."]),
        [],
        [Node(id="mod:a", kind="module", name="a")],
        [],
        [],
    )
    with sqlite3.connect(build / "index.sqlite") as conn:
        conn.execute("PRAGMA user_version=0")
    conn.close()
    # Published indexes can be on a read-only filesystem: version probing must
    # use the same immutable fallback as the rest of the reader.
    paths = [*build.iterdir(), build]
    modes = {path: path.stat().st_mode & 0o777 for path in paths}
    for path in paths:
        path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        failure = None
        try:
            with pytest.raises(RuntimeError, match="Rebuild required"):
                QueryEngine(out)
        except sqlite3.OperationalError as exc:
            failure = str(exc)
        assert failure is None
    finally:
        for path, mode in modes.items():
            path.chmod(mode)
    with sqlite3.connect(build / "index.sqlite") as conn:
        conn.execute("PRAGMA user_version=1")
    conn.close()
    for path in paths:
        path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        assert len(GraphStoreReader.from_current(out).read_nodes()) == 1
    finally:
        for path, mode in modes.items():
            path.chmod(mode)


def _closure_project(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, text in {
        "provider.py": "class Base:\n    def run(self): return 1\n",
        "exports.py": "from provider import Base as Public\n",
        "consumer.py": "from exports import Public\ndef use(x: Public): return x.run()\n",
        "unrelated.py": "from typing import Any\ndef idle(x: Any): return x.nope()\n",
    }.items():
        (repo / name).write_text(text, encoding="utf-8")
    out = tmp_path / "out"
    ArcGraphIndexer(repo, out, [SourceRoot(".")]).build()
    return repo, out


def _track(monkeypatch):
    seen = []
    original = ArcGraphIndexer.analyze_files

    def track(self, files, *args, **kwargs):
        seen.extend(f.path for f in files)
        return original(self, files, *args, **kwargs)

    monkeypatch.setattr(ArcGraphIndexer, "analyze_files", track)
    return seen


def _graph(out):
    store = GraphStoreReader.from_current(out)
    return (store.read_nodes(), store.read_edges())


def test_dependency_closure_reuses_unrelated_and_refreshes_exports(
    tmp_path, monkeypatch
):
    repo, out = _closure_project(tmp_path)
    seen = _track(monkeypatch)
    (repo / "provider.py").write_text(
        "class Base:\n    def other(self): return 2\n", encoding="utf-8"
    )
    ArcGraphReindexer(repo, out).reindex_changed()
    assert {"provider.py", "exports.py", "consumer.py"} <= set(seen)
    assert "unrelated.py" not in seen
    actual = _graph(out)
    full = tmp_path / "full"
    ArcGraphIndexer(repo, full, [SourceRoot(".")]).build()
    assert actual == _graph(full)


def test_dependency_closure_unknown_generation_falls_back(tmp_path, monkeypatch):
    repo, out = _closure_project(tmp_path)
    store = GraphStoreReader.from_current(out)
    with store.connect() as conn:
        conn.execute("DELETE FROM nodes WHERE id='mod:exports'")
    seen = _track(monkeypatch)
    (repo / "provider.py").write_text(
        "class Base:\n    def other(self): return 2\n", encoding="utf-8"
    )
    failure = None
    try:
        ArcGraphReindexer(repo, out).reindex_changed()
    except RuntimeError as exc:
        failure = str(exc)
    assert failure is None
    assert set(seen) == {"provider.py", "exports.py", "consumer.py", "unrelated.py"}
    actual = _graph(out)
    full = tmp_path / "full"
    ArcGraphIndexer(repo, full, [SourceRoot(".")]).build()
    assert actual == _graph(full)


def test_empty_incremental_sources_emit_no_survivor_calls(tmp_path):
    from arcgraph.analyzers.calls import CallAnalyzer

    repo, out = _closure_project(tmp_path)
    nodes = GraphStoreReader.from_current(out).read_nodes()
    assert CallAnalyzer(enable_v2=True).analyze(nodes, source_nodes=[]).edges == []


def test_append_private_definition_reuses_existing_scopes(tmp_path, monkeypatch):
    from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer

    repo, out = _closure_project(tmp_path)
    seen = []
    original = PythonGraphAnalyzer.analyze

    def track(self, *args, **kwargs):
        fragment = original(self, *args, **kwargs)
        seen.extend(n.name for n in fragment.nodes if n.kind in {"function", "method"})
        return fragment

    monkeypatch.setattr(PythonGraphAnalyzer, "analyze", track)
    with (repo / "consumer.py").open("a", encoding="utf-8") as handle:
        handle.write("\n\ndef _private_probe(x):\n    return x.commit()\n")
    ArcGraphReindexer(repo, out).reindex_changed()
    assert seen == ["_private_probe"]
    actual = _graph(out)
    full = tmp_path / "full"
    ArcGraphIndexer(repo, full, [SourceRoot(".")]).build()
    assert actual == _graph(full)


def test_compact_storage_preserves_fact_overrides(tmp_path):
    from arcgraph.core.schemas import Edge, SemanticFact

    node = Node(
        id="mod:a",
        kind="module",
        name="a",
        properties={
            "payload": ["abc" * 1000] * 20,
            "large_int": 2**100,
            "negative_zero": -0.0,
            "unicode": "汉字😀",
        },
    )
    edge = Edge(source=node.id, target=node.id, kind="calls")
    fact = SemanticFact(
        schema_version="custom-schema",
        fact_id="custom",
        index_version="small",
        frontend_name="custom",
        frontend_version="1",
        fact_kind="edge",
        source=node.id,
        target=node.id,
        edge_kind="calls",
        properties={"custom": [1, 2, 3]},
        resolution={"status": "unresolved"},
    )
    out = tmp_path / "out"
    build = GraphStoreWriter(out).write(
        IndexMetadata(
            index_version="small", repo_root=str(tmp_path), source_roots=["."]
        ),
        [],
        [node],
        [edge],
        [],
        semantic_facts=[fact],
    )
    assert not (build / "semantic_facts.jsonl").exists()
    reader = GraphStoreReader.from_current(out)
    assert reader.read_nodes() == [node]
    assert list(reader.iter_semantic_facts()) == [fact]
    changed = reader.read_nodes()[0]
    changed.properties = dict(reversed(list(changed.properties.items())))
    changed.properties["large_int"] = 2**101
    reused_out = tmp_path / "reused"
    GraphStoreWriter(reused_out).write(
        IndexMetadata(
            index_version="reordered", repo_root=str(tmp_path), source_roots=["."]
        ),
        [],
        [changed],
        [],
        [],
        encoded_payloads=reader.encoded_payloads,
    )
    assert GraphStoreReader.from_current(reused_out).read_nodes() == [changed]
    import math

    assert math.copysign(1, reader.read_nodes()[0].properties["negative_zero"]) == -1
    # stdlib accepts lone escaped surrogates; the faster parser must fall back.
    with reader.connect() as conn:
        conn.execute(
            "UPDATE nodes SET properties_json=?",
            (json.dumps({"value": "\ud800"}).encode("utf-8"),),
        )
    assert reader.read_nodes()[0].properties == {"value": "\ud800"}


def test_append_absence_proof_covers_unicode_and_old_annotations(tmp_path, monkeypatch):
    from arcgraph.pipeline.python_frontend import PythonGraphAnalyzer

    repo, out = _closure_project(tmp_path)
    seen = []
    original = PythonGraphAnalyzer.analyze

    def track(self, *args, **kwargs):
        fragment = original(self, *args, **kwargs)
        seen.extend(n.name for n in fragment.nodes if n.kind in {"function", "method"})
        return fragment

    monkeypatch.setattr(PythonGraphAnalyzer, "analyze", track)
    source = repo / "consumer.py"
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n\ndef _私有(x):\n    return x.commit()\n")
    ArcGraphReindexer(repo, out).reindex_changed()
    assert seen == ["_私有"]
    actual = _graph(out)
    full = tmp_path / "unicode-full"
    ArcGraphIndexer(repo, full, [SourceRoot(".")]).build()
    assert actual == _graph(full)

    # An old forward annotation is an observed name even without a resolved call.
    source.write_text('def use(x: "_注解"): return x.run()\n', encoding="utf-8")
    ArcGraphIndexer(repo, out, [SourceRoot(".")]).build()
    seen.clear()
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n\ndef _注解(x):\n    return x.commit()\n")
    ArcGraphReindexer(repo, out).reindex_changed()
    assert "use" in seen
    actual = _graph(out)
    full = tmp_path / "observed-full"
    ArcGraphIndexer(repo, full, [SourceRoot(".")]).build()
    assert actual == _graph(full)

    # Even inert additions cannot reuse an incomplete old module generation.
    with GraphStoreReader.from_current(out).connect() as conn:
        conn.execute("DELETE FROM nodes WHERE id='mod:exports'")
    seen.clear()
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n\ndef _unobserved(x):\n    return x.commit()\n")
    failure = None
    try:
        ArcGraphReindexer(repo, out).reindex_changed()
    except RuntimeError as exc:
        failure = str(exc)
    assert failure is None
    assert "idle" in seen
    actual = _graph(out)
    full = tmp_path / "unknown-append-full"
    ArcGraphIndexer(repo, full, [SourceRoot(".")]).build()
    assert actual == _graph(full)
