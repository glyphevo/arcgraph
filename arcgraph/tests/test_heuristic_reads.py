"""Heuristic answers stay visible by default and can be excluded from traversal."""

import pytest

from arcgraph.core.confidence_profiles import CONFIDENCE_PROFILES
from arcgraph.core.schemas import Edge, Evidence, FactResolution, Node
from arcgraph.core.semantic_metrics import collect_semantic_metrics
from arcgraph.core.ids import callsite_id
from arcgraph.interfaces.mcp_tools import ArcGraphMCPConfig, ArcGraphMCPToolGroup
from arcgraph.tests.test_query_engine import _build_guardrail_graph
from arcgraph.tests.test_generator_and_dynamic_exports import _targets
from arcgraph.core.graph_store import GraphStoreReader


def _graph(tmp_path):
    entry = Node(id="route:GET:/start", kind="route", name="start")
    bridge = Node(id="fn:pkg.bridge", kind="function", name="bridge")
    leaf = Node(id="fn:pkg.leaf", kind="function", name="leaf")
    engine = _build_guardrail_graph(
        tmp_path / "out",
        tmp_path,
        nodes=[entry, bridge, leaf],
        edges=[
            Edge(
                source=entry.id, target=bridge.id, kind="calls", confidence="heuristic"
            ),
            Edge(source=bridge.id, target=leaf.id, kind="calls", confidence="inferred"),
        ],
    )
    return engine, entry, bridge, leaf


@pytest.mark.parametrize("direction", ["callers", "callees", "impact", "flow"])
def test_heuristic_queries_label_and_filter_without_traversing_a_guess(
    tmp_path, direction
):
    engine, entry, bridge, leaf = _graph(tmp_path)

    def query(profile):
        if direction == "callers":
            return engine.callers(bridge.id, profile=profile)["edges"]
        if direction == "callees":
            return engine.callees(entry.id, profile=profile)["edges"]
        if direction == "impact":
            return engine.impact(leaf.id, profile=profile)["call_impact"]["edges"]
        # Assert the missing capability before calling the new keyword on the parent.
        assert "evidence_only" in CONFIDENCE_PROFILES
        return engine.entrypoint_flow(entry.id, profile=profile)["edges"]

    edges = query("review_default")
    guessed = next(e for e in edges if e["confidence"] == "heuristic")
    assert guessed["properties"].get("connection_basis") == "heuristic"
    assert "evidence_only" in CONFIDENCE_PROFILES
    filtered = query("evidence_only")
    assert all(e["confidence"] != "heuristic" for e in filtered)
    if direction == "flow":
        assert filtered == []  # The inferred leaf behind the guess is unreachable.
    if direction == "impact":
        assert {e["source"] for e in filtered} == {bridge.id}


def test_mcp_keeps_basis_in_compact_context_and_flow(tmp_path):
    engine, entry, bridge, leaf = _graph(tmp_path)
    tools = ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(
            tmp_path,
            output_dir=engine.output_dir,
            allowed_roots=[tmp_path],
        )
    )
    payload = tools.arcgraph_get_context(targets=[leaf.id])
    guessed = next(e for e in payload["edges"] if e["confidence"] == "heuristic")
    assert guessed.get("connection_basis") == "heuristic"
    assert "evidence_only" in CONFIDENCE_PROFILES
    filtered = tools.arcgraph_get_context(targets=[leaf.id], profile="evidence_only")
    assert all(e["confidence"] != "heuristic" for e in filtered["edges"])
    flow = tools.arcgraph_entrypoint_flow(entrypoint=entry.id)
    assert (
        next(e for e in flow["edges"] if e["confidence"] == "heuristic")[
            "connection_basis"
        ]
        == "heuristic"
    )
    assert (
        tools.arcgraph_entrypoint_flow(entrypoint=entry.id, profile="evidence_only")[
            "edges"
        ]
        == []
    )


