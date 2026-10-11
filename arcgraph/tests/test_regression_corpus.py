"""Source review probes shared with the verification corpus; expectations unchanged."""

import json
from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURES = Path(__file__).parent / "fixtures" / "regression_corpus"
CASES = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))["cases"]


@pytest.fixture(scope="module")
def corpus_graph(tmp_path_factory):
    root = tmp_path_factory.mktemp("regression-corpus")
    package = root / "pkg"
    package.mkdir()
    for source in FIXTURES.glob("*.txt"):
        (package / (source.stem + ".py")).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8", newline=""
        )
    ArcGraphIndexer(root, root / "out", [SourceRoot(".")]).build()
    store = GraphStoreReader.from_current(root / "out")
    return store.read_edges(), {node.id: node.start_line for node in store.read_nodes()}


@pytest.mark.parametrize("owner", sorted(CASES))
def test_regression_corpus(corpus_graph, owner):
    edges, lines = corpus_graph
    spec = CASES[owner]
    targets = set()
    for edge in edges:
        if edge.source != owner or edge.resolution.status != "resolved":
            continue
        sites = edge.properties.get("callsites") or [
            edge.properties.get("callsite", {})
        ]
        if "call" not in spec or any(
            site.get("call_expression") == spec["call"] for site in sites
        ):
            targets.add(edge.target)
    assert set(spec.get("require", [])) <= targets
    assert not set(spec.get("forbid", [])) & targets
    for node, expected_line in spec.get("node_line", {}).items():
        assert lines.get(node) == expected_line
