from __future__ import annotations

from pathlib import Path

from arcgraph.core.graph_store import GraphStoreWriter
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.schemas import (
    Edge,
    FileRecord,
    IndexMetadata,
    Node,
    SemanticDiagnostic,
)
from arcgraph.core.visual_slice_engine import (
    VisualSliceEngine,
    VisualSliceLimits,
    make_visual_slice,
)


def _write_store(
    output_dir: Path,
    *,
    files: list[FileRecord] | None = None,
    nodes: list[Node],
    edges: list[Edge],
    diagnostics: list[SemanticDiagnostic] | None = None,
) -> None:
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="visual-slice-test",
            repo_root=str(output_dir.parent),
            source_roots=["src", "tests"],
        ),
        files=files or [],
        nodes=nodes,
        edges=edges,
        warnings=[],
        diagnostics=diagnostics,
    )


def test_resource_flow_uses_impact_resource_edges(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    target = Node(
        id="fn:pkg.service.write_memory",
        kind="function",
        name="write_memory",
        qualname="pkg.service.write_memory",
        path="src/pkg/service.py",
        start_line=10,
    )
    table = Node(
        id="table:memories",
        kind="table",
        name="memories",
        qualname="memories",
    )
    queue = Node(
        id="queue:embedding",
        kind="queue",
        name="embedding",
        qualname="embedding",
    )
    config = Node(
        id="config:settings",
        kind="config",
        name="settings",
        qualname="settings",
    )
    _write_store(
        output_dir,
        files=[
            FileRecord(
                path="src/pkg/service.py",
                abs_path=str(tmp_path / "src/pkg/service.py"),
                source_root="src",
                module="pkg.service",
                file_hash="hash",
                line_count=20,
            )
        ],
        nodes=[target, table, queue, config],
        edges=[
            Edge(source=target.id, target=table.id, kind="writes"),
            Edge(source=target.id, target=queue.id, kind="enqueues"),
            Edge(source=target.id, target=config.id, kind="configures"),
        ],
    )

    visual_slice = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "resource_flow",
        target=target.id,
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )

    assert visual_slice["view"] == "resource_flow"
    assert visual_slice["status"] == "available"
    assert visual_slice["scope"]["repo_id"] == "default"
    assert visual_slice["scope"]["index_version"] == "visual-slice-test"
    assert all("label" not in action for action in visual_slice["actions"])
    assert {node["id"] for node in visual_slice["nodes"]} == {
        target.id,
        table.id,
        queue.id,
        config.id,
    }
    assert {edge["kind"] for edge in visual_slice["edges"]} == {
        "writes",
        "enqueues",
        "configures",
    }
    assert "configures" in visual_slice["summary"]["edge_kinds"]
    assert all("group_id" in node for node in visual_slice["nodes"])


