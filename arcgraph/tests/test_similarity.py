from __future__ import annotations

import ast

from arcgraph.analyzers.similarity import PROFILE_ALGORITHM, SimilarityAnalyzer
from arcgraph.core.schemas import Edge, Node


def test_same_structure_with_disjoint_features_remains_similar() -> None:
    tree = ast.parse("""
def left(value):
    return first(value)

def right(value):
    return second(value)
""")
    tree._arcgraph_module = "sample"  # type: ignore[attr-defined]
    nodes = [
        Node(
            id=f"fn:sample.{name}",
            kind="function",
            name=name,
            qualname=f"sample.{name}",
            path="src/sample.py",
            properties={"params": ["value"]},
        )
        for name in ("left", "right")
    ]
    edges = [
        Edge(source="fn:sample.left", target="fn:deps.first", kind="calls"),
        Edge(source="fn:sample.right", target="fn:deps.second", kind="calls"),
        Edge(source="fn:sample.left", target="table:first", kind="reads"),
        Edge(source="fn:sample.right", target="table:second", kind="reads"),
    ]

    analysis = SimilarityAnalyzer().analyze(
        {"src/sample.py": tree},
        nodes,
        edges,
    )

    assert len(analysis.edges) == 1
    edge = analysis.edges[0]
    assert edge.properties["score"] == 1.0
    assert edge.properties["algorithm"] == PROFILE_ALGORITHM
    assert edge.properties["reasons"] == [
        "token_jaccard=1.00",
        "same_structure",
        "call_overlap=0.00",
        "resource_overlap=0.00",
    ]
    assert all(
        node.properties["similarity"]["algorithm"] == PROFILE_ALGORITHM
        for node in nodes
    )


def test_python_similarity_does_not_restore_an_incompatible_profile() -> None:
    node = Node(
        id="fn:sample.old",
        kind="function",
        name="old",
        qualname="sample.old",
        path="src/sample.py",
        properties={
            "similarity": {
                "algorithm": "python_ast_token_ngrams_v0",
                "bucket": "src|function|0|sync",
                "structure_hash": "old",
                "ngrams": ["one", "two", "three"],
                "call_targets": [],
                "resource_targets": [],
            }
        },
    )

    profile = SimilarityAnalyzer()._profile_for_node(node, None, set(), set(), set())

    assert profile is None
    # The incompatible profile must not be restored, and must also not be
    # stripped: the reindexer passes the live unchanged nodes it republishes,
    # so popping here would delete the owning frontend's persisted profile.
    assert node.properties["similarity"]["algorithm"] == "python_ast_token_ngrams_v0"
