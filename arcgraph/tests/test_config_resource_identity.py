import json
from pathlib import Path

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.reindexer import ArcGraphReindexer


def _resources(output: Path):
    reader = GraphStoreReader.from_current(output)
    nodes = {n.id: n for n in reader.read_nodes() if n.kind == "config"}
    edges = [e for e in reader.read_edges() if e.target in nodes]
    return nodes, edges


def test_dynamic_config_keys_are_distinct_and_shared_literals_are_pathless(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("alpha", "beta"):
        (repo / f"{name}.py").write_text(
            "import os\ndef read(key):\n"
            "    os.environ.get(key)\n    os.environ.get(key)\n"
            "    return os.environ.get('SHARED')\n"
        )
    output = tmp_path / "index"
    roots = [SourceRoot(".")]
    ArcGraphIndexer(repo, output, roots).build()
    nodes, edges = _resources(output)
    dynamic = {key: n for key, n in nodes.items() if key.startswith("config:dynamic:")}
    assert len(dynamic) == 4
    assert "config:get" not in nodes
    assert all(n.properties["key_resolution"] == "unknown" for n in dynamic.values())
    assert all(n.path is None and n.start_line is None for n in nodes.values())
    shared = [e for e in edges if e.target == "config:env:SHARED"]
    assert {e.source for e in shared} == {"fn:alpha.read", "fn:beta.read"}
    assert {v.path for e in shared for v in e.evidence} == {"alpha.py", "beta.py"}
    for key in dynamic:
        refs = [e for e in edges if e.target == key]
        assert len(refs) == 1
        assert (
            refs[0].evidence[0].path
            == refs[0].source.removeprefix("fn:").split(".")[0] + ".py"
        )

    # Moving lines keeps semantic access identities; deleting references removes
    # pathless synthetic resources. Both incremental stages must match a rebuild.
    for step in ("move", "delete", "replace"):
        path = repo / "alpha.py"
        if step == "move":
            path.write_text("\n\n" + path.read_text())
        elif step == "delete":
            path.unlink()
        else:
            (repo / "beta.py").write_text(
                "import os\ndef read(key):\n    return os.getenv('OTHER')\n"
            )
        ArcGraphReindexer(repo, output, roots).reindex_changed()
        full = tmp_path / f"full-{step}"
        ArcGraphIndexer(repo, full, roots).build()
        actual_nodes, actual_edges = _resources(output)
        expected_nodes, expected_edges = _resources(full)
        assert actual_nodes == expected_nodes
        assert _edge_state(actual_edges) == _edge_state(expected_edges)
        if step == "move":
            assert set(actual_nodes) == set(nodes)
        elif step == "delete":
            assert len(actual_nodes) == 3
        else:
            assert set(actual_nodes) == {"config:env:OTHER"}


def _edge_state(edges):
    result = []
    for edge in edges:
        record = edge.model_dump()
        props = record["properties"]
        # Incremental dedupe may materialize the singleton callsites alias.
        if props.get("callsites") == [props.get("callsite")]:
            props.pop("callsites")
        result.append(json.dumps(record, sort_keys=True))
    return sorted(result)
