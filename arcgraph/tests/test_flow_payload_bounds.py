"""Agent flows must remain useful and honest under large analysis records."""

import json
from pathlib import Path

import pytest

from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.metrics import _payload_was_truncated
from arcgraph.interfaces.mcp_tools import ArcGraphMCPConfig, ArcGraphMCPToolGroup
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.providers.flow_payload import (
    FLOW_PAYLOAD_BYTES,
    compact_flow,
    payload_size,
)


@pytest.fixture
def tools(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n"
        "def leaf(): pass\ndef middle(): leaf()\n"
        "@app.get('/chain')\ndef route(): middle()\n"
        "def target(): pass\n"
        + "\n".join(f"def caller{i}(): target()" for i in range(40)),
        encoding="utf-8",
    )
    index = tmp_path / "index"
    ArcGraphIndexer(repo, index, [SourceRoot(".")]).build()
    return ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(repo, output_dir=index)
    ), QueryEngine(index)


def test_flow_reports_depth_and_count_boundaries_without_dangling_edges(tools):
    facade, engine = tools
    complete = facade.arcgraph_entrypoint_flow(method="GET", path="/chain", max_depth=6)
    assert not complete["analysis_limits"]["traversal_truncated"]
    assert any(n["id"] == "fn:app.leaf" for n in complete["nodes"])
    count = len(complete["edges"])
    exact = engine.entrypoint_flow("GET /chain", max_depth=6, max_results=count)
    assert not exact["analysis_limits"]["traversal_truncated"]
    for args, reason in (
        ({"max_depth": 0}, "max_depth"),
        ({"max_results": 1, "max_depth": 6}, "max_results"),
    ):
        result = facade.arcgraph_entrypoint_flow(method="GET", path="/chain", **args)
        assert reason in result["analysis_limits"]["reasons"]
        assert result["analysis_limits"]["remaining_edge_count"] is None
        assert _payload_was_truncated(result)
        ids = {n["id"] for n in result["nodes"]}
        assert all(e["source"] in ids and e["target"] in ids for e in result["edges"])
        assert payload_size(result) <= FLOW_PAYLOAD_BYTES
    context = facade.arcgraph_get_context(targets=["GET /chain"], max_results=1)
    assert context["status"] == "partial"
    assert context["truncation"]["entrypoint_flow_truncated"]
    assert context["assurance"]["limits"]["traversal_truncated"]
    missing = facade.arcgraph_entrypoint_flow(method="GET", path="/missing")
    assert missing["status"] == "unavailable"
    assert not missing["truncation"]["truncated"]


@pytest.mark.parametrize("depth", [0, 1])
def test_route_depth_counts_forward_graph_hops(tools, depth):
    facade, engine = tools
    responses = [
        engine.entrypoint_flow("GET /chain", max_depth=depth),
        facade.arcgraph_entrypoint_flow(method="GET", path="/chain", max_depth=depth),
    ]
    for result in responses:
        assert {n["id"] for n in result["entrypoints"]} == {"route:GET:/chain"}
        assert {n["id"] for n in result["nodes"]} == (
            {"route:GET:/chain", "fn:app.route"} if depth else {"route:GET:/chain"}
        )
        assert {(e["source"], e["kind"], e["target"]) for e in result["edges"]} == (
            {("route:GET:/chain", "invokes", "fn:app.route")} if depth else set()
        )
        assert result["analysis_limits"]["reasons"] == ["max_depth"]
        assert result["analysis_limits"]["remaining_edge_count"] is None
        assert result["truncation"]["truncated"]


def test_analysis_properties_do_not_cross_flow_boundary():
    node = {
        "id": "fn:a",
        "kind": "function",
        "path": "app.py",
        "properties": {
            "bindings": ["x" * 1000] * 1000,
            "type_refs": ["T" * 1000] * 1000,
        },
    }
    raw = {"nodes": [node], "entrypoints": [node], "edges": [], "status": "available"}
    before = json.dumps(raw)
    compact = compact_flow(raw)
    assert json.dumps(raw) == before
    assert compact["nodes"][0]["id"] == "fn:a"
    assert "properties" not in compact["nodes"][0]
    assert payload_size(compact) < 3000


def test_byte_budget_preserves_some_edges_and_their_endpoints():
    nodes = [
        {"id": f"fn:{i}", "path": "x" * 2000, "kind": "function"} for i in range(101)
    ]
    raw = {
        "nodes": nodes,
        "entrypoints": [nodes[0]],
        "edges": [
            {
                "source": "fn:0",
                "target": n["id"],
                "kind": "calls",
                "confidence": "inferred",
                "resolution": {"status": "resolved", "strategy": "receiver_type"},
                "evidence": [{"kind": "ast_call", "path": "app.py", "start_line": 7}]
                * 4,
            }
            for n in nodes[1:]
        ],
        "status": "available",
    }
    result = compact_flow(raw)
    assert payload_size(result) <= FLOW_PAYLOAD_BYTES
    assert 0 < len(result["edges"]) < 100
    assert len(result["edges"]) + result["truncation"]["omitted_edges"] == 100
    assert len(result["nodes"]) + result["truncation"]["omitted_nodes"] == 101
    assert result["truncation"]["omitted_evidence_items"] == 200
    assert result["truncation"]["response_truncated"]
    ids = {n["id"] for n in result["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in result["edges"])
    assert result["edges"][0]["resolution"]["strategy"] == "receiver_type"
    assert result["edges"][0]["evidence"][0]["start_line"] == 7


def test_oversized_envelope_is_explicitly_omitted():
    result = compact_flow(
        {"status": "available", "query": "長" * 100000, "warnings": ["x" * 100000]}
    )
    assert payload_size(result) <= FLOW_PAYLOAD_BYTES
    assert result["status"] == "partial"
    assert set(result["truncation"]["omitted_fields"]) == {"query", "warnings"}


def test_detailed_can_recover_callers_beyond_thirty(tools):
    facade, _ = tools
    summary = facade.arcgraph_explain(targets=["app.target"], max_results=60)
    detailed = facade.arcgraph_explain(
        targets=["app.target"], detail_level="detailed", max_results=60
    )
    assert len(summary["explanations"][0]["callers"]) == 6
    assert len(detailed["explanations"][0]["callers"]) == 40
    assert not detailed["truncation"]["truncated"]


@pytest.mark.parametrize(
    "name", ["arcgraph_get_context", "arcgraph_explain", "arcgraph_find_similar"]
)
def test_invalid_detail_level_is_not_silently_downgraded(tools, name):
    facade, _ = tools
    args = (
        {"target": "app.target"}
        if name.endswith("find_similar")
        else {"targets": ["app.target"]}
    )
    with pytest.raises(ValueError, match="summary, standard, or detailed"):
        getattr(facade, name)(detail_level="full", **args)