def test_resource_flow_respects_edge_kind_and_runtime_filters(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    target = Node(
        id="fn:pkg.service.process",
        kind="function",
        name="process",
        qualname="pkg.service.process",
        path="src/pkg/service.py",
        start_line=5,
    )
    table = Node(id="table:data", kind="table", name="data", qualname="data")
    config = Node(id="config:env", kind="config", name="env", qualname="env")
    _write_store(
        output_dir,
        files=[
            FileRecord(
                path="src/pkg/service.py",
                abs_path=str(tmp_path / "src/pkg/service.py"),
                source_root="src",
                module="pkg.service",
                file_hash="hash",
                line_count=20,
            )
        ],
        nodes=[target, table, config],
        edges=[
            Edge(source=target.id, target=table.id, kind="reads"),
            Edge(source=target.id, target=config.id, kind="configures"),
            Edge(
                source=target.id,
                target=table.id,
                kind="writes",
                confidence="runtime-only",
            ),
        ],
    )
    engine = VisualSliceEngine(QueryEngine(output_dir))

    # include_edge_kinds narrows to intersection with RESOURCE_EDGE_KINDS
    slice_filtered = engine.build_slice(
        "resource_flow",
        target=target.id,
        include_edge_kinds=["reads"],
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )
    assert len(slice_filtered["edges"]) > 0
    assert {e["kind"] for e in slice_filtered["edges"]} <= {"reads"}

    # include_runtime=False drops runtime-only edges
    slice_no_runtime = engine.build_slice(
        "resource_flow",
        target=target.id,
        include_runtime=False,
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )
    assert all(e.get("confidence") != "runtime-only" for e in slice_no_runtime["edges"])

    slice_with_runtime = engine.build_slice(
        "resource_flow",
        target=target.id,
        include_runtime=True,
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )
    assert any(
        e.get("confidence") == "runtime-only" for e in slice_with_runtime["edges"]
    )
    assert "writes" in slice_with_runtime["summary"]["edge_kinds"]

    # non-overlapping filter returns empty, not fallback to all
    slice_empty = engine.build_slice(
        "resource_flow",
        target=target.id,
        include_edge_kinds=["calls"],
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )
    assert slice_empty["edges"] == []
    assert slice_empty["summary"]["edge_kinds"] == []
    assert slice_empty["summary"]["resource_count"] == 0


def test_resource_flow_respects_exclude_edge_kind_filter(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    target = Node(
        id="fn:pkg.service.process",
        kind="function",
        name="process",
        qualname="pkg.service.process",
        path="src/pkg/service.py",
        start_line=5,
    )
    table = Node(id="table:data", kind="table", name="data", qualname="data")
    config = Node(id="config:env", kind="config", name="env", qualname="env")
    _write_store(
        output_dir,
        nodes=[target, table, config],
        edges=[
            Edge(source=target.id, target=table.id, kind="reads"),
            Edge(source=target.id, target=config.id, kind="configures"),
        ],
    )

    visual_slice = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "resource_flow",
        target=target.id,
        exclude_edge_kinds=["configures"],
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )

    assert {edge["kind"] for edge in visual_slice["edges"]} == {"reads"}
    assert "configures" not in visual_slice["summary"]["edge_kinds"]


def test_visual_slice_engine_returns_unavailable_for_unsupported_view(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    _write_store(output_dir, nodes=[], edges=[])

    visual_slice = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "unknown_view",
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )

    assert visual_slice["status"] == "unavailable"
    assert visual_slice["view"] == "unknown_view"
    assert visual_slice["diagnostics"][0]["severity"] == "error"
    assert "Unsupported VisualSlice view" in visual_slice["diagnostics"][0]["message"]


def test_target_required_views_return_unavailable_without_target(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    _write_store(output_dir, nodes=[], edges=[])

    visual_slice = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "impact_radius",
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )

    assert visual_slice["status"] == "unavailable"
    assert visual_slice["nodes"] == []
    assert "requires a target" in visual_slice["diagnostics"][0]["message"]


def test_system_map_degrades_to_source_root_kind_groups(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    module = Node(
        id="mod:pkg.service",
        kind="module",
        name="pkg.service",
        qualname="pkg.service",
        path="src/pkg/service.py",
    )
    function = Node(
        id="fn:pkg.service.run",
        kind="function",
        name="run",
        qualname="pkg.service.run",
        path="src/pkg/service.py",
    )
    test = Node(
        id="test:tests.test_service.test_run",
        kind="test_case",
        name="test_run",
        qualname="tests.test_service.test_run",
        path="tests/test_service.py",
    )
    _write_store(
        output_dir,
        files=[
            FileRecord(
                path="src/pkg/service.py",
                abs_path=str(tmp_path / "src/pkg/service.py"),
                source_root="src",
                module="pkg.service",
                file_hash="hash1",
                line_count=20,
            ),
            FileRecord(
                path="tests/test_service.py",
                abs_path=str(tmp_path / "tests/test_service.py"),
                source_root="tests",
                module="tests.test_service",
                file_hash="hash2",
                line_count=20,
            ),
        ],
        nodes=[module, function, test],
        edges=[Edge(source=test.id, target=function.id, kind="covers")],
    )

    visual_slice = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "system_map",
        limits=VisualSliceLimits(max_nodes=20, max_edges=20),
    )

    assert visual_slice["view"] == "system_map"
    assert visual_slice["scope"] == {
        "repo_id": "default",
        "index_version": "visual-slice-test",
        "mode": "source_root_kind_aggregate",
    }
    assert visual_slice["summary"]["degraded"].startswith("package/domain map")
    assert "source_root:src" in {node["id"] for node in visual_slice["nodes"]}
    assert "node_kind:function" in {node["id"] for node in visual_slice["nodes"]}
    assert ("source_root:src", "node_kind:function") in {
        (edge["source"], edge["target"]) for edge in visual_slice["edges"]
    }


def test_unresolved_risk_map_links_diagnostics_to_source_scope(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    source = Node(
        id="fn:pkg.api.handler",
        kind="function",
        name="handler",
        qualname="pkg.api.handler",
        path="src/pkg/api.py",
        start_line=5,
    )
    diagnostic = SemanticDiagnostic(
        diagnostic_id="diagnostic:dynamic-call",
        index_version="visual-slice-test",
        diagnostic_kind="unresolved_callsite",
        message="Unresolved Python callsite 'service.run'",
        severity="info",
        path="src/pkg/api.py",
        start_line=8,
        frontend_name="python-v1-compat-shim",
        properties={
            "source_scope": source.id,
            "raw_expression": "service.run",
            "failed_strategy": "dynamic_dispatch",
        },
    )
    _write_store(
        output_dir,
        files=[
            FileRecord(
                path="src/pkg/api.py",
                abs_path=str(tmp_path / "src/pkg/api.py"),
                source_root="src",
                module="pkg.api",
                file_hash="hash",
                line_count=20,
            )
        ],
        nodes=[source],
        edges=[],
        diagnostics=[diagnostic],
    )

    visual_slice = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "unresolved_risk_map",
        limits=VisualSliceLimits(max_nodes=10, max_edges=10, max_diagnostics=10),
    )

    assert {node["id"] for node in visual_slice["nodes"]} == {
        source.id,
        diagnostic.diagnostic_id,
    }
    assert visual_slice["edges"] == [
        {
            "source": source.id,
            "target": diagnostic.diagnostic_id,
            "kind": "dynamic_call",
            "confidence": "unresolved",
            "semantic_role": "unresolved_callsite",
            "evidence_summary": "Unresolved Python callsite 'service.run'",
            "properties": {},
        }
    ]
    assert all(
        edge["kind"] != "has_unresolved_callsite" for edge in visual_slice["edges"]
    )
    assert visual_slice["diagnostics"][0]["diagnostic_id"] == diagnostic.diagnostic_id


def test_entrypoint_circuit_builds_from_entrypoint_flow(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    route = Node(
        id="route:GET:/api/v1/items",
        kind="route",
        name="GET /api/v1/items",
        qualname="GET /api/v1/items",
    )
    handler = Node(
        id="fn:pkg.api.list_items",
        kind="function",
        name="list_items",
        qualname="pkg.api.list_items",
        path="src/pkg/api.py",
        start_line=5,
    )
    table = Node(
        id="table:items",
        kind="table",
        name="items",
        qualname="items",
    )
    _write_store(
        output_dir,
        nodes=[route, handler, table],
        edges=[
            Edge(source=route.id, target=handler.id, kind="invokes"),
            Edge(source=handler.id, target=table.id, kind="reads"),
        ],
    )

    visual_slice = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "entrypoint_circuit",
        target="GET /api/v1/items",
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )

    assert visual_slice["status"] == "available"
    assert visual_slice["view"] == "entrypoint_circuit"
    assert {node["id"] for node in visual_slice["nodes"]} == {
        route.id,
        handler.id,
        table.id,
    }
    assert {edge["kind"] for edge in visual_slice["edges"]} == {"invokes", "reads"}


def test_impact_radius_includes_call_entrypoint_and_resource_edges(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    target = Node(
        id="fn:pkg.service.save_item",
        kind="function",
        name="save_item",
        qualname="pkg.service.save_item",
        path="src/pkg/service.py",
        start_line=12,
    )
    caller = Node(
        id="fn:pkg.api.create_item",
        kind="function",
        name="create_item",
        qualname="pkg.api.create_item",
        path="src/pkg/api.py",
        start_line=7,
    )
    route = Node(
        id="route:POST:/api/v1/items",
        kind="route",
        name="POST /api/v1/items",
        qualname="POST /api/v1/items",
    )
    table = Node(
        id="table:items",
        kind="table",
        name="items",
        qualname="items",
    )
    _write_store(
        output_dir,
        nodes=[target, caller, route, table],
        edges=[
            Edge(source=caller.id, target=target.id, kind="calls"),
            Edge(source=route.id, target=caller.id, kind="invokes"),
            Edge(source=target.id, target=table.id, kind="writes"),
        ],
    )

    visual_slice = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "impact_radius",
        target=target.id,
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )

    assert visual_slice["status"] == "available"
    assert visual_slice["view"] == "impact_radius"
    assert {node["id"] for node in visual_slice["nodes"]} == {
        target.id,
        caller.id,
        route.id,
        table.id,
    }
    assert {edge["kind"] for edge in visual_slice["edges"]} == {
        "calls",
        "invokes",
        "writes",
    }


def test_visual_slice_engine_batches_large_node_lookup(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    nodes = [
        Node(
            id=f"fn:pkg.module.func_{index:04d}",
            kind="function",
            name=f"func_{index:04d}",
            qualname=f"pkg.module.func_{index:04d}",
        )
        for index in range(1005)
    ]
    _write_store(output_dir, nodes=nodes, edges=[])

    found = VisualSliceEngine(QueryEngine(output_dir))._nodes_by_ids(
        [node.id for node in nodes]
    )

    assert len(found) == len(nodes)
    assert found[0]["id"] == "fn:pkg.module.func_0000"
    assert found[-1]["id"] == "fn:pkg.module.func_1004"


def test_make_visual_slice_drops_invalid_edges_and_reports_truncation() -> None:
    visual_slice = make_visual_slice(
        [
            {"id": "node:a", "kind": "function", "name": "a"},
            {"id": "node:b", "kind": "function", "name": "b"},
            {"id": "node:c", "kind": "function", "name": "c"},
        ],
        [
            {"source": "node:a", "target": "node:b", "kind": "calls"},
            {"source": "node:a", "target": "node:b", "kind": "calls"},
            {"source": "node:b", "target": "node:c", "kind": "calls"},
            {"source": "node:c", "target": None, "kind": "calls"},
        ],
        view="custom",
        diagnostics=[{"message": "hidden"}],
        limits=VisualSliceLimits(max_nodes=2, max_edges=1, max_diagnostics=0),
    )

    assert [edge["target"] for edge in visual_slice["edges"]] == ["node:b"]
    assert visual_slice["truncation"] == {
        "truncated": True,
        "nodes_dropped": 1,
        "edges_dropped": 1,
        "diagnostics_dropped": 1,
        "limits": {"max_nodes": 2, "max_edges": 1, "max_diagnostics": 0},
    }


def test_legacy_visual_slice_scope_has_contract_defaults() -> None:
    visual_slice = make_visual_slice([], [], view="custom")

    assert visual_slice["scope"] == {
        "repo_id": "unknown",
        "index_version": "unknown",
    }


def test_module_map_shows_import_graph(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    mod_a = Node(
        id="mod:pkg.api",
        kind="module",
        name="pkg.api",
        qualname="pkg.api",
        path="src/pkg/api.py",
    )
    mod_b = Node(
        id="mod:pkg.service",
        kind="module",
        name="pkg.service",
        qualname="pkg.service",
        path="src/pkg/service.py",
    )
    mod_c = Node(
        id="mod:pkg.repo",
        kind="module",
        name="pkg.repo",
        qualname="pkg.repo",
        path="src/pkg/repo.py",
    )
    _write_store(
        output_dir,
        nodes=[mod_a, mod_b, mod_c],
        edges=[
            Edge(source=mod_a.id, target=mod_b.id, kind="imports"),
            Edge(source=mod_b.id, target=mod_c.id, kind="imports"),
        ],
    )

    visual_slice = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "module_map",
        target="pkg.service",
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )

    assert visual_slice["view"] == "module_map"
    assert visual_slice["status"] == "available"
    node_ids = {node["id"] for node in visual_slice["nodes"]}
    assert mod_b.id in node_ids
    assert visual_slice["summary"]["target_module"] == mod_b.id
    assert visual_slice["summary"]["outgoing_count"] >= 1
    assert visual_slice["summary"]["incoming_count"] >= 1


def test_symbol_map_shows_callers_and_callees(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    target = Node(
        id="fn:pkg.service.save",
        kind="function",
        name="save",
        qualname="pkg.service.save",
        path="src/pkg/service.py",
        start_line=10,
    )
    caller = Node(
        id="fn:pkg.api.handle",
        kind="function",
        name="handle",
        qualname="pkg.api.handle",
        path="src/pkg/api.py",
        start_line=5,
    )
    callee = Node(
        id="fn:pkg.repo.store",
        kind="function",
        name="store",
        qualname="pkg.repo.store",
        path="src/pkg/repo.py",
        start_line=3,
    )
    _write_store(
        output_dir,
        nodes=[target, caller, callee],
        edges=[
            Edge(source=caller.id, target=target.id, kind="calls"),
            Edge(source=target.id, target=callee.id, kind="calls"),
        ],
    )

    visual_slice = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "symbol_map",
        target=target.id,
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )

    assert visual_slice["view"] == "symbol_map"
    assert visual_slice["status"] == "available"
    node_ids = {node["id"] for node in visual_slice["nodes"]}
    assert target.id in node_ids
    assert caller.id in node_ids
    assert callee.id in node_ids
    assert visual_slice["summary"]["caller_count"] >= 1
    assert visual_slice["summary"]["callee_count"] >= 1


def test_similarity_map_shows_similar_symbols(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    target = Node(
        id="fn:pkg.service.normalize_title",
        kind="function",
        name="normalize_title",
        qualname="pkg.service.normalize_title",
        path="src/pkg/service.py",
        start_line=5,
    )
    similar_fn = Node(
        id="fn:pkg.service.normalize_label",
        kind="function",
        name="normalize_label",
        qualname="pkg.service.normalize_label",
        path="src/pkg/service.py",
        start_line=15,
    )
    _write_store(
        output_dir,
        nodes=[target, similar_fn],
        edges=[
            Edge(
                source=target.id,
                target=similar_fn.id,
                kind="similar_to",
                confidence="heuristic",
            ),
        ],
    )

    visual_slice = VisualSliceEngine(QueryEngine(output_dir)).build_slice(
        "similarity_map",
        target=target.id,
        limits=VisualSliceLimits(max_nodes=10, max_edges=10),
    )

    assert visual_slice["view"] == "similarity_map"
    assert visual_slice["status"] == "available"
    node_ids = {node["id"] for node in visual_slice["nodes"]}
    assert target.id in node_ids
    assert similar_fn.id in node_ids
    assert visual_slice["summary"]["similar_count"] >= 1


def test_new_views_require_target(tmp_path: Path) -> None:
    """module_map, symbol_map, and similarity_map all require a target."""
    output_dir = tmp_path / "arcgraph"
    _write_store(output_dir, nodes=[], edges=[])
    engine = VisualSliceEngine(QueryEngine(output_dir))

    for view in ("module_map", "symbol_map", "similarity_map"):
        visual_slice = engine.build_slice(
            view,
            limits=VisualSliceLimits(max_nodes=10, max_edges=10),
        )
        assert visual_slice["status"] == "unavailable", f"{view} should require target"
        assert "requires a target" in visual_slice["diagnostics"][0]["message"]
