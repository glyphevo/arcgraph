"""Source review probes shared with the verification corpus; expectations unchanged."""

import json
from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURES = Path(__file__).parent / "fixtures" / "regression_corpus"
CASES = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))["cases"]
D1_CASES = json.loads((FIXTURES / "d1" / "expected.json").read_text(encoding="utf-8"))[
    "cases"
]
assert all(spec.get("stage") == "D1" for spec in D1_CASES.values())
ACTIVE_CASES = CASES
CASES = {**ACTIVE_CASES, **D1_CASES}


@pytest.fixture(scope="module")
def corpus_graph(tmp_path_factory):
    return {
        "active": _build_corpus(tmp_path_factory, FIXTURES),
        "D1": _build_corpus(tmp_path_factory, FIXTURES / "d1"),
    }


def _build_corpus(tmp_path_factory, fixtures):
    root = tmp_path_factory.mktemp("regression-corpus")
    package = root / "pkg"
    package.mkdir()
    for source in fixtures.glob("*.txt"):
        (package / (source.stem + ".py")).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8", newline=""
        )
    ArcGraphIndexer(root, root / "out", [SourceRoot(".")]).build()
    store = GraphStoreReader.from_current(root / "out")
    return store.read_edges(), {node.id: node.start_line for node in store.read_nodes()}


@pytest.mark.parametrize(
    "owner",
    [
        pytest.param(
            owner,
            marks=(
                pytest.mark.xfail(strict=False, reason="D1 backend acceptance")
                if spec.get("stage") == "D1"
                else ()
            ),
        )
        for owner, spec in sorted(CASES.items())
    ],
)
def test_regression_corpus(corpus_graph, owner):
    spec = CASES[owner]
    edges, lines = corpus_graph[spec.get("stage", "active")]
    targets = set()
    for edge in edges:
        if edge.source != owner or edge.resolution.status != "resolved":
            continue
        if spec.get("stage") == "D1" and edge.confidence == "heuristic":
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
