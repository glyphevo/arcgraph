from __future__ import annotations

from pathlib import Path

from arcgraph.core.force_graph_export import (
    ForceGraphExportOptions,
    build_force_graph_export,
)
from arcgraph.core.graph_store import GraphStoreWriter
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.schemas import Edge, FileRecord, IndexMetadata, Node


def _write_store(
    output_dir: Path,
    *,
    files: list[FileRecord],
    nodes: list[Node],
    edges: list[Edge],
) -> None:
    GraphStoreWriter(output_dir).write(
        IndexMetadata(
            index_version="force-graph-test",
            repo_root=str(output_dir.parent),
            source_roots=["src", "tests", "htmlcov"],
        ),
        files=files,
        nodes=nodes,
        edges=edges,
        warnings=[],
    )


def test_force_graph_export_defaults_to_production_semantic_graph(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    api_module = Node(
        id="mod:pkg.api",
        kind="module",
        name="api",
        qualname="pkg.api",
        path="src/pkg/api.py",
    )
    service_module = Node(
        id="mod:pkg.service",
        kind="module",
        name="service",
        qualname="pkg.service",
        path="src/pkg/service.py",
    )
    test_module = Node(
        id="mod:tests.test_service",
        kind="module",
        name="test_service",
        qualname="tests.test_service",
        path="tests/test_service.py",
    )
    htmlcov_module = Node(
        id="mod:htmlcov.coverage_html_cb",
        kind="module",
        name="coverage_html_cb",
        qualname="htmlcov.coverage_html_cb",
        path="htmlcov/coverage_html_cb.js",
    )
    handler = Node(
        id="fn:pkg.api.handler",
        kind="function",
        name="handler",
        qualname="pkg.api.handler",
        path="src/pkg/api.py",
    )
    run = Node(
        id="fn:pkg.service.run",
        kind="function",
        name="run",
        qualname="pkg.service.run",
        path="src/pkg/service.py",
    )
    test_run = Node(
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
                path="src/pkg/api.py",
                abs_path=str(tmp_path / "src/pkg/api.py"),
                source_root="src",
                module="pkg.api",
                file_hash="api",
                line_count=10,
            ),
            FileRecord(
                path="src/pkg/service.py",
                abs_path=str(tmp_path / "src/pkg/service.py"),
                source_root="src",
                module="pkg.service",
                file_hash="service",
                line_count=20,
            ),
            FileRecord(
                path="tests/test_service.py",
                abs_path=str(tmp_path / "tests/test_service.py"),
                source_root="tests",
                module="tests.test_service",
                file_hash="test",
                line_count=30,
            ),
            FileRecord(
                path="htmlcov/coverage_html_cb.js",
                abs_path=str(tmp_path / "htmlcov/coverage_html_cb.js"),
                source_root="htmlcov",
                module="htmlcov.coverage_html_cb",
                file_hash="htmlcov",
                line_count=1,
            ),
        ],
        nodes=[
            api_module,
            service_module,
            test_module,
            htmlcov_module,
            handler,
            run,
            test_run,
        ],
        edges=[
            Edge(source=api_module.id, target=handler.id, kind="defines"),
            Edge(source=service_module.id, target=run.id, kind="defines"),
            Edge(source=test_module.id, target=test_run.id, kind="defines"),
            Edge(source=handler.id, target=run.id, kind="calls"),
            Edge(source=test_run.id, target=run.id, kind="calls"),
        ],
    )

    payload = build_force_graph_export(
        QueryEngine(output_dir),
        ForceGraphExportOptions(min_symbol_degree=1),
    )

    assert payload["schema"] == "ArcGraphForceGraph"
    assert {node["id"] for node in payload["module_graph"]["nodes"]} == {
        api_module.id,
        service_module.id,
    }
    assert payload["module_graph"]["edges"] == [
        {
            "source": api_module.id,
            "target": service_module.id,
            "kind": "calls",
            "weight": 1,
            "layout_weight": 5.0,
            "dominant_confidence": "confirmed",
            "confidence_counts": {"confirmed": 1},
        }
    ]
    assert {node["id"] for node in payload["symbol_graph"]["nodes"]} == {
        handler.id,
        run.id,
    }
    assert test_module.id not in payload["module_symbols"]
    assert payload["meta"]["excluded_node_counts"]["tests"] >= 2
    assert payload["meta"]["excluded_node_counts"]["generated"] >= 1


