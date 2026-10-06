"""One edge per caller and target keeps a fact for every call it stands for.

Edges merge by caller, target and kind, and their callsite facts merged by a
path, line and column that the facts do not carry, so two calls of one name
kept one fact: the index recorded the first call and lost the rest. A fact's
callsite_id is derived from that position, and facts now merge by it. The
callsites query matched an edge to its first call only, so a later call of
the same target was reported unresolved; it now matches every fact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer

SOURCE = """def helper(value):
    return value


def use(conn, items):
    helper(1)
    conn.commit()
    helper(2)
    conn.commit()
    items.append(1); items.append(2)
"""


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("merged")
    package = root / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "store.py").write_text(SOURCE, encoding="utf-8")
    output = root / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
    ).build()
    return output


def _edge(index: Path, target: str):
    return next(
        edge
        for edge in GraphStoreReader.from_current(index).read_edges()
        if edge.source == "fn:pkg.store.use" and edge.target == target
    )


@pytest.mark.parametrize(
    ("target", "expressions"),
    [
        ("fn:pkg.store.helper", ["helper(1)", "helper(2)"]),
        (
            "protocol:pep249.Connection.commit",
            ["conn.commit()", "conn.commit()"],
        ),
        (
            "extsym:collections.abc.MutableSequence.append",
            ["items.append(1)", "items.append(2)"],
        ),
    ],
)
def test_every_call_keeps_its_fact(index, target, expressions):
    edge = _edge(index, target)
    facts = edge.properties["callsites"]
    assert [fact["call_expression"] for fact in facts] == expressions
    assert len({fact["callsite_id"] for fact in facts}) == len(expressions)
    # The edge's own callsite is still the first call.
    assert edge.properties["callsite"] == facts[0]


def test_the_callsites_query_resolves_every_call(index):
    result = QueryEngine(index).callsites("pkg.store.use", limit=50)
    calls = [
        (item["line"], item["raw_expression"], item["resolution_status"])
        for item in result["callsites"]
    ]
    assert calls == [
        (6, "helper", "resolved"),
        (7, "conn.commit", "resolved"),
        (8, "helper", "resolved"),
        (9, "conn.commit", "resolved"),
        (10, "items.append", "resolved"),
        (10, "items.append", "resolved"),
    ]
    # No record stands for a fact without a position of its own.
    assert all(item["line"] is not None for item in result["callsites"])


ORDERED = """def use(db, conn):
    db.commit()
    conn.commit()
    conn.commit()
"""


def _build(root: Path, text: str) -> Path:
    package = root / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "store.py").write_text(text, encoding="utf-8")
    output = root / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
    ).build()
    return output


def test_facts_follow_the_source_order(tmp_path):
    edge = next(
        edge
        for edge in GraphStoreReader.from_current(
            _build(tmp_path, ORDERED)
        ).read_edges()
        if edge.target == "protocol:pep249.Connection.commit"
    )
    facts = edge.properties["callsites"]
    assert [(fact["call_expression"], fact["line"]) for fact in facts] == [
        ("db.commit()", 2),
        ("conn.commit()", 3),
        ("conn.commit()", 4),
    ]
    # The edge's own callsite is the first call in the source, not the first
    # by name.
    assert edge.properties["callsite"]["call_expression"] == "db.commit()"


def test_a_line_shift_changes_no_merged_edge(tmp_path):
    from arcgraph.change.graph_delta import _edge_semantic_projection

    def projections(root: Path, text: str) -> dict[tuple[str, str, str], dict]:
        return {
            (edge.source, edge.target, edge.kind): _edge_semantic_projection(edge)
            for edge in GraphStoreReader.from_current(_build(root, text)).read_edges()
            if edge.source == "fn:pkg.store.use"
        }

    before = projections(tmp_path / "before", SOURCE)
    after = projections(tmp_path / "after", "\n\n\n" + SOURCE)
    # Positions move, but no call changes meaning: change safety must report
    # no semantic change for an edge that stands for several calls.
    assert before.keys() == after.keys()
    assert [key for key in before if before[key] != after[key]] == []
