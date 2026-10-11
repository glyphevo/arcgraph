"""Type evidence is inferred; method-name guesses remain heuristic."""

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.tests.test_generator_and_dynamic_exports import _targets


@pytest.mark.parametrize(
    "source,target",
    [
        (
            "import sqlite3\ndef use():\n    connection=sqlite3.connect(':memory:')\n    connection.execute('select 1')\n",
            "extsym:sqlite3.Connection.execute",
        ),
        (
            "from pathlib import Path\ndef use(p: Path):p.exists()\n",
            "extsym:pathlib.Path.exists",
        ),
        (
            "import sqlite3\ndef use():sqlite3.connect(':memory:').execute('select 1')\n",
            "extsym:sqlite3.Connection.execute",
        ),
    ],
)
def test_typed_external_receiver_is_inferred(tmp_path, source, target):
    _targets(tmp_path, {"user": source}, v2=True)
    edges = GraphStoreReader.from_current(tmp_path / "out").read_edges()
    matching = [
        e for e in edges if e.source == "fn:pkg.user.use" and e.target == target
    ]
    assert matching
    assert all(e.confidence == "inferred" for e in matching)


def test_name_only_external_receiver_remains_heuristic(tmp_path):
    _targets(tmp_path, {"user": "def use(value):value.exists()\n"}, v2=True)
    edges = GraphStoreReader.from_current(tmp_path / "out").read_edges()
    matching = [e for e in edges if e.target == "extsym:pathlib.Path.exists"]
    assert matching
    assert all(e.confidence == "heuristic" for e in matching)


def test_nested_expression_resolver_reports_the_same_type_confidence(tmp_path):
    import ast
    from arcgraph.analyzers.calls.analyzer import CallAnalyzer
    from arcgraph.analyzers.calls.context import _CallResolutionContext

    _targets(
        tmp_path,
        {
            "user": "import sqlite3\ndef use():sqlite3.connect(':memory:').execute('select 1')\n"
        },
        v2=True,
    )
    nodes = GraphStoreReader.from_current(tmp_path / "out").read_nodes()
    source = next(n for n in nodes if n.id == "fn:pkg.user.use")
    context = _CallResolutionContext(nodes)
    context.callsite_position = (source.id, 2, 10)
    result = CallAnalyzer(enable_v2=True)._resolve_call_node(
        source,
        ast.parse("sqlite3.connect(':memory:').execute('select 1')", mode="eval").body,
        context,
    )
    assert result is not None
    assert result.target.id == "extsym:sqlite3.Connection.execute"
    assert result.confidence == "inferred"


def test_a_documented_return_after_a_name_guess_remains_heuristic(tmp_path):
    _targets(
        tmp_path,
        {
            "user": "def use(session, statement):\n    session.execute(statement).scalars().all()\n    statement.where(True).order_by('id')\n"
        },
        v2=True,
    )
    edges = GraphStoreReader.from_current(tmp_path / "out").read_edges()
    matching = [
        e
        for e in edges
        if e.source == "fn:pkg.user.use"
        and e.resolution.strategy == "external_receiver_type"
    ]
    assert matching
    assert all(e.confidence == "heuristic" for e in matching)
