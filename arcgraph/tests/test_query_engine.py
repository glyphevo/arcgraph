from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

from arcgraph.core.graph_store import GraphStoreReader, GraphStoreWriter
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.core.query_engine import PathQueryLimits, QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.core.schemas import (
    BuildWarning,
    Edge,
    FactResolution,
    IndexMetadata,
    MergeMetrics,
    Node,
    SemanticDiagnostic,
)
from arcgraph.interfaces.trace import (
    _RuntimeTraceRecorder,
    import_runtime_trace,
    run_runtime_trace,
)
from arcgraph.interfaces.reports import render_impact_markdown

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


def test_query_imports_and_symbol(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    engine = QueryEngine(output_dir)
    imports = engine.imports("pkg.api")
    symbol = engine.symbol("pkg.service.Greeter.greet")

    assert {edge["target"] for edge in imports["outgoing"]} == {
        "ext:fastapi",
        "mod:pkg.container",
        "mod:pkg.service",
    }
    assert symbol["matches"][0]["id"] == "method:pkg.service.Greeter.greet"

    impact = engine.impact("pkg.service.build_message")
    assert impact["assurance"]["posture"] == "limited"
    assert impact["assurance"]["target_resolutions"][0]["status"] == "resolved"
    assert impact["assurance"]["limits"]["traversal_truncated"] is False
    assert (
        "Test coverage and test sufficiency are not established."
        in impact["assurance"]["non_claims"]
    )


def test_query_responses_have_consistent_envelopes(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    engine = QueryEngine(output_dir)
    payloads = [
        engine.current(),
        engine.stats(),
        engine.semantic_stats(),
        engine.unresolved(),
        engine.bindings("pkg.service.MemoryService.create_memory"),
        engine.types("pkg.service.MemoryService.create_memory"),
        engine.callsites("pkg.service.MemoryService.create_memory"),
        engine.imports("pkg.api"),
        engine.symbol("pkg.service.Greeter.greet"),
        engine.callers("pkg.service.build_message"),
        engine.callees("pkg.api.hello"),
        engine.impact("pkg.service.build_message"),
        engine.route("POST", "/memories"),
        engine.worker("generate_embedding_task"),
        engine.entrypoint_flow("route:POST:/memories"),
        engine.architecture(),
        engine.tests("pkg.service.build_message"),
        engine.similar("pkg.service.normalize_title"),
    ]

    required_keys = {
        "schema_version",
        "index_version",
        "freshness",
        "status",
        "capabilities",
        "warnings",
    }
    for payload in payloads:
        assert required_keys <= payload.keys()
        assert payload["status"] in {"available", "partial", "unavailable"}
        assert isinstance(payload["capabilities"], dict)
        assert isinstance(payload["warnings"], list)

    assert engine.symbol("pkg.missing.Nope")["status"] == "partial"
    assert engine.callers("pkg.missing.Nope")["warnings"]
    assert engine.impact("pkg.missing.Nope")["status"] == "partial"
    assert engine.route("GET", "/missing")["status"] == "unavailable"
    assert engine.worker("missing")["status"] == "unavailable"


def test_entrypoint_flow_edge_kinds_follow_platform_contract(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    route = Node(
        id="route:POST:/flow",
        kind="route",
        name="POST /flow",
        qualname="POST /flow",
    )
    handler = Node(
        id="fn:pkg.api.flow",
        kind="function",
        name="flow",
        qualname="pkg.api.flow",
    )
    service = Node(
        id="fn:pkg.service.run",
        kind="function",
        name="run",
        qualname="pkg.service.run",
    )
    table = Node(id="table:flow", kind="table", name="flow", qualname="flow")
    topic = Node(id="queue:events", kind="queue", name="events", qualname="events")
    metadata = IndexMetadata(
        index_version="entrypoint-flow",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )

    GraphStoreWriter(output_dir).write(
        metadata,
        files=[],
        nodes=[route, handler, service, table, topic],
        edges=[
            Edge(source=route.id, target=handler.id, kind="registers"),
            Edge(source=handler.id, target=service.id, kind="calls"),
            Edge(source=handler.id, target=table.id, kind="maps_to"),
            Edge(source=handler.id, target=topic.id, kind="publishes"),
        ],
        warnings=[],
    )

    flow = QueryEngine(output_dir).entrypoint_flow(route.id, max_depth=3)
    edge_kinds = {edge["kind"] for edge in flow["edges"]}

    assert flow["status"] == "available"
    assert "registers" in edge_kinds
    assert "calls" in edge_kinds
    assert "maps_to" not in edge_kinds
    assert "publishes" not in edge_kinds


def test_route_phrasing_without_slash_stays_a_symbol_query(tmp_path: Path) -> None:
    """Method words without a slash path are prose, not route lookups."""

    output_dir = tmp_path / "arcgraph"
    handler = Node(
        id="fn:get_users",
        kind="function",
        name="get_users",
        qualname="get missing",
    )
    prose = Node(
        id="fn:get_users_helper",
        kind="function",
        name="get_users_helper",
        qualname="get users helper",
    )
    metadata = IndexMetadata(
        index_version="route-fallback",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    GraphStoreWriter(output_dir).write(
        metadata,
        files=[],
        nodes=[handler, prose],
        edges=[],
        warnings=[],
    )
    engine = QueryEngine(output_dir)

    assert engine.impact("get missing")["resolved_targets"] == [handler.id]
    assert engine.impact("get users helper")["resolved_targets"] == [prose.id]
    # Slash forms keep the route/path interpretation and resolve to nothing
    # when neither a route nor a file path matches.
    assert engine.impact("GET /missing")["resolved_targets"] == []


def test_unique_short_name_resolves_but_ambiguous_name_fails_closed(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    first = Node(
        id="fn:pkg.alpha.read_guarded_file",
        kind="function",
        name="readGuardedFile",
        qualname="pkg.alpha.readGuardedFile",
        path="src/pkg/alpha.py",
    )
    second = Node(
        id="fn:pkg.beta.read_guarded_file",
        kind="function",
        name="otherName",
        qualname="pkg.beta.otherName",
        path="src/pkg/beta.py",
    )
    metadata = IndexMetadata(
        index_version="short-name-resolution",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    GraphStoreWriter(output_dir).write(
        metadata,
        files=[],
        nodes=[first, second],
        edges=[],
        warnings=[],
    )
    engine = QueryEngine(output_dir)

    unique = engine.symbol("readGuardedFile")
    assert unique["matches"][0]["id"] == first.id
    assert unique["target_resolution"]["strategy"] == "exact_name"

    with sqlite3.connect(engine.store.sqlite_path) as conn:
        conn.execute(
            "UPDATE nodes SET name = ? WHERE id = ?", ("readGuardedFile", second.id)
        )
        conn.commit()

    ambiguous = engine.impact("readGuardedFile")
    assert ambiguous["status"] == "partial"
    assert ambiguous["resolved_targets"] == []
    assert ambiguous["target_resolution"]["status"] == "ambiguous"
    assert [item["id"] for item in ambiguous["target_resolution"]["candidates"]] == [
        first.id,
        second.id,
    ]


def test_qualname_suffix_resolution_requires_a_unique_boundary_match(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    nodes = [
        Node(
            id=f"method:pkg.{module}.Guard.read",
            kind="method",
            name="read",
            qualname=f"pkg.{module}.Guard.read",
            path=f"src/pkg/{module}.py",
        )
        for module in ("alpha", "beta")
    ]
    metadata = IndexMetadata(
        index_version="suffix-resolution",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    GraphStoreWriter(output_dir).write(
        metadata, files=[], nodes=nodes, edges=[], warnings=[]
    )

    resolution = QueryEngine(output_dir).symbol("Guard.read")["target_resolution"]

    assert resolution["status"] == "ambiguous"
    assert resolution["strategy"] == "qualname_suffix"
    assert resolution["resolved_ids"] == []


def test_single_target_resolution_preserves_non_symbol_ids_and_qualnames(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    table = Node(
        id="table:public.users",
        kind="table",
        name="users",
        qualname="public.users",
        path="schema.sql",
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="non-symbol-resolution",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[table],
        edges=[],
        warnings=[],
    )
    engine = QueryEngine(output_dir)

    by_id = engine.symbol(table.id)
    by_qualname = engine.symbol("public.users")

    assert by_id["matches"][0]["id"] == table.id
    assert by_id["target_resolution"]["strategy"] == "node_id"
    assert by_qualname["matches"][0]["id"] == table.id
    assert by_qualname["target_resolution"]["strategy"] == "exact_qualname"


def test_worker_policy_does_not_return_a_route_with_the_same_query(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    route = Node(
        id="route:GET:/users",
        kind="route",
        name="GET /users",
        qualname="GET /users",
    )
    worker = Node(
        id="worker:jobs:users",
        kind="worker_task",
        name="GET /users",
        qualname="jobs.GET /users",
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="worker-route-isolation",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[route, worker],
        edges=[],
        warnings=[],
    )

    payload = QueryEngine(output_dir).worker("GET /users")

    assert [item["id"] for item in payload["workers"]] == [worker.id]
    assert payload["target_resolution"]["strategy"] == "worker_alias"


def test_ambiguous_candidates_and_unresolved_suggestions_are_bounded(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    nodes = [
        Node(
            id=f"fn:pkg.module_{index}.shared",
            kind="function",
            name="shared",
            qualname=f"pkg.module_{index}.shared",
            path=f"src/pkg/module_{index}.py",
        )
        for index in range(12)
    ]
    metadata = IndexMetadata(
        index_version="bounded-resolution-evidence",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    GraphStoreWriter(output_dir).write(
        metadata, files=[], nodes=nodes, edges=[], warnings=[]
    )
    engine = QueryEngine(output_dir)

    ambiguous = engine.symbol("shared")["target_resolution"]
    unresolved = engine.symbol("module_1.share")["target_resolution"]

    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["resolved_ids"] == []
    assert len(ambiguous["candidates"]) == 8
    assert ambiguous["candidates_truncated"] is True
    assert unresolved["status"] == "unresolved"
    assert unresolved["resolved_ids"] == []
    assert len(unresolved["suggestions"]) <= 8


def test_similarity_pattern_family_reads_only_the_target_component(
    tmp_path: Path, monkeypatch
) -> None:
    output_dir = tmp_path / "arcgraph"

    def node(name: str) -> Node:
        return Node(
            id=f"fn:pkg.{name}",
            kind="function",
            name=name,
            qualname=f"pkg.{name}",
            path=f"src/pkg/{name}.py",
            properties={
                "similarity": {
                    "algorithm": "test_similarity",
                    "bucket": "test",
                    "ngram_count": 12,
                }
            },
        )

    nodes = [node(name) for name in ("alpha", "beta", "gamma", "delta", "epsilon")]
    by_name = {item.name: item for item in nodes}
    edges = [
        Edge(
            source=by_name[source].id,
            target=by_name[target].id,
            kind="similar_to",
            properties={"score": score},
        )
        for source, target, score in (
            ("alpha", "beta", 0.9),
            ("beta", "gamma", 0.8),
            ("delta", "epsilon", 0.7),
        )
    ]
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="component-scoped-similarity",
            repo_root=str(tmp_path),
            source_roots=["src"],
            capabilities={"similarity": "available"},
        ),
        files=[],
        nodes=nodes,
        edges=edges,
        warnings=[],
    )
    engine = QueryEngine(output_dir)
    statements: list[str] = []
    original_connect = engine.store.connect

    def traced_connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    def reject_full_node_scan():
        raise AssertionError("similar() must not load every node in the index")

    monkeypatch.setattr(engine.store, "connect", traced_connect)
    monkeypatch.setattr(engine.store, "read_nodes", reject_full_node_scan)

    family = engine.similar("pkg.alpha")["pattern_family"]

    assert {item["id"] for item in family["members"]} == {
        by_name["alpha"].id,
        by_name["beta"].id,
        by_name["gamma"].id,
    }
    similarity_reads = [
        statement
        for statement in statements
        if "FROM edges" in statement and "similar_to" in statement
    ]
    assert similarity_reads
    assert all(
        "source IN" in statement or "target IN" in statement
        for statement in similarity_reads
    )


def test_method_path_alias_resolves_in_general_target_queries(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    route = Node(
        id="route:GET:/users",
        kind="route",
        name="GET /users",
        qualname="GET /users",
    )
    metadata = IndexMetadata(
        index_version="method-path-alias",
        repo_root=str(tmp_path),
        source_roots=["src"],
        capabilities={"similarity": "available"},
    )
    GraphStoreWriter(output_dir).write(
        metadata,
        files=[],
        nodes=[route],
        edges=[],
        warnings=[],
    )

    engine = QueryEngine(output_dir)

    assert engine.impact("GET /users")["resolved_targets"] == [route.id]
    assert engine.similar("GET /users")["status"] == "available"
    assert engine.tests("GET /users")["resolved_targets"] == [route.id]


def test_method_path_alias_includes_applicable_wildcard_route(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    get_route = Node(
        id="route:GET:/users",
        kind="route",
        name="GET /users",
        qualname="GET /users",
    )
    all_route = Node(
        id="route:ALL:/users",
        kind="route",
        name="ALL /users",
        qualname="ALL /users",
    )
    get_handler = Node(
        id="fn:get_users",
        kind="function",
        name="get_users",
        qualname="get_users",
    )
    audit_handler = Node(
        id="fn:audit_users",
        kind="function",
        name="audit_users",
        qualname="audit_users",
    )
    metadata = IndexMetadata(
        index_version="method-path-wildcard-alias",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    GraphStoreWriter(output_dir).write(
        metadata,
        files=[],
        nodes=[get_route, all_route, get_handler, audit_handler],
        edges=[
            Edge(source=get_route.id, target=get_handler.id, kind="registers"),
            Edge(source=all_route.id, target=audit_handler.id, kind="registers"),
        ],
        warnings=[],
    )

    engine = QueryEngine(output_dir)

    assert engine.impact("GET /users")["resolved_targets"] == [
        get_route.id,
        all_route.id,
    ]
    flow = engine.entrypoint_flow("GET /users")
    assert [node["id"] for node in flow["entrypoints"]] == [
        get_route.id,
        all_route.id,
    ]
    assert {node["id"] for node in flow["nodes"]} == {
        get_route.id,
        get_handler.id,
        all_route.id,
        audit_handler.id,
    }


def test_visual_workbench_queries_are_query_engine_owned(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    function = Node(
        id="fn:pkg.service.run_query",
        kind="function",
        name="run_query",
        qualname="pkg.service.run_query",
        path="src/pkg/service.py",
        start_line=5,
    )
    caller = Node(
        id="fn:pkg.api.handle",
        kind="function",
        name="handle",
        qualname="pkg.api.handle",
        path="src/pkg/api.py",
        start_line=3,
    )
    diagnostic = SemanticDiagnostic(
        diagnostic_id="diagnostic:run-query",
        index_version="workbench-query-test",
        diagnostic_kind="unresolved_callsite",
        message="Unresolved call",
        severity="info",
        path="src/pkg/service.py",
        start_line=6,
        frontend_name="python-v1-compat-shim",
        properties={"source_scope": function.id},
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="workbench-query-test",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[function, caller],
        edges=[Edge(source=caller.id, target=function.id, kind="calls")],
        warnings=[],
        diagnostics=[diagnostic],
    )

    engine = QueryEngine(output_dir)
    status = engine.workspace_status()
    search = engine.search_nodes(query="run", kind="function", limit=10)
    detail = engine.node_detail(function.id)

    assert status["visual_contract"]["default_view"] == "system_map"
    assert "configures" in status["visual_contract"]["resource_edge_kinds"]
    assert [item["id"] for item in search["items"]] == [function.id]
    assert detail["node"]["id"] == function.id
    assert detail["callers"][0]["id"] == caller.id
    assert detail["diagnostics"][0]["diagnostic_id"] == diagnostic.diagnostic_id


def test_semantic_stats_and_unresolved_query(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    engine = QueryEngine(output_dir)
    stats = engine.semantic_stats()
    unresolved = engine.unresolved("src/pkg/worker.py")
    bindings = engine.bindings("pkg.service.MemoryService.create_memory")
    types = engine.types("pkg.service.MemoryService.create_memory")
    callsites = engine.callsites("pkg.service.MemoryService.create_memory")
    method_unresolved = engine.unresolved("pkg.service.MemoryService.create_memory")

    assert stats["capabilities"]["semantic_stats"] == "available"
    assert stats["metrics"]["callsite_total"] > 0
    assert stats["metrics"]["unresolved_callsite_total"] > 0
    assert stats["metrics"]["by_diagnostic_kind"]["unresolved_callsite"] > 0
    assert stats["binding_summary"]["binding_total"] > 0
    assert stats["binding_summary"]["instance_attribute_binding_total"] > 0
    assert stats["metrics"]["binding_summary"] == stats["binding_summary"]
    assert stats["type_summary"]["type_ref_total"] > 0
    assert stats["type_summary"]["resolved_type_ref_total"] > 0
    assert stats["metrics"]["type_summary"] == stats["type_summary"]
    assert unresolved["capabilities"]["unresolved"] == "available"
    assert unresolved["summary"]["total"] > 0
    assert bindings["capabilities"]["bindings"] == "available"
    assert bindings["summary"]["total"] > 0
    assert types["capabilities"]["types"] == "available"
    assert types["summary"]["total"] > 0
    assert callsites["capabilities"]["callsites"] == "available"
    assert callsites["summary"]["total"] > 0
    assert callsites["summary"]["by_resolution_status"]["resolved"] > 0
    assert any(
        item["properties"]["raw_expression"] == "redis.enqueue_job"
        for item in unresolved["unresolved"]
    )
    assert method_unresolved["resolved_targets"] == [
        "method:pkg.service.MemoryService.create_memory"
    ]
    assert method_unresolved["summary"]["total"] == 0
    assert method_unresolved["unresolved"] == []


def test_symbol_query_exposes_scope_bindings(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    engine = QueryEngine(output_dir)
    init_method = engine.symbol("pkg.service.MemoryService.__init__")
    method_bindings = {
        item["name"]: item
        for item in init_method["matches"][0]["properties"]["bindings"]
    }

    assert init_method["capabilities"]["bindings"] == "available"
    assert method_bindings["repo"]["kind"] == "parameter"
    assert method_bindings["repo"]["annotation"] == "object"
    assert method_bindings["self.repo"]["kind"] == "instance_attribute"
    assert method_bindings["self.repo"]["value"] == "repo"


def test_symbol_query_exposes_scope_type_refs(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    engine = QueryEngine(output_dir)
    create_memory = engine.symbol("pkg.api.create_memory")
    type_refs = {
        item["name"]: item
        for item in create_memory["matches"][0]["properties"]["type_refs"]
    }
    bindings = {
        item["name"]: item
        for item in create_memory["matches"][0]["properties"]["bindings"]
    }

    assert create_memory["capabilities"]["types"] == "available"
    assert type_refs["service"]["strategy"] == "annotation"
    assert type_refs["service"]["type_id"] == "class:pkg.service.MemoryService"
    assert type_refs["return"]["type_id"] == "builtin:object"
    assert bindings["service"]["type_ref"] == "class:pkg.service.MemoryService"


def test_v2_receiver_call_resolution_is_opt_in(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    metadata, _ = ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        enable_v2_call_resolution=True,
    ).build()

    engine = QueryEngine(output_dir)
    greeter_callers = engine.callers("pkg.service.Greeter")
    create_memory_callees = engine.callees("pkg.api.create_memory")
    stats = engine.semantic_stats()

    assert metadata.capabilities["receiver_resolution"] == "available"
    assert stats["metrics"]["by_edge_kind"]["constructs"] > 0
    assert any(
        edge["kind"] == "constructs" and edge["source"] == "fn:pkg.api.hello"
        for edge in greeter_callers["edges"]
    )
    create_edges = [
        edge
        for edge in create_memory_callees["edges"]
        if edge["target"] == "method:pkg.service.MemoryService.create_memory"
    ]
    assert create_edges
    assert create_edges[0]["properties"]["callsite"]["receiver_expression"] == "service"


def test_query_callers_callees_impact_and_tests(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    engine = QueryEngine(output_dir)
    callers = engine.callers("pkg.service.build_message")
    callees = engine.callees("pkg.api.hello")
    impact = engine.impact("pkg.service.build_message")
    debug_impact = engine.impact(
        "pkg.service.build_message", profile="debugging_default"
    )
    calls_only_impact = engine.impact(
        "pkg.service.build_message", include_edge_kinds=["calls"]
    )
    tests = engine.tests("pkg.service.build_message")
    similar = engine.similar("pkg.service.normalize_title")
    similar_by_path = engine.similar("src/pkg/service.py")

    assert {node["id"] for node in callers["callers"]} == {
        "fn:pkg.api.hello",
        "fn:tests.service_cases.test_build_message",
    }
    assert "fn:pkg.service.build_message" in {node["id"] for node in callees["callees"]}
    assert impact["capabilities"]["entrypoints"] == "available"
    assert impact["capabilities"]["impact"] == "call-import-entrypoint-resource"
    assert debug_impact["confidence_profile"]["name"] == "debugging_default"
    assert calls_only_impact["edge_kind_filter"] == {
        "include": ["calls"],
        "exclude": [],
    }
    assert calls_only_impact["call_impact"]["edges"]
    assert calls_only_impact["import_impact"]["edges"] == []
    assert all(
        edge["kind"] == "calls"
        for edge in [
            *calls_only_impact["call_impact"]["edges"],
            *calls_only_impact["import_impact"]["edges"],
            *calls_only_impact["entrypoint_impact"]["edges"],
            *calls_only_impact["resource_impact"]["edges"],
        ]
    )
    assert tests["evidence"] == "heuristic"
    assert tests["capabilities"]["coverage"] == "unavailable"
    assert tests["test_gaps"] == [
        {
            "target": "fn:pkg.service.build_message",
            "reason": "coverage data unavailable",
            "severity": "unknown",
        }
    ]
    assert tests["candidates"] == [
        {
            "path": "tests/service_cases.py",
            "reason": "imports mod:pkg.service",
            "evidence": "heuristic",
        }
    ]
    assert similar["status"] == "available"
    assert similar["capabilities"]["similarity"] == "available"
    assert similar["similar"][0]["node"]["id"] == "fn:pkg.service.normalize_label"
    assert similar["similar"][0]["score"] >= 0.95
    assert "similarity" not in similar["similar"][0]["node"]["properties"]
    assert similar_by_path["similar"] == []


def test_extends_edges_flow_through_callers_callees_impact(tmp_path: Path) -> None:
    """Extends edges must be visible in callers, callees, and impact queries.

    Fixture project has ``Memory(Base)`` in ``src/pkg/model.py``, generating
    ``class:pkg.model.Memory -> class:pkg.model.Base`` extends edge.
    """
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    engine = QueryEngine(output_dir)

    # callers(Base) should include Memory (Memory extends Base)
    callers = engine.callers("class:pkg.model.Base")
    caller_ids = {node["id"] for node in callers["callers"]}
    extends_edges = [e for e in callers["edges"] if e["kind"] == "extends"]
    assert "class:pkg.model.Memory" in caller_ids
    assert len(extends_edges) >= 1
    assert any(
        e["source"] == "class:pkg.model.Memory"
        and e["target"] == "class:pkg.model.Base"
        for e in extends_edges
    )

    # callees(Memory) should include Base (Memory extends Base)
    callees = engine.callees("class:pkg.model.Memory")
    callee_ids = {node["id"] for node in callees["callees"]}
    assert "class:pkg.model.Base" in callee_ids

    # impact(Base) blast radius should include Memory
    impact = engine.impact("class:pkg.model.Base")
    affected_ids = {node["id"] for node in impact["call_impact"]["affected_symbols"]}
    assert "class:pkg.model.Memory" in affected_ids


def test_ast_fallback_tracks_module_and_class_body_calls(tmp_path: Path) -> None:
    repo_root = tmp_path / "project"
    package_root = repo_root / "src" / "pkg"
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "helpers.py").write_text(
        "\n".join(
            [
                "def get_logger(name: str) -> object:",
                "    return object()",
                "",
                "def build_factory() -> object:",
                "    return object()",
            ]
        ),
        encoding="utf-8",
    )
    (package_root / "app.py").write_text(
        "\n".join(
            [
                "from .helpers import build_factory, get_logger",
                "",
                "logger = get_logger(__name__)",
                "",
                "class Registry:",
                "    factory = build_factory()",
            ]
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()

    engine = QueryEngine(output_dir)
    logger_callers = engine.callers("pkg.helpers.get_logger")
    factory_callers = engine.callers("pkg.helpers.build_factory")

    assert {node["id"] for node in logger_callers["callers"]} == {"mod:pkg.app"}
    assert {
        evidence["kind"]
        for edge in logger_callers["edges"]
        for evidence in edge["evidence"]
    } == {"ast_definition_time_call"}
    assert {node["id"] for node in factory_callers["callers"]} == {
        "class:pkg.app.Registry"
    }
    assert {
        evidence["kind"]
        for edge in factory_callers["edges"]
        for evidence in edge["evidence"]
    } == {"ast_definition_time_call"}


def test_query_phase4_route_worker_and_architecture(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    engine = QueryEngine(output_dir)
    route = engine.route("POST", "/memories")
    worker = engine.worker("generate_embedding_task")
    worker_by_queue = engine.worker("arq:embedding")
    worker_by_handler = engine.worker("fn:pkg.worker.generate_embedding_task")
    architecture = engine.architecture()
    impact = engine.impact("pkg.service.MemoryService.create_memory")

    assert route["route"]["id"] == "route:POST:/memories"
    assert {edge["kind"] for edge in route["edges"]} == {"injects", "invokes"}
    assert "fn:pkg.api.create_memory" in {node["id"] for node in route["targets"]}
    assert worker["workers"][0]["id"] == "worker:arq:embedding:generate_embedding_task"
    assert "fn:pkg.worker.schedule_embedding" in {
        edge["source"] for edge in worker["incoming"]
    }
    assert {node["id"] for node in worker_by_queue["workers"]} == {
        "queue:arq:embedding",
        "worker:arq:embedding:cleanup_task",
        "worker:arq:embedding:generate_embedding_task",
    }
    assert {node["id"] for node in worker_by_handler["workers"]} == {
        "worker:arq:embedding:generate_embedding_task"
    }
    assert architecture["entrypoints"] == {
        "route": 2,
        "mcp_tool": 1,
        "worker_task": 2,
        "component": 0,
        "cli_command": 0,
    }
    assert architecture["resources"] == {
        "table": 1,
        "queue": 1,
        "config": 0,
        "log_sink": 0,
        "pydantic_model": 0,
    }
    assert "route:POST:/memories" in {
        node["id"] for node in impact["entrypoint_impact"]["entrypoints"]
    }


def test_phase8_precise_references_and_coverage(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    scip_path = tmp_path / "scip-index.json"
    pyright_path = tmp_path / "pyright-export.json"
    coverage_path = tmp_path / "coverage.json"
    scip_path.write_text(
        json.dumps(
            {
                "package": {"name": "sample_project", "version": "test"},
                "source_roots": ["src", "tests"],
                "documents": [
                    {
                        "relative_path": "tests/service_cases.py",
                        "occurrences": [
                            {
                                "symbol": "fn:pkg.service.build_message",
                                "enclosing_symbol": "mod:pkg.service",
                                "range": [7, 0, 7, 17],
                                "role": "definition",
                            },
                            {
                                "symbol": "fn:pkg.service.build_message",
                                "enclosing_symbol": "fn:tests.service_cases.test_build_message",
                                "range": [5, 11, 5, 24],
                                "kind": "call",
                            },
                            {
                                "symbol": "class:pkg.service.MemoryService",
                                "enclosing_symbol": "fn:pkg.api.create_memory",
                                "range": [7, 22, 7, 35],
                                "kind": "type_occurrence",
                            },
                            {
                                "symbol": "fn:pkg.service.build_message",
                                "enclosing_symbol": "fn:pkg.api.hello",
                                "range": [13, 11, 13, 24],
                                "kind": "reference",
                            },
                            {
                                "source": "class:pkg.service.MemoryService",
                                "target": "class:pkg.service.Greeter",
                                "range": [1, 0, 1, 10],
                                "kind": "implementation",
                            },
                            {
                                "symbol": "fn:pkg.service.missing",
                                "enclosing_symbol": "fn:pkg.api.hello",
                                "range": [14, 0, 14, 7],
                                "kind": "reference",
                            },
                        ],
                    }
                ],
                "diagnostics": [
                    {
                        "kind": "coverage_note",
                        "path": "tests/service_cases.py",
                        "message": "sample precision diagnostic",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    coverage_path.write_text(
        json.dumps(
            {
                "tests": [
                    {
                        "test": "fn:tests.service_cases.test_build_message",
                        "covers": [
                            {
                                "path": "src/pkg/service.py",
                                "symbols": ["fn:pkg.service.build_message"],
                                "lines": [8, 9],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    pyright_path.write_text(
        json.dumps(
            {
                "package": {"name": "sample_project", "version": "test"},
                "source_roots": ["src", "tests"],
                "references": [
                    {
                        "source": "fn:pkg.api.hello",
                        "target": "fn:pkg.service.build_message",
                        "path": "src/pkg/api.py",
                        "line": 15,
                        "column": 11,
                        "kind": "call",
                    },
                    {
                        "source": "fn:pkg.api.create_memory",
                        "target": "class:pkg.service.MemoryService",
                        "path": "src/pkg/api.py",
                        "line": 20,
                        "column": 13,
                        "kind": "type_occurrence",
                    },
                    {
                        "source": "fn:pkg.api.hello",
                        "target": "fn:pkg.missing.nope",
                        "path": "src/pkg/api.py",
                        "line": 16,
                        "column": 0,
                        "kind": "reference",
                    },
                ],
                "type_info": [
                    {
                        "subject": "fn:pkg.api.create_memory",
                        "target": "class:pkg.service.MemoryService",
                        "name": "service",
                        "type": "pkg.service.MemoryService",
                        "path": "src/pkg/api.py",
                        "line": 20,
                        "column": 4,
                        "evidence_kind": "pyright_lsp_type_info",
                        "lsp_method": "textDocument/typeDefinition",
                    }
                ],
                "lsp": {
                    "status": "available",
                    "probe_total": 2,
                    "requestable_probe_total": 1,
                    "skipped_unmappable_total": 1,
                    "requests_total": 3,
                    "type_info_total": 1,
                    "unresolved_total": 0,
                },
                "diagnostics": [
                    {
                        "kind": "type_partial",
                        "path": "src/pkg/api.py",
                        "message": "sample pyright diagnostic",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"
    metadata, _ = ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        scip_index_path=scip_path,
        pyright_export_path=pyright_path,
        coverage_path=coverage_path,
    ).build()

    engine = QueryEngine(output_dir)
    current = engine.current()
    semantic_stats = engine.semantic_stats()
    callers = engine.callers("pkg.service.build_message")
    tests = engine.tests("pkg.service.build_message")
    impact = engine.impact("pkg.service.build_message")
    report = render_impact_markdown(impact)
    store_edges = GraphStoreReader.from_current(output_dir).read_edges()

    assert metadata.capabilities["precise_references"] == "available"
    assert metadata.capabilities["precision"] == "precision_available"
    assert metadata.capabilities["coverage"] == "available"
    assert current["coverage_metrics"]["status"] == "available"
    assert current["coverage_metrics"]["edges_imported"] > 0
    assert current["coverage_metrics"]["stale"] is False
    assert current["precision_metrics"]["occurrences_total"] == 9
    assert current["precision_metrics"]["resolved_occurrences"] == 7
    assert current["precision_metrics"]["unresolved_occurrences"] == 2
    assert current["precision_metrics"]["definitions"] == 1
    assert current["precision_metrics"]["calls"] == 2
    assert current["precision_metrics"]["references"] == 1
    assert current["precision_metrics"]["type_occurrences"] == 3
    assert current["precision_metrics"]["implementations"] == 1
    assert current["precision_metrics"]["diagnostics"] == 2
    assert current["precision_metrics"]["scip_occurrences_total"] == 6
    assert current["precision_metrics"]["scip_resolved_occurrences"] == 5
    assert current["precision_metrics"]["scip_unresolved_occurrences"] == 1
    assert current["precision_metrics"]["pyright_records_total"] == 3
    assert current["precision_metrics"]["pyright_type_info_total"] == 1
    assert current["precision_metrics"]["pyright_lsp_probe_total"] == 2
    assert current["precision_metrics"]["pyright_lsp_requestable_probe_total"] == 1
    assert current["precision_metrics"]["pyright_lsp_skipped_unmappable_total"] == 1
    assert current["precision_metrics"]["pyright_lsp_type_info_total"] == 1
    assert current["precision_metrics"]["pyright_lsp_requests_total"] == 3
    assert current["precision_metrics"]["pyright_resolved_type_info"] == 1
    assert current["precision_metrics"]["source_roots"] == ["src", "tests"]
    assert semantic_stats["type_summary"]["by_strategy"]["pyright"] == 1
    assert any(
        edge["confidence"] == "confirmed"
        and any(item["kind"] == "scip_call" for item in edge["evidence"])
        for edge in callers["edges"]
    )
    assert any(
        edge.kind == "defines"
        and edge.source == "mod:pkg.service"
        and edge.target == "fn:pkg.service.build_message"
        and any(item.kind == "scip_definition" for item in edge.evidence)
        for edge in store_edges
    )
    assert any(
        edge.kind == "references"
        and edge.semantic_role == "type_occurrence"
        and any(item.kind == "scip_type_occurrence" for item in edge.evidence)
        for edge in store_edges
    )
    assert any(
        edge.kind == "calls"
        and edge.source == "fn:pkg.api.hello"
        and edge.target == "fn:pkg.service.build_message"
        and any(item.kind == "pyright_call" for item in edge.evidence)
        for edge in store_edges
    )
    assert any(
        edge.kind == "references"
        and edge.semantic_role == "type_info"
        and any(item.kind == "pyright_type_info" for item in edge.evidence)
        for edge in store_edges
    )
    assert any(
        edge.kind == "implements"
        and any(item.kind == "scip_implementation" for item in edge.evidence)
        for edge in store_edges
    )
    assert tests["evidence"] == "coverage+heuristic"
    assert tests["coverage"]["status"] == "available"
    assert tests["coverage"]["metrics"]["status"] == "available"
    assert "fn:pkg.service.build_message" in tests["coverage"]["covered_targets"]
    assert tests["candidates"] == [
        {
            "path": "tests/service_cases.py",
            "reason": "imports mod:pkg.service; runtime coverage covers target",
            "evidence": "coverage+heuristic",
        }
    ]
    assert tests["test_gaps"] == []
    assert impact["test_gaps"] == []
    assert impact["confidence_summary"]["confirmed_edges"] > 0
    assert "confirmed" in impact["confidence_summary"]["by_confidence"]
    assert isinstance(impact["unresolved_risks"]["items"], list)
    assert "## Confidence Impact" in report
    assert "## Unresolved Risks" in report
    assert "## Coverage" in report
    assert "Status: available" in report


def test_scip_native_json_infers_sources_and_callsites(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    scip_path = tmp_path / "native-scip.json"
    scip_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "relative_path": "pkg/api.py",
                        "occurrences": [
                            {
                                "symbol": (
                                    "scip-python python sample_project 0.1 "
                                    "pkg/api.py/hello()."
                                ),
                                "range": [13, 4, 13, 9],
                                "symbolRoles": 1,
                            },
                            {
                                "symbol": (
                                    "scip-python python sample_project 0.1 "
                                    "pkg/service.py/Greeter#"
                                ),
                                "range": [14, 14, 14, 21],
                            },
                            {
                                "symbol": (
                                    "scip-python python sample_project 0.1 "
                                    "pkg/service.py/build_message()."
                                ),
                                "range": [15, 11, 15, 24],
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"

    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        scip_index_path=scip_path,
        enable_v2_call_resolution=True,
    ).build()

    engine = QueryEngine(output_dir)
    current = engine.current()
    store_edges = GraphStoreReader.from_current(output_dir).read_edges()
    scip_call_edges = [
        edge
        for edge in store_edges
        if edge.source == "fn:pkg.api.hello"
        and edge.kind == "calls"
        and any(item.kind == "scip_call" for item in edge.evidence)
    ]

    assert current["capabilities"]["precision"] == "precision_available"
    assert current["precision_metrics"]["occurrences_total"] == 3
    assert current["precision_metrics"]["resolved_occurrences"] == 3
    assert current["precision_metrics"]["definitions"] == 1
    assert current["precision_metrics"]["calls"] == 2
    assert any(
        edge.target == "fn:pkg.service.build_message" and edge.resolution.callsite_id
        for edge in scip_call_edges
    )
    assert any(edge.target == "class:pkg.service.Greeter" for edge in scip_call_edges)
    assert any(
        edge.kind == "defines"
        and edge.source == "mod:pkg.api"
        and edge.target == "fn:pkg.api.hello"
        and any(item.kind == "scip_definition" for item in edge.evidence)
        for edge in store_edges
    )


def test_v2_resolves_module_level_constructor_singleton_methods(
    tmp_path: Path,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    service_path = repo_root / "src" / "pkg" / "service.py"
    service_path.write_text(
        service_path.read_text(encoding="utf-8") + """


default_memory_service = MemoryService(repo=object())


async def create_default(content: str) -> object:
    return await default_memory_service.create_memory(content)
""",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"

    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        enable_v2_call_resolution=True,
    ).build()

    callees = QueryEngine(output_dir).callees("pkg.service.create_default")

    assert any(
        edge["target"] == "method:pkg.service.MemoryService.create_memory"
        and edge["resolution"]["strategy"] == "receiver_type"
        for edge in callees["edges"]
    )


def test_phase8_optional_inputs_degrade_to_ast_and_heuristics(
    tmp_path: Path,
) -> None:
    repo_root = _copy_fixture(tmp_path)
    scip_path = tmp_path / "broken-scip.json"
    missing_coverage_path = tmp_path / "missing-coverage.json"
    scip_path.write_text("{not-json", encoding="utf-8")
    output_dir = tmp_path / "arcgraph"

    metadata, _ = ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        scip_index_path=scip_path,
        coverage_path=missing_coverage_path,
    ).build()
    engine = QueryEngine(output_dir)
    current = engine.current()
    tests = engine.tests("pkg.service.build_message")

    assert metadata.capabilities["precise_references"] == "ast-fallback"
    assert metadata.capabilities["precision"] == "precision_partial"
    assert metadata.capabilities["coverage"] == "unavailable"
    assert current["coverage_metrics"]["status"] == "partial"
    assert current["coverage_metrics"]["reason"] == "missing"
    assert {warning.kind for warning in engine.store.read_warnings()} == {
        "coverage_missing",
        "scip_parse_error",
    }
    assert tests["evidence"] == "heuristic"
    assert tests["test_gaps"][0]["reason"] == "coverage data unavailable"


def test_coverage_stale_metrics_mark_partial(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(
        json.dumps(
            {
                "tests": [
                    {
                        "test": "fn:tests.service_cases.test_build_message",
                        "covers": [
                            {
                                "path": "src/pkg/service.py",
                                "symbols": ["fn:pkg.service.build_message"],
                                "lines": [8, 9],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    service_path = repo_root / "src" / "pkg" / "service.py"
    stale_mtime = coverage_path.stat().st_mtime + 10
    os.utime(service_path, (stale_mtime, stale_mtime))
    output_dir = tmp_path / "arcgraph"

    metadata, _ = ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        coverage_path=coverage_path,
    ).build()

    engine = QueryEngine(output_dir)
    current = engine.current()
    tests = engine.tests("pkg.service.build_message")

    assert metadata.capabilities["coverage"] == "partial"
    assert current["coverage_metrics"]["status"] == "partial"
    assert current["coverage_metrics"]["stale"] is True
    assert current["coverage_metrics"]["stale_paths"] == ["src/pkg/service.py"]
    assert tests["coverage"]["status"] == "partial"
    assert any(
        "Coverage evidence is partial" in warning for warning in tests["warnings"]
    )
    assert "coverage_stale" in {
        warning.kind for warning in engine.store.read_warnings()
    }


def test_runtime_trace_imports_runtime_only_edges(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    trace_path = tmp_path / "runtime-trace.json"
    trace_payload = {
        "trace_run": {
            "trace_run_id": "run-1",
            "source": "pytest",
            "frontend_version": "test",
        },
        "events": [
            {
                "kind": "dynamic_call",
                "source": "fn:pkg.api.create_memory",
                "target": "fn:pkg.service.build_message",
                "path": "src/pkg/api.py",
                "line": 21,
                "column": 17,
                "test_id": "tests/service_cases.py::test_build_message",
                "args": ["do-not-persist"],
                "headers": {"authorization": "secret"},
            },
            {
                "kind": "call",
                "source": "fn:pkg.api.create_memory",
                "target": "fn:pkg.missing.nope",
                "path": "src/pkg/api.py",
                "line": 22,
            },
        ],
    }
    trace_path.write_text(json.dumps(trace_payload), encoding="utf-8")
    output_dir = tmp_path / "arcgraph"

    metadata, _ = ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        runtime_trace_path=trace_path,
    ).build()

    engine = QueryEngine(output_dir)
    current = engine.current()
    callers = engine.callers("pkg.service.build_message")
    impact = engine.impact("pkg.service.build_message")
    store_edges = GraphStoreReader.from_current(output_dir).read_edges()
    runtime_edge = next(
        edge
        for edge in store_edges
        if edge.source == "fn:pkg.api.create_memory"
        and edge.target == "fn:pkg.service.build_message"
        and any(item.kind == "runtime_trace_dynamic_call" for item in edge.evidence)
    )

    assert metadata.capabilities["runtime_trace"] == "partial"
    assert current["runtime_metrics"]["events_total"] == 2
    assert current["runtime_metrics"]["edges_imported"] == 1
    assert current["runtime_metrics"]["unresolved_events"] == 1
    assert current["runtime_metrics"]["redacted_fields"] == 2
    assert runtime_edge.confidence == "runtime-only"
    assert runtime_edge.resolution.status == "runtime-only"
    assert impact["confidence_summary"]["runtime_only_edges"] >= 1
    assert any(
        edge["confidence"] == "runtime-only" for edge in impact["runtime_impact"]
    )
    assert "args" not in runtime_edge.properties
    assert "headers" not in runtime_edge.properties
    assert any(
        edge["source"] == "fn:pkg.api.create_memory"
        and edge["confidence"] == "runtime-only"
        and any(
            item["kind"] == "runtime_trace_dynamic_call" for item in edge["evidence"]
        )
        for edge in callers["edges"]
    )
    assert "runtime_trace_redacted_fields" in {
        warning.kind for warning in engine.store.read_warnings()
    }


def test_trace_import_rejects_stale_trace(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    current = QueryEngine(output_dir).current()
    trace_path = tmp_path / "stale-runtime-trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "trace_run": {
                    "trace_run_id": "old-run",
                    "index_version": "different-index",
                },
                "events": [
                    {
                        "kind": "call",
                        "source": "fn:pkg.api.create_memory",
                        "target": "fn:pkg.service.build_message",
                        "path": "src/pkg/api.py",
                        "line": 21,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = import_runtime_trace(
        repo_root=repo_root,
        output_dir=output_dir,
        trace_path=trace_path,
    )
    imported = QueryEngine(output_dir).current()
    callers = QueryEngine(output_dir).callers("pkg.service.build_message")

    assert result["status"] == "partial"
    assert imported["runtime_metrics"]["status"] == "stale"
    assert imported["runtime_metrics"]["edges_imported"] == 0
    assert imported["index_version"] != current["index_version"]
    assert all(
        not any(item["kind"].startswith("runtime_trace_") for item in edge["evidence"])
        for edge in callers["edges"]
    )


def test_trace_import_enforces_wall_time_limit(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    trace_path = tmp_path / "limited-runtime-trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "trace_run": {"trace_run_id": "limited-run", "source": "pytest"},
                "events": [
                    {
                        "kind": "call",
                        "source": "fn:pkg.api.create_memory",
                        "target": "fn:pkg.service.build_message",
                        "path": "src/pkg/api.py",
                        "line": 21,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = import_runtime_trace(
        repo_root=repo_root,
        output_dir=output_dir,
        trace_path=trace_path,
        max_seconds=0.0,
    )
    current = QueryEngine(output_dir).current()
    warnings = QueryEngine(output_dir).store.read_warnings()

    assert result["status"] == "partial"
    assert current["runtime_metrics"]["wall_time_limited"] is True
    assert current["runtime_metrics"]["events_processed"] == 0
    assert current["runtime_metrics"]["edges_imported"] == 0
    assert "runtime_trace_wall_time_limit_exceeded" in {
        warning.kind for warning in warnings
    }


def test_runtime_trace_runner_records_project_calls(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    trace_path = tmp_path / "pytest-runtime-trace.json"

    result = run_runtime_trace(
        repo_root=repo_root,
        output_path=trace_path,
        pytest_args=["tests/service_cases.py::test_build_message", "-q"],
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
        max_events=200,
        max_seconds=30,
    )
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    events = payload["events"]

    assert result["status"] == "pass"
    assert result["events"] > 0
    assert payload["trace_run"]["source"] == "pytest"
    assert any(
        event["source"] == "fn:tests.service_cases.test_build_message"
        and event["target"] == "fn:pkg.service.build_message"
        for event in events
    )
    assert all("args" not in event and "kwargs" not in event for event in events)


def test_runtime_trace_runner_ignores_virtualenv_paths() -> None:
    assert _RuntimeTraceRecorder._is_ignored_runtime_path(
        Path("arcgraph/.venv/Lib/site-packages/pytest.py")
    )
    assert not _RuntimeTraceRecorder._is_ignored_runtime_path(
        Path("arcgraph/arcgraph/interfaces/trace.py")
    )


def test_query_freshness_reports_stale_files(tmp_path: Path) -> None:
    repo_root = _copy_fixture(tmp_path)
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    service_path = repo_root / "src" / "pkg" / "service.py"
    original = service_path.read_text(encoding="utf-8")

    try:
        service_path.write_text(
            original + "\n\ndef changed() -> str:\n    return 'changed'\n",
            encoding="utf-8",
        )
        freshness = QueryEngine(output_dir).current()["freshness"]
    finally:
        service_path.write_text(original, encoding="utf-8")

    assert freshness["status"] == "stale"
    assert "src/pkg/service.py" in freshness["stale_files"]
    assert "pkg.service" in freshness["stale_modules"]


def test_query_freshness_scans_indexed_typescript_extensions(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "App.tsx").write_text(
        "export function App() { return <main />; }\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()

    freshness = QueryEngine(output_dir).current()["freshness"]

    assert freshness["status"] == "fresh"
    assert freshness["stale_files"] == []


def test_query_freshness_hides_identity_only_typescript_variant_lane(
    tmp_path: Path,
) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "dual.ts").write_text(
        "export const typed = 1;\n",
        encoding="utf-8",
    )
    runtime = src_dir / "dual.mjs"
    runtime.write_text("export const runtime = 1;\n", encoding="utf-8")
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=tmp_path,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    runtime_module = next(
        node
        for node in GraphStoreReader.from_current(output_dir).read_nodes()
        if node.kind == "module" and node.path == "src/dual.mjs"
    )

    assert "__arcgraph_variant_" in (runtime_module.qualname or "")
    assert runtime_module.name == "dual"
    assert runtime_module.properties["display_module"] == "dual"

    runtime.write_text("export const runtime = 2;\n", encoding="utf-8")
    freshness = QueryEngine(output_dir).current()["freshness"]

    assert freshness["status"] == "stale"
    assert freshness["stale_files"] == ["src/dual.mjs"]
    assert freshness["stale_modules"] == ["dual"]


def _copy_fixture(tmp_path: Path) -> Path:
    repo_root = tmp_path / "sample_project"
    shutil.copytree(FIXTURE_ROOT, repo_root)
    return repo_root


def test_sqlite_node_indexes_exist(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    _, build_dir = ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    with sqlite3.connect(build_dir / "index.sqlite") as conn:
        index_names = {row[1] for row in conn.execute("PRAGMA index_list('nodes')")}

    assert "idx_nodes_path" in index_names
    assert "idx_nodes_kind" in index_names
    assert "idx_nodes_name_kind_id" in index_names
    assert "idx_nodes_qualname_kind_id" in index_names
    assert "idx_nodes_canonical_identity" in index_names


def test_schema_0_6_platform_tables_and_artifacts_exist(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    _, build_dir = ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    assert (build_dir / "semantic_facts.jsonl").exists()
    assert (build_dir / "diagnostics.jsonl").exists()
    assert (build_dir / "merge_metrics.json").exists()

    with sqlite3.connect(build_dir / "index.sqlite") as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        node_columns = {row[1] for row in conn.execute("PRAGMA table_info('nodes')")}
        edge_columns = {row[1] for row in conn.execute("PRAGMA table_info('edges')")}
        merge_metrics_columns = {
            row[1] for row in conn.execute("PRAGMA table_info('merge_metrics')")
        }
        semantic_fact_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list('semantic_facts')")
        }
        edge_indexes = {row[1] for row in conn.execute("PRAGMA index_list('edges')")}
        semantic_fact_count = conn.execute(
            "SELECT COUNT(*) FROM semantic_facts"
        ).fetchone()[0]
        node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        merge_metrics_count = conn.execute(
            "SELECT COUNT(*) FROM merge_metrics"
        ).fetchone()[0]

    assert {
        "semantic_facts",
        "diagnostics",
        "merge_metrics",
        "schema_migrations",
    } <= tables
    assert "canonical_identity" in node_columns
    assert {
        "semantic_role",
        "resolution_json",
        "confidence_sources_json",
    } <= edge_columns
    assert {
        "repo_id",
        "index_version",
        "metrics_json",
        "created_at",
    } <= merge_metrics_columns
    assert "idx_semantic_facts_edge_merge" in semantic_fact_indexes
    assert "idx_edges_source_kind_role" in edge_indexes
    assert "idx_edges_target_kind_role" in edge_indexes
    # Structural nodes now go through the merge pipeline (pre-merge),
    # so every node and edge has a corresponding semantic fact.
    assert semantic_fact_count == node_count + edge_count
    assert merge_metrics_count == 1
    store_edges = GraphStoreReader.from_current(output_dir).read_edges()
    assert isinstance(store_edges[0].resolution, FactResolution)


def test_summary_reports_truncated_warning_and_diagnostic_counts(
    tmp_path: Path,
) -> None:
    metadata = IndexMetadata(
        index_version="test",
        repo_root=str(tmp_path),
        source_roots=["src"],
    )
    warnings = [
        BuildWarning(kind="parse_error", message=f"warning {index}")
        for index in range(201)
    ]
    diagnostics = [
        SemanticDiagnostic(
            diagnostic_id=f"diagnostic:{index}",
            index_version="test",
            diagnostic_kind="test",
            message=f"diagnostic {index}",
        )
        for index in range(202)
    ]

    build_dir = GraphStoreWriter(tmp_path / "arcgraph").write(
        metadata,
        files=[],
        nodes=[],
        edges=[],
        warnings=warnings,
        diagnostics=diagnostics,
        merge_metrics=MergeMetrics(index_version="test"),
    )
    summary = json.loads((build_dir / "summary.json").read_text(encoding="utf-8"))

    assert len(summary["warnings"]) == 200
    assert len(summary["diagnostics"]) == 200
    assert summary["truncated_count"] == 3
    assert summary["summary_truncation"] == {"warnings": 1, "diagnostics": 2}
    assert summary["merge_metrics"]["truncated_count"] == 3
    assert summary["diagnostic_lifecycle"]["new_diagnostics"] == 202


def test_diagnostic_lifecycle_marks_repeated_diagnostics_existing(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    _, second_build_dir = ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()

    diagnostics = GraphStoreReader.from_current(output_dir).read_diagnostics()
    summary = json.loads(
        (second_build_dir / "summary.json").read_text(encoding="utf-8")
    )

    assert diagnostics
    assert all(diagnostic.seen_count >= 2 for diagnostic in diagnostics)
    assert summary["diagnostic_lifecycle"]["existing_diagnostics"] == len(diagnostics)
    assert summary["diagnostic_lifecycle"]["new_diagnostics"] == 0


def test_impact_guardrails_truncate_high_fan_out(tmp_path: Path) -> None:
    """PathQueryLimits.max_edges caps BFS expansion and reports truncation."""
    output_dir = tmp_path / "arcgraph"
    target = Node(
        id="fn:pkg.service.save",
        kind="function",
        name="save",
        qualname="pkg.service.save",
        path="src/pkg/service.py",
        start_line=5,
    )
    # Create 10 callers → enough to exceed max_edges=3
    callers = [
        Node(
            id=f"fn:pkg.api.handler_{i}",
            kind="function",
            name=f"handler_{i}",
            qualname=f"pkg.api.handler_{i}",
            path="src/pkg/api.py",
            start_line=10 + i,
        )
        for i in range(10)
    ]
    edges = [
        Edge(source=caller.id, target=target.id, kind="calls") for caller in callers
    ]
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="guardrail-test",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[target, *callers],
        edges=edges,
        warnings=[],
    )
    engine = QueryEngine(output_dir)

    # With tight max_edges, traversal should truncate
    impact = engine.impact(
        target.id,
        path_limits=PathQueryLimits(max_edges=3, max_fan_in=200, timeout_seconds=10),
    )

    assert len(impact["call_impact"]["edges"]) <= 3
    truncation_warnings = [
        w for w in impact.get("warnings", []) if "truncated by guardrails" in w
    ]
    assert len(truncation_warnings) == 1
    assert "max_edges=3" in truncation_warnings[0]

    # Default limits should NOT truncate this small graph
    impact_full = engine.impact(target.id)
    full_warnings = [
        w for w in impact_full.get("warnings", []) if "truncated by guardrails" in w
    ]
    assert len(full_warnings) == 0
    assert len(impact_full["call_impact"]["edges"]) == 10


# ── Guardrail state-matrix tests ──────────────────────────────────
#
# The truncation invariant:
#   truncated=True IFF there is evidence that matching edges were NOT returned.
#
# Evidence sources:
#   1. sql_capped  (SQL helper hit per-target or global limits)
#   2. timeout     (wall-clock deadline exceeded)
#   3. batch_overflow (inner loop stopped before consuming all edges)
#   4. budget_full + next-depth exists (existence query)
#
# The 5 tests below cover the (limit × evidence) matrix.


def _build_guardrail_graph(
    output_dir: Path,
    root: Path,
    *,
    nodes: list[Node],
    edges: list[Edge],
) -> QueryEngine:
    """Helper: write a graph and return QueryEngine."""
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="guardrail-matrix",
            repo_root=str(root),
            source_roots=["src"],
        ),
        files=[],
        nodes=nodes,
        edges=edges,
        warnings=[],
    )
    return QueryEngine(output_dir)


def test_guardrail_exact_limit_no_next_depth_no_warning(
    tmp_path: Path,
) -> None:
    """Exact limit hit, no next-depth edges → no truncation warning.

    Graph: target ← 3 callers.  max_edges=3, max_depth=2.
    BFS finds exactly 3 edges at depth 1, no depth 2 edges exist.
    Result is complete; no warning should be emitted.
    """
    output_dir = tmp_path / "arcgraph"
    target = Node(
        id="fn:pkg.svc.handle",
        kind="function",
        name="handle",
        qualname="pkg.svc.handle",
        path="src/pkg/svc.py",
        start_line=1,
    )
    callers = [
        Node(
            id=f"fn:pkg.api.caller_{i}",
            kind="function",
            name=f"caller_{i}",
            qualname=f"pkg.api.caller_{i}",
            path="src/pkg/api.py",
            start_line=10 + i,
        )
        for i in range(3)
    ]
    edges = [Edge(source=c.id, target=target.id, kind="calls") for c in callers]
    engine = _build_guardrail_graph(
        output_dir,
        tmp_path,
        nodes=[target, *callers],
        edges=edges,
    )

    impact = engine.impact(
        target.id,
        path_limits=PathQueryLimits(
            max_edges=3,
            max_fan_in=200,
            timeout_seconds=10,
        ),
    )

    assert len(impact["call_impact"]["edges"]) == 3
    warnings = [w for w in impact.get("warnings", []) if "truncated" in w.lower()]
    assert len(warnings) == 0, f"False truncation warning: {warnings}"


def test_guardrail_exact_limit_has_next_depth_warns(
    tmp_path: Path,
) -> None:
    """Exact limit hit, next-depth edges exist → truncation warning.

    Graph: target ← 3 callers ← 3 upstream (1 per caller).
    max_edges=3, max_depth=2.  BFS fills budget at depth 1.
    Depth 2 has real unseen edges → must warn.
    """
    output_dir = tmp_path / "arcgraph"
    target = Node(
        id="fn:pkg.svc.handle",
        kind="function",
        name="handle",
        qualname="pkg.svc.handle",
        path="src/pkg/svc.py",
        start_line=1,
    )
    callers = [
        Node(
            id=f"fn:pkg.api.caller_{i}",
            kind="function",
            name=f"caller_{i}",
            qualname=f"pkg.api.caller_{i}",
            path="src/pkg/api.py",
            start_line=10 + i,
        )
        for i in range(3)
    ]
    upstreams = [
        Node(
            id=f"fn:pkg.entry.upstream_{i}",
            kind="function",
            name=f"upstream_{i}",
            qualname=f"pkg.entry.upstream_{i}",
            path="src/pkg/entry.py",
            start_line=20 + i,
        )
        for i in range(3)
    ]
    edges = [
        *[Edge(source=c.id, target=target.id, kind="calls") for c in callers],
        *[
            Edge(source=u.id, target=callers[i].id, kind="calls")
            for i, u in enumerate(upstreams)
        ],
    ]
    engine = _build_guardrail_graph(
        output_dir,
        tmp_path,
        nodes=[target, *callers, *upstreams],
        edges=edges,
    )

    impact = engine.impact(
        target.id,
        path_limits=PathQueryLimits(
            max_edges=3,
            max_fan_in=200,
            timeout_seconds=10,
        ),
    )

    assert len(impact["call_impact"]["edges"]) == 3
    warnings = [w for w in impact.get("warnings", []) if "truncated" in w.lower()]
    assert len(warnings) == 1, f"Expected truncation warning, got: {warnings}"


def test_guardrail_batch_overflow_warns(tmp_path: Path) -> None:
    """Current SQL batch has more edges than max_edges → truncation warning.

    Graph: target ← 4 callers.  max_edges=3.
    SQL returns 4 edges but only 3 are kept → must warn.
    """
    output_dir = tmp_path / "arcgraph"
    target = Node(
        id="fn:pkg.svc.handle",
        kind="function",
        name="handle",
        qualname="pkg.svc.handle",
        path="src/pkg/svc.py",
        start_line=1,
    )
    callers = [
        Node(
            id=f"fn:pkg.api.caller_{i}",
            kind="function",
            name=f"caller_{i}",
            qualname=f"pkg.api.caller_{i}",
            path="src/pkg/api.py",
            start_line=10 + i,
        )
        for i in range(4)
    ]
    edges = [Edge(source=c.id, target=target.id, kind="calls") for c in callers]
    engine = _build_guardrail_graph(
        output_dir,
        tmp_path,
        nodes=[target, *callers],
        edges=edges,
    )

    impact = engine.impact(
        target.id,
        path_limits=PathQueryLimits(
            max_edges=3,
            max_fan_in=200,
            timeout_seconds=10,
        ),
    )

    assert len(impact["call_impact"]["edges"]) <= 3
    warnings = [w for w in impact.get("warnings", []) if "truncated" in w.lower()]
    assert len(warnings) == 1


def test_guardrail_fan_in_sentinel_warns(tmp_path: Path) -> None:
    """Per-target fan-in sentinel triggers truncation warning.

    Graph: seed ← hub_a ← 3 callers, seed ← hub_b ← 3 callers.
    max_fan_in=2 per hub → sentinels detected, each hub keeps 2 callers.
    Must warn because callers were dropped.
    """
    output_dir = tmp_path / "arcgraph"
    seed = Node(
        id="fn:pkg.seed",
        kind="function",
        name="seed",
        qualname="pkg.seed",
        path="src/pkg/core.py",
        start_line=1,
    )
    hub_a = Node(
        id="fn:pkg.hub_a",
        kind="function",
        name="hub_a",
        qualname="pkg.hub_a",
        path="src/pkg/svc.py",
        start_line=1,
    )
    hub_b = Node(
        id="fn:pkg.hub_b",
        kind="function",
        name="hub_b",
        qualname="pkg.hub_b",
        path="src/pkg/svc.py",
        start_line=2,
    )
    callers_a = [
        Node(
            id=f"fn:pkg.ca{i}",
            kind="function",
            name=f"ca{i}",
            qualname=f"pkg.ca{i}",
            path="src/pkg/api.py",
            start_line=10 + i,
        )
        for i in range(3)
    ]
    callers_b = [
        Node(
            id=f"fn:pkg.cb{i}",
            kind="function",
            name=f"cb{i}",
            qualname=f"pkg.cb{i}",
            path="src/pkg/api.py",
            start_line=20 + i,
        )
        for i in range(3)
    ]
    edges = [
        Edge(source=hub_a.id, target=seed.id, kind="calls"),
        Edge(source=hub_b.id, target=seed.id, kind="calls"),
        *[Edge(source=c.id, target=hub_a.id, kind="calls") for c in callers_a],
        *[Edge(source=c.id, target=hub_b.id, kind="calls") for c in callers_b],
    ]
    engine = _build_guardrail_graph(
        output_dir,
        tmp_path,
        nodes=[seed, hub_a, hub_b, *callers_a, *callers_b],
        edges=edges,
    )

    impact = engine.impact(
        seed.id,
        path_limits=PathQueryLimits(
            max_edges=5000,
            max_fan_in=2,
            timeout_seconds=10,
        ),
    )

    call_edges = impact["call_impact"]["edges"]
    hub_a_callers = [e for e in call_edges if e["target"] == hub_a.id]
    hub_b_callers = [e for e in call_edges if e["target"] == hub_b.id]
    assert len(hub_a_callers) == 2
    assert len(hub_b_callers) == 2
    warnings = [w for w in impact.get("warnings", []) if "truncated" in w.lower()]
    assert len(warnings) == 1


def test_guardrail_max_depth_exhausted_no_false_warning(
    tmp_path: Path,
) -> None:
    """max_depth=1 with frontier non-empty → no warning because depth is done.

    Graph: target ← 3 callers ← 3 upstream.  max_depth=1.
    BFS finds 3 caller edges at depth 1 and stops.  Frontier has callers
    with upstream edges but max_depth is exhausted; the BFS completed its
    requested depth so no truncation evidence exists.
    """
    output_dir = tmp_path / "arcgraph"
    target = Node(
        id="fn:pkg.svc.handle",
        kind="function",
        name="handle",
        qualname="pkg.svc.handle",
        path="src/pkg/svc.py",
        start_line=1,
    )
    callers = [
        Node(
            id=f"fn:pkg.api.caller_{i}",
            kind="function",
            name=f"caller_{i}",
            qualname=f"pkg.api.caller_{i}",
            path="src/pkg/api.py",
            start_line=10 + i,
        )
        for i in range(3)
    ]
    upstreams = [
        Node(
            id=f"fn:pkg.entry.upstream_{i}",
            kind="function",
            name=f"upstream_{i}",
            qualname=f"pkg.entry.upstream_{i}",
            path="src/pkg/entry.py",
            start_line=20 + i,
        )
        for i in range(3)
    ]
    edges = [
        *[Edge(source=c.id, target=target.id, kind="calls") for c in callers],
        *[
            Edge(source=u.id, target=callers[i].id, kind="calls")
            for i, u in enumerate(upstreams)
        ],
    ]
    engine = _build_guardrail_graph(
        output_dir,
        tmp_path,
        nodes=[target, *callers, *upstreams],
        edges=edges,
    )

    impact = engine.impact(
        target.id,
        max_depth=1,
        path_limits=PathQueryLimits(
            max_edges=5000,
            max_fan_in=200,
            timeout_seconds=10,
        ),
    )

    # Should find exactly 3 edges (depth 1 only)
    assert len(impact["call_impact"]["edges"]) == 3
    warnings = [w for w in impact.get("warnings", []) if "truncated" in w.lower()]
    assert len(warnings) == 0, f"False truncation warning: {warnings}"


def test_strict_static_excludes_heuristic_bridge_edges(
    tmp_path: Path,
) -> None:
    """strict_static must not traverse heuristic edges in BFS.

    Graph: target <-heuristic- bridge <-confirmed- upstream
    Under strict_static only confirmed edges are allowed.
    Because the only path from target to upstream passes through a
    heuristic edge, strict_static should NOT include the confirmed
    bridge->upstream edge in the result.  Under review_default both
    edges should appear.
    """
    output_dir = tmp_path / "arcgraph"
    target = Node(
        id="fn:pkg.svc.handle",
        kind="function",
        name="handle",
        qualname="pkg.svc.handle",
        path="src/pkg/svc.py",
        start_line=1,
    )
    bridge = Node(
        id="fn:pkg.svc.bridge",
        kind="function",
        name="bridge",
        qualname="pkg.svc.bridge",
        path="src/pkg/svc.py",
        start_line=10,
    )
    upstream = Node(
        id="fn:pkg.svc.upstream",
        kind="function",
        name="upstream",
        qualname="pkg.svc.upstream",
        path="src/pkg/svc.py",
        start_line=20,
    )
    edges = [
        Edge(
            source=bridge.id,
            target=target.id,
            kind="calls",
            confidence="heuristic",
        ),
        Edge(
            source=upstream.id,
            target=bridge.id,
            kind="calls",
            confidence="confirmed",
        ),
    ]
    engine = _build_guardrail_graph(
        output_dir,
        tmp_path,
        nodes=[target, bridge, upstream],
        edges=edges,
    )

    # strict_static: should NOT return any edges because the only
    # edge touching target is heuristic.
    strict_impact = engine.impact(
        target.id,
        profile="strict_static",
        max_depth=5,
    )
    strict_call_edges = strict_impact["call_impact"]["edges"]
    strict_sources = {e["source"] for e in strict_call_edges}
    assert (
        bridge.id not in strict_sources
    ), "strict_static should not traverse heuristic edge to bridge"
    assert (
        upstream.id not in strict_sources
    ), "strict_static should not include upstream reachable only via heuristic bridge"
    assert len(strict_call_edges) == 0

    # review_default: should return both edges
    review_impact = engine.impact(
        target.id,
        profile="review_default",
        max_depth=5,
    )
    review_call_edges = review_impact["call_impact"]["edges"]
    review_sources = {e["source"] for e in review_call_edges}
    assert bridge.id in review_sources
    assert upstream.id in review_sources
    assert len(review_call_edges) == 2


def test_bare_package_name_resolves_indexed_external_package(tmp_path: Path) -> None:
    """A dependency name must resolve to its ext: node, as it did before the
    shared TargetResolver replaced the qualname fallback."""

    output_dir = tmp_path / "arcgraph"
    package = Node(
        id="ext:fastapi",
        kind="external_package",
        name="fastapi",
        qualname="fastapi",
    )
    consumer = Node(
        id="fn:pkg.api.handle",
        kind="function",
        name="handle",
        qualname="pkg.api.handle",
        path="src/pkg/api.py",
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="external-package-resolution",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[package, consumer],
        edges=[Edge(source=consumer.id, target=package.id, kind="imports")],
        warnings=[],
    )
    engine = QueryEngine(output_dir)

    impact = engine.impact("fastapi")
    assert impact["resolved_targets"] == [package.id]
    assert impact["target_resolution"]["status"] == "resolved"

    suggestions = engine.impact("fastap")["target_resolution"]["suggestions"]
    assert package.id in {item.get("id") for item in suggestions}


def test_unresolved_bare_ts_filename_is_path_scoped(tmp_path: Path) -> None:
    """A bare TS filename that matches no nodes must scope the diagnostic
    filter to that path instead of matching the entire index."""

    output_dir = tmp_path / "arcgraph"
    function = Node(
        id="fn:pkg.service.run",
        kind="function",
        name="run",
        qualname="pkg.service.run",
        path="src/pkg/service.py",
        start_line=5,
    )
    diagnostic = SemanticDiagnostic(
        diagnostic_id="diagnostic:unrelated",
        index_version="ts-path-scope",
        diagnostic_kind="unresolved_callsite",
        message="Unresolved call",
        severity="info",
        path="src/pkg/service.py",
        start_line=6,
        frontend_name="python-v1-compat-shim",
        properties={"source_scope": function.id},
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="ts-path-scope",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[function],
        edges=[],
        warnings=[],
        diagnostics=[diagnostic],
    )
    engine = QueryEngine(output_dir)

    for query in ("index.ts", "missing.py"):
        payload = engine.unresolved(query)
        assert payload["summary"]["total"] == 0, query


def test_ci_similarity_expansion_keeps_ambiguous_candidates(tmp_path: Path) -> None:
    """An ambiguous similarity target expands to every bounded candidate
    instead of silently expanding to nothing."""

    from arcgraph.interfaces.ci import _expand_similarity_targets

    output_dir = tmp_path / "arcgraph"
    first = Node(
        id="fn:pkg.alpha.shared",
        kind="function",
        name="shared",
        qualname="pkg.alpha.shared",
        path="src/pkg/alpha.py",
    )
    second = Node(
        id="fn:pkg.beta.shared",
        kind="function",
        name="shared",
        qualname="pkg.beta.shared",
        path="src/pkg/beta.py",
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="ambiguous-similarity",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[first, second],
        edges=[],
        warnings=[],
    )
    engine = QueryEngine(output_dir)

    expanded = _expand_similarity_targets(engine, ["shared"], max_results=10)
    assert set(expanded) == {first.id, second.id}


def test_merged_multifact_edge_yields_one_callsite_record_per_fact(
    tmp_path: Path,
) -> None:
    """A merged edge carrying N callsite facts must surface N records, and
    the edge-level resolution id may only be bound to a single-fact edge."""

    from arcgraph.core.schemas import FactResolution

    output_dir = tmp_path / "arcgraph"
    caller = Node(
        id="fn:app.caller",
        kind="function",
        name="caller",
        qualname="app.caller",
        path="src/app.ts",
        start_line=1,
    )
    callee = Node(
        id="fn:lib.callee",
        kind="function",
        name="callee",
        qualname="lib.callee",
        path="src/lib.ts",
        start_line=1,
    )
    edge = Edge(
        source=caller.id,
        target=callee.id,
        kind="calls",
        resolution=FactResolution(callsite_id="cs:promoted"),
        properties={
            "callsites": [
                {
                    "path": "src/app.ts",
                    "line": 3,
                    "column": 2,
                    "raw_expression": "callee(1)",
                },
                {
                    "path": "src/app.ts",
                    "line": 9,
                    "column": 4,
                    "raw_expression": "callee(2)",
                },
            ]
        },
    )
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="multifact-callsites",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=[caller, callee],
        edges=[edge],
        warnings=[],
    )

    payload = QueryEngine(output_dir).callsites("app.caller")
    records = payload["callsites"]
    assert len(records) == 2
    assert len({record["callsite_id"] for record in records}) == 2
    assert [(record["line"], record["raw_expression"]) for record in records] == [
        (3, "callee(1)"),
        (9, "callee(2)"),
    ]


def test_repository_root_typescript_paths_and_dot_prefix_resolve(
    tmp_path: Path,
) -> None:
    """Every indexable source suffix is a path even without a separator, and
    a ./-prefixed target reduces to the repository-relative key the index
    stores."""

    output_dir = tmp_path / "arcgraph"
    nodes = [
        Node(
            id="mod:entry",
            kind="module",
            name="entry",
            qualname="entry",
            path="entry.mts",
        ),
        Node(
            id="mod:root",
            kind="module",
            name="root",
            qualname="root",
            path="root.cjs",
        ),
        Node(
            id="mod:src.service",
            kind="module",
            name="service",
            qualname="src.service",
            path="src/service.ts",
        ),
    ]
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="root-path-resolution",
            repo_root=str(tmp_path),
            source_roots=["."],
        ),
        files=[],
        nodes=nodes,
        edges=[],
        warnings=[],
    )
    engine = QueryEngine(output_dir)

    for query, expected in (
        ("entry.mts", "mod:entry"),
        ("./entry.mts", "mod:entry"),
        ("root.cjs", "mod:root"),
        ("./root.cjs", "mod:root"),
        ("src/service.ts", "mod:src.service"),
        ("./src/service.ts", "mod:src.service"),
        (f"{tmp_path}/src/service.ts", "mod:src.service"),
    ):
        payload = engine.impact(query)
        assert payload["resolved_targets"] == [expected], query


def test_path_suffixes_come_from_the_index_not_a_static_table(
    tmp_path: Path,
) -> None:
    """Frontends and SCIP ingestion contribute their own extensions, so the
    path-shaped test reads the index instead of a table that cannot keep up.
    A bare word still must not become a path: directory-shaped node paths
    would otherwise resolve an ambiguous name to one candidate."""

    output_dir = tmp_path / "arcgraph"
    nodes = [
        Node(
            id="mod:widget",
            kind="module",
            name="Widget",
            qualname="Widget",
            path="Widget.vue",
        ),
        Node(
            id="doc:scip:main.go",
            kind="module",
            name="main.go",
            qualname="main.go",
            path="main.go",
        ),
        # Structural source-root nodes carry a directory path and no
        # qualname, exactly as arcgraph/core/structural.py builds them.
        Node(
            id="source_root:lib",
            kind="source_root",
            name="lib",
            path="lib",
        ),
        Node(
            id="fn:mod.lib",
            kind="function",
            name="lib",
            qualname="mod.lib",
            path="lib/mod.py",
        ),
    ]
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="indexed-suffixes",
            repo_root=str(tmp_path),
            source_roots=["."],
        ),
        files=[],
        nodes=nodes,
        edges=[],
        warnings=[],
    )
    engine = QueryEngine(output_dir)

    # A suffix the static table never listed still resolves at the root.
    assert engine.impact("Widget.vue")["resolved_targets"] == ["mod:widget"]

    # A bare word that matches a directory-shaped node path stays ambiguous:
    # resolution never picks one candidate out of an ambiguous set.
    resolution = engine.impact("lib")["target_resolution"]
    assert resolution["status"] == "ambiguous"
    assert {item["id"] for item in resolution["candidates"]} == {
        "source_root:lib",
        "fn:mod.lib",
    }


def test_uppercase_suffix_targets_are_path_shaped(tmp_path: Path) -> None:
    """Indexed suffixes are cached lowercased, so the shape test compares the
    target the same way; the path lookup itself stays exact."""

    output_dir = tmp_path / "arcgraph"
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="uppercase-suffix",
            repo_root=str(tmp_path),
            source_roots=["."],
        ),
        files=[],
        nodes=[
            Node(
                id="mod:widget",
                kind="module",
                name="Widget",
                qualname="Widget",
                path="Widget.VUE",
            )
        ],
        edges=[],
        warnings=[],
    )

    assert QueryEngine(output_dir).impact("Widget.VUE")["resolved_targets"] == [
        "mod:widget"
    ]


def test_edge_reads_batch_past_the_real_sqlite_variable_limit(
    tmp_path: Path,
) -> None:
    """A file target resolves to every symbol the file declares, so an id
    list is bounded only by file size. The boundary is the connection's own
    SQLITE_LIMIT_VARIABLE_NUMBER, so the test lowers that limit rather than
    materializing tens of thousands of nodes — otherwise it asserts a
    boundary it never reaches."""

    import sqlite3 as sqlite3_module

    from arcgraph.core.query_engine import SQLITE_IN_BATCH_SIZE

    output_dir = tmp_path / "arcgraph"
    count = SQLITE_IN_BATCH_SIZE + 250
    nodes = [
        Node(
            id=f"fn:huge.s{index}",
            kind="function",
            name=f"s{index}",
            qualname=f"huge.s{index}",
            path="src/huge.py",
        )
        for index in range(count)
    ]
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="wide-id-list",
            repo_root=str(tmp_path),
            source_roots=["src"],
        ),
        files=[],
        nodes=nodes,
        edges=[Edge(source="fn:huge.s0", target="fn:huge.s1", kind="calls")],
        warnings=[],
    )
    engine = QueryEngine(output_dir)
    target_ids = [f"fn:huge.s{index}" for index in range(count)]

    with engine.store.connect() as conn:
        # Bind the real ceiling just above one batch, so a helper that does
        # not batch raises here exactly as it would at the default ceiling
        # with a large enough file.
        conn.setlimit(
            sqlite3_module.SQLITE_LIMIT_VARIABLE_NUMBER, SQLITE_IN_BATCH_SIZE + 8
        )
        assert engine._edges_for_targets_kinds(conn, target_ids, ["calls"]) is not None
        assert engine._edges_for_sources_kinds(conn, target_ids, ["calls"]) is not None
        capped_edges, _capped = engine._edges_for_targets_kinds_limited(
            conn,
            target_ids,
            ["calls"],
            per_target_limit=5,
            global_limit=50,
        )
        assert capped_edges is not None
        assert engine._has_unseen_edges(conn, target_ids, ["calls"], {}) is not None

    # The public surfaces that bind these lists still answer.
    assert engine.impact("src/huge.py")["status"] == "available"
    assert engine.callers("src/huge.py")["status"] in {"available", "partial"}
