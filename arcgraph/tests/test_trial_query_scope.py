"""Regressions from the TraceMind agent trial: visible facts and honest scope."""

import json
from pathlib import Path

import pytest

from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.mcp_tools import ArcGraphMCPConfig, ArcGraphMCPToolGroup
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.providers.relationship_payload import (
    RELATIONSHIP_BYTES,
    bound_explain_relations,
    compact_relations,
)


@pytest.fixture
def query_trial(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("""from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.orm import DeclarativeBase
from arq import cron
app = FastAPI()
class Base(DeclarativeBase): pass
class Item(Base):
    __tablename__ = "items"
def unrelated(blob): blob.other()
def target(blob): blob.missing()
@app.get("/search")
def route(): target(None)
async def job(session):
    await session.execute(update(Item).values(value=1))
async def poll(ctx):
    await ctx["redis"].enqueue_job("job", _queue_name="arq:jobs")
class WorkerSettings:
    queue_name = "arq:jobs"
    functions = [job]
    cron_jobs = [cron(poll)]
""")
    (repo / "test_app.py").write_text(
        "from app import target\n"
        + "\n".join(f"def test_{i}(): target(None)" for i in range(8))
    )
    index = tmp_path / "index"
    ArcGraphIndexer(repo, index, [SourceRoot(".")]).build()
    return ArcGraphMCPToolGroup(
        ArcGraphMCPConfig.for_single_repo(repo, output_dir=index)
    ), QueryEngine(index)


def test_resource_edges_are_visible_without_becoming_callers(query_trial):
    facade, engine = query_trial
    response = facade.arcgraph_explain(targets=["table:items"], detail_level="detailed")
    explanation = response["explanations"][0]
    assert explanation["callers"] == []
    assert explanation["incoming_edges"] == []
    assert [(e["source"], e["kind"]) for e in explanation["relations"]["incoming"]] == [
        ("fn:app.job", "writes")
    ]
    assert not explanation["relations"]["truncation"]["truncated"]
    assert "writes" not in explanation["call_scope"]["edge_kinds"]
    assert "writes" in explanation["relations"]["scope"]["edge_kinds"]
    assert engine.callers("table:items")["edges"] == []
    poll = facade.arcgraph_explain(targets=["app.poll"], detail_level="detailed")[
        "explanations"
    ][0]
    assert any(
        e["kind"] == "enqueues" and e["target"] == "worker:arq:jobs:job"
        for e in poll["relations"]["outgoing"]
    )
    assert all(e["kind"] != "enqueues" for e in poll["outgoing_edges"])


def test_entrypoint_suggestions_are_actionable_not_silent_reinterpretations(
    query_trial,
):
    facade, _ = query_trial
    missing = facade.arcgraph_entrypoint_flow(entrypoint="app.poll")
    assert missing["status"] == "unavailable"
    assert "worker:arq:jobs:poll" in {
        n["id"] for n in missing["target_resolution"]["suggestions"]
    }
    good = facade.arcgraph_entrypoint_flow(entrypoint="worker:arq:jobs:poll")
    assert good["status"] == "available"
    assert any(
        "cron_jobs" in e["detail"]
        for edge in facade.arcgraph_explain(
            targets=["app.poll"], detail_level="detailed"
        )["explanations"][0]["incoming_edges"]
        for e in edge.get("evidence", [])
    )
    for query in ("POST /search", "route:POST:/search"):
        wrong_method = facade.arcgraph_entrypoint_flow(entrypoint=query)
        assert wrong_method["status"] == "unavailable"
        assert "route:GET:/search" in {
            n["id"] for n in wrong_method["target_resolution"]["suggestions"]
        }


def test_risk_discloses_file_expansion_and_prioritizes_target(query_trial):
    facade, engine = query_trial
    raw = engine.impact("app.target")
    items = raw["unresolved_risks"]["items"]
    assert items[0]["inclusion_scope"] == "target"
    assert any(
        i["inclusion_scope"] == "same_file"
        and i["properties"]["source_scope"] == "fn:app.unrelated"
        for i in items
    )
    response = facade.arcgraph_get_risk(targets=["app.target"], max_results=1)
    report = response["target_reports"][0]
    assert report["unresolved_risks"][0]["inclusion_scope"] == "target"
    assert report["unresolved_risks"][0]["source_scope"] == "fn:app.target"
    assert report["unresolved_scope"]["counts"]["same_file"] > 0
    assert (
        "not a release verdict" in report["unresolved_scope"]["release_blocking_scope"]
    )
    assert response["truncation"]["truncated"]
    assert response["blast_radius"]["entrypoints"][0]["id"] == "route:GET:/search"
    counts = response["entrypoint_summary"]["counts"]
    assert counts["non_test"] == {"total": 1, "returned": 1, "omitted": 0}
    assert counts["test"] == {"total": 8, "returned": 0, "omitted": 8}


@pytest.mark.parametrize("limit", [1, 3])
def test_relations_disclose_count_evidence_and_byte_bounds(limit):
    edge = {
        "source": "fn:a",
        "target": "table:b",
        "kind": "writes",
        "confidence": "confirmed",
        "evidence": [{"path": "a.py", "start_line": n} for n in range(3)],
    }
    raw = {"incoming": [edge] * 3, "outgoing": [], "scope": {"edge_kinds": ["writes"]}}
    bounded = compact_relations(raw, limit)
    assert bounded["truncation"]["omitted_edges"]["incoming"] == 3 - limit
    assert bounded["truncation"]["omitted_evidence_on_retained_edges"] == limit
    assert ("context_limit" in bounded["truncation"]["reasons"]) == (limit < 3)
    # Model sanitizer expansion: the re-bound section drops whole records.
    bounded["incoming"][0]["evidence"][0]["path"] = "x" * 40000
    result = bound_explain_relations({"explanations": [{"relations": bounded}]})
    assert result["truncation"]["relations_truncated"]
    assert "byte_budget" in bounded["truncation"]["reasons"]
    assert (
        len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode())
        <= RELATIONSHIP_BYTES
    )
    assert bounded["totals"]["incoming"] == 3


def test_empty_or_exact_relations_are_not_falsely_truncated():
    raw = {"incoming": [], "outgoing": [], "scope": {"edge_kinds": ["reads", "writes"]}}
    assert not compact_relations(raw, 1)["truncation"]["truncated"]
    raw["outgoing"] = [
        {"source": "fn:a", "target": "table:t", "kind": "reads", "evidence": []}
    ]
    assert not compact_relations(raw, 1)["truncation"]["truncated"]
