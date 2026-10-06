"""A guessed method of a protocol with no module is a protocol_symbol target.

PEP 249 defines a database connection and cursor but no module to import, so
a call guessed to be one of its methods links protocol:pep249.<Class>.<method>,
named by the class that defines the method, rather than an external symbol
that looks importable and is not. Like an external symbol it is outside the
project: shown only with include_external, kept by similarity, dropped by the
reindexer when nothing refers to it.
"""

from __future__ import annotations

import pytest

from arcgraph.analyzers.calls import CallAnalyzer
from arcgraph.analyzers.calls.constants import DB_METHOD_OWNERS, PEP249_METHODS_BY_CLASS
from arcgraph.analyzers.similarity import SimilarityAnalyzer
from arcgraph.change.identities import StableIdentityCollision, stable_node_identity
from arcgraph.core import force_graph_export
from arcgraph.core.schemas import Node
from arcgraph.pipeline.reindexer import _is_pathless_synthetic_node
from arcgraph.tests.test_calls import _analyzed_nodes, function_id

PROTOCOL_NODE = Node(
    id="protocol:pep249.Cursor.execute",
    kind="protocol_symbol",
    name="execute",
    qualname="pep249.Cursor.execute",
)


def test_each_guessed_database_method_is_its_protocol_class_s():
    for method, owner in DB_METHOD_OWNERS.items():
        assert method in PEP249_METHODS_BY_CLASS[owner], (method, owner)
    # PEP 249 gives a connection no execute or fetch method.
    assert not {"execute", "executemany", "fetchone", "fetchall"} & (
        PEP249_METHODS_BY_CLASS["pep249.Connection"]
    )


@pytest.mark.parametrize(
    ("call", "target"),
    [
        ("conn.execute('select 1')", "protocol:pep249.Cursor.execute"),
        ("db_cursor.fetchone()", "protocol:pep249.Cursor.fetchone"),
        ("conn.commit()", "protocol:pep249.Connection.commit"),
        ("conn.close()", "protocol:pep249.Connection.close"),
        ("db_cursor.close()", "protocol:pep249.Cursor.close"),
    ],
)
def test_a_database_guess_links_the_protocol_class_method(call, target):
    result = CallAnalyzer(enable_v2=True).analyze(
        _analyzed_nodes(f"def use(conn, db_cursor) -> None:\n    {call}\n")
    )
    edges = [e for e in result.edges if e.source == function_id("pkg.calls.use")]
    assert [(e.target, e.kind) for e in edges] == [(target, "uses")]
    node = next(n for n in result.nodes if n.id == target)
    assert node.kind == "protocol_symbol"
    assert node.properties["protocol"] == "PEP 249"


def test_the_identity_profile_knows_the_protocol_prefix():
    identity = stable_node_identity("repo", PROTOCOL_NODE)
    assert identity["stable_node_id"] == PROTOCOL_NODE.id
    # The prefix belongs to protocol_symbol alone.
    with pytest.raises(StableIdentityCollision):
        stable_node_identity(
            "repo",
            Node(id=PROTOCOL_NODE.id, kind="external_symbol", name="execute"),
        )


def test_the_force_graph_shows_protocol_targets_only_with_externals():
    nodes = {
        "fn:pkg.a": {
            "id": "fn:pkg.a",
            "kind": "function",
            "name": "a",
            "qualname": "pkg.a",
            "start_line": 1,
            "end_line": 2,
        },
        PROTOCOL_NODE.id: {
            "id": PROTOCOL_NODE.id,
            "kind": "protocol_symbol",
            "name": "execute",
            "qualname": "pep249.Cursor.execute",
            "start_line": None,
            "end_line": None,
        },
    }
    edges = [{"source": "fn:pkg.a", "target": PROTOCOL_NODE.id, "kind": "uses"}]
    for include in (False, True):
        options = force_graph_export.ForceGraphExportOptions(
            include_external=include, min_symbol_degree=0
        )
        reason = force_graph_export._node_exclusion_reason(
            nodes[PROTOCOL_NODE.id], options
        )
        assert reason == (None if include else "external")
        focus = force_graph_export._focus_symbol_ids(nodes, options)
        assert (PROTOCOL_NODE.id in focus) is include
        graph = force_graph_export._symbol_graph(nodes, edges, options=options)
        shown = {node["id"] for node in graph["nodes"]}
        assert (PROTOCOL_NODE.id in shown) is include