def test_force_graph_export_keeps_config_resource_signals(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    service_file = FileRecord(
        path="src/pkg/service.py",
        abs_path=str(tmp_path / "src/pkg/service.py"),
        source_root="src",
        module="pkg.service",
        file_hash="service",
        line_count=20,
    )
    handler = Node(
        id="fn:pkg.service.handle",
        kind="function",
        name="handle",
        qualname="pkg.service.handle",
        path="src/pkg/service.py",
    )
    env_config = Node(
        id="config:env:API_BASE_URL",
        kind="config",
        name="API_BASE_URL",
        qualname="API_BASE_URL",
        path="src/pkg/service.py",
    )
    storage_config = Node(
        id="config:browser_storage:localStorage:theme",
        kind="config",
        name="localStorage:theme",
        qualname="localStorage:theme",
        path="src/pkg/service.py",
    )
    session_config = Node(
        id="config:browser_storage:sessionStorage:session-id",
        kind="config",
        name="sessionStorage:session-id",
        qualname="sessionStorage:session-id",
        path="src/pkg/service.py",
    )
    _write_store(
        output_dir,
        files=[service_file],
        nodes=[handler, env_config, storage_config, session_config],
        edges=[
            Edge(source=handler.id, target=env_config.id, kind="configures"),
            Edge(source=handler.id, target=storage_config.id, kind="reads"),
            Edge(source=handler.id, target=session_config.id, kind="writes"),
        ],
    )

    engine = QueryEngine(output_dir)
    payload = build_force_graph_export(
        engine,
        ForceGraphExportOptions(min_symbol_degree=1),
    )
    architecture = engine.architecture()

    nodes_by_id = {node["id"]: node for node in payload["symbol_graph"]["nodes"]}
    assert nodes_by_id[env_config.id]["semantic_tier"] == "resource"
    assert nodes_by_id[storage_config.id]["semantic_tier"] == "resource"
    assert nodes_by_id[session_config.id]["semantic_tier"] == "resource"
    assert {edge["kind"] for edge in payload["symbol_graph"]["edges"]} >= {
        "configures",
        "reads",
        "writes",
    }
    contract = payload["meta"]["visual_contract"]
    assert "config" in contract["resource_node_kinds"]
    assert {"configures", "reads", "writes"} <= set(contract["resource_edge_kinds"])
    assert architecture["resources"]["config"] == 3
    assert architecture["edge_kinds"]["configures"] == 1
    assert architecture["edge_kinds"]["reads"] == 1
    assert architecture["edge_kinds"]["writes"] == 1


def test_force_graph_export_can_include_tests_generated_and_structural_edges(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    left_module = Node(
        id="mod:pkg.left",
        kind="module",
        name="left",
        qualname="pkg.left",
        path="lib/python/pkg/left.py",
    )
    right_module = Node(
        id="mod:pkg.right",
        kind="module",
        name="right",
        qualname="pkg.right",
        path="lib/python/pkg/right.py",
    )
    left_class = Node(
        id="class:pkg.left.Left",
        kind="class",
        name="Left",
        qualname="pkg.left.Left",
        path="lib/python/pkg/left.py",
    )
    right_class = Node(
        id="class:pkg.right.Right",
        kind="class",
        name="Right",
        qualname="pkg.right.Right",
        path="lib/python/pkg/right.py",
    )
    _write_store(
        output_dir,
        files=[
            FileRecord(
                path="lib/python/pkg/left.py",
                abs_path=str(tmp_path / "lib/python/pkg/left.py"),
                source_root="lib/python",
                module="pkg.left",
                file_hash="left",
                line_count=10,
            ),
            FileRecord(
                path="lib/python/pkg/right.py",
                abs_path=str(tmp_path / "lib/python/pkg/right.py"),
                source_root="lib/python",
                module="pkg.right",
                file_hash="right",
                line_count=10,
            ),
        ],
        nodes=[left_module, right_module, left_class, right_class],
        edges=[
            Edge(source=left_module.id, target=left_class.id, kind="defines"),
            Edge(source=right_module.id, target=right_class.id, kind="defines"),
            Edge(source=left_class.id, target=right_class.id, kind="references"),
        ],
    )

    default_payload = build_force_graph_export(QueryEngine(output_dir))
    structural_payload = build_force_graph_export(
        QueryEngine(output_dir),
        ForceGraphExportOptions(include_structural_edges=True),
    )

    assert default_payload["module_graph"]["edges"] == []
    assert structural_payload["module_graph"]["edges"][0]["kind"] == "references"
    assert structural_payload["module_graph"]["edges"][0]["source"] == left_module.id
    assert structural_payload["module_graph"]["edges"][0]["target"] == right_module.id