def test_verified_metric_excludes_only_heuristic_resolution_and_keeps_gate_count():
    node = Node(
        id="fn:pkg.use",
        kind="function",
        name="use",
        path="m.py",
        properties={
            "callsites": [
                {"name": "value.get", "line": 1, "column": 0},
                {"name": "value.put", "line": 2, "column": 0},
            ],
        },
    )
    edges = [
        Edge(
            source=node.id,
            target="extsym:pkg.Target.get",
            kind="uses",
            confidence="heuristic",
            evidence=[
                Evidence(
                    kind="ast_attribute_call",
                    path="m.py",
                    start_line=1,
                    column=0,
                    detail="value.get",
                )
            ],
        ),
        Edge(
            source=node.id,
            target="extsym:pkg.Target.put",
            kind="uses",
            confidence="inferred",
            evidence=[
                Evidence(
                    kind="ast_attribute_call",
                    path="m.py",
                    start_line=2,
                    column=0,
                    detail="value.put",
                )
            ],
        ),
    ]
    stats = collect_semantic_metrics([node], edges)
    assert stats["resolution_rate"] == 1.0
    assert stats.get("verified_resolution_rate") == 0.5
    assert stats["verified_resolved_callsite_total"] == 1
    # A second evidential producer for the same callsite counts it as verified.
    edges.append(edges[0].model_copy(update={"confidence": "confirmed"}))
    assert collect_semantic_metrics([node], edges)["verified_resolution_rate"] == 1.0
    edges[-1].resolution = FactResolution(status="unresolved")
    assert collect_semantic_metrics([node], edges)["verified_resolution_rate"] == 0.5


def test_mixed_edge_filters_callsite_provenance_without_changing_the_store(tmp_path):
    source = Node(
        id="fn:pkg.use",
        kind="function",
        name="use",
        path="m.py",
        properties={
            "callsites": [
                {"name": "a.find", "line": 1, "column": 0},
                {"name": "b.find", "line": 2, "column": 0},
            ],
        },
    )
    target = Node(id="method:pkg.Matcher.find", kind="method", name="find")
    sites = [
        {
            "callsite_id": callsite_id(source.id, "m.py", i, 0, f"{n}.find"),
            "raw_expression": f"{n}.find",
            "confidence": confidence,
            "resolution_strategy": strategy,
        }
        for i, n, confidence, strategy in [
            (1, "a", "heuristic", "unique_method_fallback"),
            (2, "b", "inferred", "receiver_type"),
        ]
    ]
    edge = Edge(
        source=source.id,
        target=target.id,
        kind="calls",
        confidence="inferred",
        resolution=FactResolution(
            callsite_id=sites[1]["callsite_id"], strategy="receiver_type"
        ),
        evidence=[
            Evidence(
                kind="ast_attribute_call",
                path="m.py",
                start_line=i,
                column=0,
                detail=f"{n}.find",
            )
            for i, n in [(1, "a"), (2, "b")]
        ],
        properties={"callsites": sites},
    )
    engine = _build_guardrail_graph(
        tmp_path / "out", tmp_path, nodes=[source, target], edges=[edge]
    )
    assert engine.semantic_stats()["metrics"].get("verified_resolution_rate") == 0.5
    assert (
        engine.callees(source.id)["edges"][0]["properties"]["connection_basis"]
        == "mixed_evidence"
    )
    for output in (
        engine.callees(source.id, profile="evidence_only")["edges"],
        engine.impact(target.id, profile="evidence_only")["call_impact"]["edges"],
    ):
        assert len(output) == 1
        assert output[0]["properties"]["callsites"] == [sites[1]]
        assert [e["start_line"] for e in output[0]["evidence"]] == [2]
    assert len(engine.store.read_edges()[0].properties["callsites"]) == 2


def test_analyzer_preserves_each_callsite_confidence_before_merging(tmp_path):
    _targets(
        tmp_path,
        {
            "user": "class Matcher:\n    def run(self): pass\ndef use(m: Matcher, matcher):\n    m.run()\n    matcher.run()\n"
        },
        v2=True,
    )
    edge = next(
        e
        for e in GraphStoreReader.from_current(tmp_path / "out").read_edges()
        if e.source == "fn:pkg.user.use" and e.target == "method:pkg.user.Matcher.run"
    )
    sites = {
        fact["raw_expression"]: fact.get("confidence")
        for fact in edge.properties.get("callsites", [edge.properties.get("callsite")])
    }
    assert sites == {"m.run": "inferred", "matcher.run": "heuristic"}
