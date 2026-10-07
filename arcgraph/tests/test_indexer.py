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


def test_a_parse_error_names_the_python_that_parsed_the_file(tmp_path):
    import sys
    from pathlib import Path

    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.core.scanner import SourceRoot

    package = Path(tmp_path) / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    # Not Python on any version; a file of newer syntax fails the same way on
    # an older parser, which is what the message is for.
    (package / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    output = Path(tmp_path) / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
    ).build()
    warnings = [
        warning
        for warning in GraphStoreReader.from_current(output).read_warnings()
        if warning.kind == "parse_error"
    ]
    assert [warning.path for warning in warnings] == ["src/pkg/broken.py"]
    version = f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert f"parsed with Python {version};" in warnings[0].message
    assert "needs ArcGraph to run on that version" in warnings[0].message
