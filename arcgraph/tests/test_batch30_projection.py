"""C06: public call identity combines supports without losing occurrences."""

from copy import deepcopy

from arcgraph.semantic.graph_projection import project_graph
from arcgraph.tests.test_semantic_core import make_records, make_view


def test_plain_calls_merge_roles_and_supports_without_mutating_inputs(tmp_path):
    snapshot, root, view = make_view(tmp_path, source="def f(): pass\n")
    fallback = make_records(snapshot, root, view, "arcgraph").finish()
    first = {
        "source": "fn:demo.f",
        "target": "extsym:builtins.print",
        "kind": "calls",
        "confidence": "inferred",
        "semantic_role": None,
        "evidence": [{"kind": "ast", "detail": "syntax"}],
        "confidence_sources": {"inferred": ["syntax"]},
        "properties": {
            "callsite": {"callsite_id": "one", "line": 2, "column": 4},
            "callsites": [{"callsite_id": "one", "line": 2, "column": 4}],
        },
    }
    second = deepcopy(first)
    second.update(semantic_role="call", confidence="confirmed")
    second["evidence"] = [{"kind": "precision_scip", "detail": "precise"}]
    second["confidence_sources"] = {"confirmed": ["precision_scip"]}
    third = deepcopy(second)
    third["properties"] = {
        "callsite": {"callsite_id": "two", "line": 3, "column": 4},
        "callsites": [{"callsite_id": "two", "line": 3, "column": 4}],
    }
    raw = {"nodes": [], "edges": [first, second, third]}
    before = deepcopy(raw)
    graph, _ = project_graph(
        view,
        raw,
        fallback,
        {"projected_calls": [], "edges": []},
        primary_producer="none",
    )
    assert len(graph) == 1, "C06 requires one support set across None/call roles"
    edge = graph[0]
    assert edge["semantic_role"] is None
    assert edge["confidence"] == "confirmed"
    assert {row["kind"] for row in edge["evidence"]} == {"ast", "precision_scip"}
    assert edge["confidence_sources"] == {
        "inferred": ["syntax"],
        "confirmed": ["precision_scip"],
    }
    assert [row["callsite_id"] for row in edge["properties"]["callsites"]] == [
        "one",
        "two",
    ]
    assert "call" in edge["properties"]["provenance"]["semantic_roles"]
    assert raw == before


def test_non_call_roles_remain_distinct(tmp_path):
    snapshot, root, view = make_view(tmp_path, source="def f(): pass\n")
    fallback = make_records(snapshot, root, view, "arcgraph").finish()
    raw = {
        "nodes": [],
        "edges": [
            {
                "source": "module:demo",
                "target": "fn:demo.f",
                "kind": kind,
                "confidence": "confirmed",
                "semantic_role": role,
            }
            for kind, role in (("imports", "import"), ("initializes", "initialize"))
        ],
    }
    graph, _ = project_graph(
        view,
        raw,
        fallback,
        {"projected_calls": [], "edges": []},
        primary_producer="none",
    )
    assert {(edge["kind"], edge["semantic_role"]) for edge in graph} == {
        ("imports", "import"),
        ("initializes", "initialize"),
    }
