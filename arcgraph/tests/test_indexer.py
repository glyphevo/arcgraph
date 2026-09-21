from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.core.schemas import Edge, Evidence


def test_dedupe_edges_merges_confidence_variants() -> None:
    edges = [
        Edge(
            source="fn:pkg.a",
            target="fn:pkg.b",
            kind="calls",
            confidence="inferred",
            evidence=[Evidence(kind="ast_call", path="pkg/a.py", start_line=1)],
        ),
        Edge(
            source="fn:pkg.a",
            target="fn:pkg.b",
            kind="calls",
            confidence="confirmed",
            evidence=[Evidence(kind="scip_reference", path="pkg/a.py", start_line=1)],
        ),
    ]

    deduped = ArcGraphIndexer.dedupe_edges(edges)

    assert len(deduped) == 1
    assert deduped[0].confidence == "confirmed"
    assert [evidence.kind for evidence in deduped[0].evidence] == [
        "ast_call",
        "scip_reference",
    ]


def test_dedupe_edges_keeps_distinct_semantic_roles() -> None:
    edges = [
        Edge(
            source="fn:pkg.a",
            target="config:FEATURE",
            kind="configures",
            semantic_role="read",
            confidence="heuristic",
        ),
        Edge(
            source="fn:pkg.a",
            target="config:FEATURE",
            kind="configures",
            semantic_role="declare",
            confidence="confirmed",
        ),
    ]

    deduped = ArcGraphIndexer.dedupe_edges(edges)

    assert len(deduped) == 2
    assert {edge.semantic_role for edge in deduped} == {"read", "declare"}
