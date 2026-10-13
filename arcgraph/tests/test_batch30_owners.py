"""Public anonymous execution owners keep structure identities and callsites."""

from copy import deepcopy

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.tests.test_batch30_build import configure
from arcgraph.tests.test_semantic_isolation import prepared as prepared


def test_structural_comprehension_and_duplicate_lambda_owners_are_distinct(tmp_path):
    from arcgraph.tests.test_semantic_core import make_view
    from arcgraph.semantic.public_owners import materialize_owners, anonymous_id

    _, _, view = make_view(
        tmp_path,
        source="def target(): return 1\ndef seeds(): return [1]\ndef outer():\n"
        "    a, b = lambda: target(), lambda: target()\n"
        "    c = (target() for x in seeds())\n"
        "    d = [target() for x in seeds()]\n",
    )
    raw = {"nodes": [], "edges": []}
    original = deepcopy(raw)
    result = materialize_owners(view, raw)
    lambdas = [n for n in result["nodes"] if n["name"] == "<lambda>"]
    assert len(lambdas) == 2
    assert lambdas[0]["qualname"] == lambdas[1]["qualname"]
    assert lambdas[0]["id"] != lambdas[1]["id"]
    assert all(n["id"].startswith("fn:" + n["qualname"] + "#") for n in result["nodes"])
    assert all(n["kind"] == "function" for n in result["nodes"])
    assert {n["name"] for n in result["nodes"]} >= {"<lambda>", "<genexpr>"}
    for node in result["nodes"]:
        record = view.records[result["owner_symbols"][node["id"]]]
        assert node["id"] == anonymous_id(view, record)
        assert all(f["name"] == "target" for f in node["properties"]["callsites"])
    assert raw == original


def test_actual_build_projects_lambda_and_generator_callers(prepared, tmp_path):
    from arcgraph.core.ids import callsite_id
    from arcgraph.core.query_engine import QueryEngine
    from arcgraph.core.schemas import ExplainResponse
    from arcgraph.interfaces.mcp_tools import ArcGraphMCPConfig, ArcGraphMCPToolGroup

    project, _, _, _ = prepared
    configure(project)
    (project / "demo.py").write_text(
        "def target(): return 1\ndef seeds(): return [1]\ndef outer():\n"
        "    a, b = lambda: target(), lambda: target()\n"
        "    c = (target() for x in seeds())\n"
        "    d = [target() for x in seeds()]\n"
        "    return a, b, c, d\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"
    metadata, _ = ArcGraphIndexer(project, output).build()
    reader = GraphStoreReader.from_current(output)
    owners = [n for n in reader.read_nodes() if "#" in n.id and n.kind == "function"]
    assert len(owners) == 3, owners
    assert len({n.id for n in owners}) == 3
    assert metadata.toolchain_status["semantic_core"]["status"] == "active"
    edges = reader.read_edges()
    for node in owners:
        assert node.path == "demo.py" and node.start_line and node.end_line
        facts = node.properties["callsites"]
        assert len(facts) == 1 and facts[0]["name"] == "target"
        fact = facts[0]
        expected_id = callsite_id(
            node.id, node.path, fact["line"], fact["column"], fact["name"]
        )
        calls = [
            e for e in edges if e.source == node.id and e.target == "fn:demo.target"
        ]
        assert len(calls) == 1 and calls[0].kind == "calls"
        assert calls[0].resolution.callsite_id == expected_id
        assert all(
            f["callsite_id"] == expected_id for f in calls[0].properties["callsites"]
        )
        assert any(e.kind == "type_inference" for e in calls[0].evidence)
    outer = next(n for n in reader.read_nodes() if n.id == "fn:demo.outer")
    assert {(f["line"], f["name"]) for f in outer.properties["callsites"]} == {
        (5, "seeds"),
        (6, "seeds"),
        (6, "target"),
    }
    assert any(e.source == outer.id and e.target == "fn:demo.seeds" for e in edges)
    query = QueryEngine(output)
    for owner in owners:
        calls = query.callsites(owner.id, limit=1)
        assert calls["summary"]["total"] == 1
        assert calls["callsites"][0]["resolution_status"] == "resolved"
    config = ArcGraphMCPConfig.for_single_repo(project, output_dir=output)
    config.max_results_limit = 1
    tools = ArcGraphMCPToolGroup(config)
    response = tools.arcgraph_explain(targets=[owners[0].id], max_results=100)
    ExplainResponse.model_validate(response)
    assert response["max_results"] == 1
    assert response["status"] == "available"
    assert len(response["explanations"]) <= 1


def test_anonymous_supports_move_identity_and_recompute_their_resolution(tmp_path):
    from arcgraph.tests.test_semantic_core import make_view
    from arcgraph.semantic.public_owners import materialize_owners
    from arcgraph.core.ids import callsite_id

    _, _, view = make_view(
        tmp_path, source="def target(): return 1\na = lambda: target()\n"
    )
    syntax = {"name": "target", "line": 2, "column": 12, "call_expression": "target()"}
    fact = {
        "callsite_id": "old",
        "stable_callsite_occurrence": 7,
        "line": 2,
        "column": 12,
        "call_expression": "target()",
        "raw_expression": "target",
        "context": "module_body",
        "confidence": "confirmed",
        "resolution_status": "resolved",
        "resolution_strategy": "lexical",
        "candidate_count": 1,
        "edge_kind": "initializes",
    }
    raw = {
        "nodes": [
            {
                "id": "mod:demo",
                "kind": "module",
                "name": "demo",
                "path": "demo.py",
                "properties": {"callsites": [syntax]},
            }
        ],
        "edges": [
            {
                "source": "mod:demo",
                "target": "fn:demo.target",
                "kind": "initializes",
                "confidence": "confirmed",
                "resolution": {"callsite_id": "old"},
                "properties": {"callsite": fact},
            }
        ],
    }
    result = materialize_owners(view, raw)
    owner = next(n for n in result["nodes"] if n["kind"] == "function")
    edge = result["edges"][0]
    identity = callsite_id(owner["id"], "demo.py", 2, 12, "target")
    assert edge["source"] == owner["id"]
    assert edge["kind"] == "calls"
    assert edge["resolution"]["callsite_id"] == identity
    assert edge["properties"]["callsites"][0]["stable_callsite_occurrence"] == 1
    assert result["nodes"][0]["properties"]["callsites"] == []