def test_protocol_targets_are_synthetic_for_the_reindexer_and_similarity():
    assert _is_pathless_synthetic_node(PROTOCOL_NODE)
    kept = SimilarityAnalyzer._surviving_targets(
        [PROTOCOL_NODE.id, "fn:pkg.removed"], set()
    )
    assert kept == {PROTOCOL_NODE.id}


def test_a_method_the_protocol_class_lacks_is_not_linked():
    assert CallAnalyzer._protocol_symbol("pep249.Connection", "execute") is None
    assert CallAnalyzer._protocol_symbol("pep249.Missing", "close") is None
    assert CallAnalyzer._protocol_symbol("pep249.Cursor", "execute") is not None


def test_a_protocol_execute_result_is_not_another_library_s():
    # PEP 249 does not say what execute returns, so the next call is guessed
    # as no library's result; a session's execute is still SQLAlchemy's.
    result = CallAnalyzer(enable_v2=True).analyze(
        _analyzed_nodes(
            "def use(conn, session) -> None:\n"
            "    conn.execute('select 1').fetchall()\n"
            "    session.execute('select 1').fetchall()\n"
        )
    )
    targets = {
        e.target for e in result.edges if e.source == function_id("pkg.calls.use")
    }
    assert "protocol:pep249.Cursor.execute" in targets
    assert "extsym:sqlalchemy.engine.Result.fetchall" in targets
    fetchall = [
        e
        for e in result.edges
        if e.target == "extsym:sqlalchemy.engine.Result.fetchall"
    ]
    assert [e.properties["callsite"]["receiver_expression"] for e in fetchall] == [
        "session.execute('select 1')"
    ]


def test_symbol_queries_do_not_take_a_protocol_target_for_a_project_symbol(
    tmp_path,
):
    from arcgraph.core.graph_store import GraphStoreReader
    from arcgraph.core.scanner import SourceRoot
    from arcgraph.core.target_resolver import TargetResolver
    from arcgraph.pipeline.indexer import ArcGraphIndexer

    package = tmp_path / "repo" / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "store.py").write_text(
        "def use(conn):\n    conn.execute('select 1')\n    conn.commit()\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"
    ArcGraphIndexer(
        repo_root=package.parents[1],
        output_dir=output,
        source_roots=[SourceRoot("src")],
    ).build()
    store = GraphStoreReader.from_current(output)
    assert any(n.kind == "protocol_symbol" for n in store.read_nodes())
    resolver = TargetResolver(str(package.parents[1]))
    with store.connect() as conn:
        # By name, qualname, suffix or search, a protocol method is not a
        # project symbol, as an external one is not.
        for query in ("execute", "commit", "pep249.Cursor.execute", "Cursor.execute"):
            assert resolver.resolve(conn, query).status != "resolved", query
        assert not [
            row
            for row in resolver._suggestions(conn, "execute")
            if str(row["id"]).startswith("protocol:")
        ]
        # Its own id still names it.
        by_id = resolver.resolve(conn, "protocol:pep249.Cursor.execute")
        assert by_id.status == "resolved"


def test_a_connection_named_receiver_has_no_fetch_method():
    result = CallAnalyzer(enable_v2=True).analyze(
        _analyzed_nodes("def use(conn) -> None:\n    conn.fetchone()\n")
    )
    assert not [
        e
        for e in result.edges
        if e.source == function_id("pkg.calls.use")
        and e.resolution.status == "resolved"
    ]
