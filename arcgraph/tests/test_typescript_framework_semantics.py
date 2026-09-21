from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import FileScanner, SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.openapi_frontend import _operation_node_id
from arcgraph.pipeline.typescript_frameworks import TypeScriptFrameworkAnalyzer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "typescript_frameworks"


def _requires_typescript_baseline() -> bool:
    return os.environ.get("ARCGRAPH_REQUIRE_TS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _skip_or_fail_typescript_fixture(message: str) -> None:
    if _requires_typescript_baseline():
        pytest.fail(message)
    pytest.skip(message)


def _build_fixture(tmp_path: Path) -> GraphStoreReader:
    if shutil.which("node") is None:
        _skip_or_fail_typescript_fixture(
            "Node.js is required for the TypeScript framework semantics fixture."
        )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot(".")],
    ).build()
    reader = GraphStoreReader.from_current(output_dir)
    warning_kinds = {warning.kind for warning in reader.read_warnings()}
    if "typescript_frontend_unavailable" in warning_kinds:
        _skip_or_fail_typescript_fixture(
            "TypeScript compiler API is unavailable in this environment."
        )
    return reader


def test_nextjs_framework_semantics(tmp_path: Path) -> None:
    reader = _build_fixture(tmp_path)
    nodes = reader.read_nodes()
    edges = reader.read_edges()
    node_by_id = {node.id: node for node in nodes}
    edge_by_key = {(edge.source, edge.kind, edge.target): edge for edge in edges}

    expected_routes = {
        "route:GET:/": {"router": "app", "route_kind": "page"},
        "route:GET:/blog/[slug]": {"router": "app", "route_kind": "page"},
        "route:GET:/api/users": {"router": "app", "route_kind": "route_handler"},
        "route:POST:/api/users": {"router": "app", "route_kind": "route_handler"},
        "route:GET:/about": {"router": "pages", "route_kind": "page"},
        "route:ANY:/api/legacy": {"router": "pages", "route_kind": "api_route"},
    }
    for route, properties in expected_routes.items():
        node = node_by_id.get(route)
        assert node is not None, route
        assert node.kind == "route"
        assert node.properties["framework"] == "nextjs"
        for key, value in properties.items():
            assert node.properties[key] == value

    assert node_by_id["route:GET:/blog/[slug]"].properties["dynamic_segments"] == [
        "[slug]"
    ]

    handler_get = edge_by_key[
        ("route:GET:/api/users", "invokes", "fn:app.api.users.route.GET")
    ]
    assert handler_get.confidence == "inferred"
    assert handler_get.resolution.strategy == "nextjs_exported_http_method"

    handler_post = edge_by_key[
        ("route:POST:/api/users", "invokes", "fn:app.api.users.route.POST")
    ]
    assert handler_post.confidence == "inferred"
    assert handler_post.resolution.strategy == "nextjs_exported_http_method"

    pages_api = edge_by_key[
        ("route:ANY:/api/legacy", "invokes", "fn:pages.api.legacy.handler")
    ]
    assert pages_api.confidence == "inferred"
    assert pages_api.resolution.strategy == "nextjs_pages_api"

    page_render = edge_by_key[
        (
            "route:GET:/blog/[slug]",
            "renders",
            "component:app.blog.[slug].page.BlogPostPage",
        )
    ]
    assert page_render.confidence == "inferred"

    static_link = edge_by_key[
        ("component:app.page.HomePage", "links_to", "route:GET:/about")
    ]
    assert static_link.confidence == "heuristic"


def test_vue_framework_semantics(tmp_path: Path) -> None:
    reader = _build_fixture(tmp_path)
    nodes = reader.read_nodes()
    edges = reader.read_edges()
    warnings = reader.read_warnings()
    node_by_id = {node.id: node for node in nodes}
    edge_by_key = {(edge.source, edge.kind, edge.target): edge for edge in edges}

    child = node_by_id["component:src.components.ChildCard.ChildCard"]
    parent = node_by_id["component:src.components.ParentSetup.ParentSetup"]
    options = node_by_id["component:src.components.OptionsCard.OptionsCard"]
    assert child.properties["framework"] == "vue"
    assert child.properties["script_setup"] is True
    assert child.properties["script_lang"] == "ts"
    assert parent.properties["script_setup"] is True
    assert options.properties["script_lang"] == "ts"

    for declaration in [
        "vue_prop:src.components.ChildCard.ChildCard:title",
        "vue_prop:src.components.ChildCard.ChildCard:featured",
        "vue_emit:src.components.ChildCard.ChildCard:select",
        "vue_prop:src.components.OptionsCard.OptionsCard:label",
        "vue_emit:src.components.OptionsCard.OptionsCard:save",
    ]:
        assert declaration in node_by_id

    import_edge = edge_by_key[
        (
            "component:src.components.ParentSetup.ParentSetup",
            "imports",
            "component:src.components.ChildCard.ChildCard",
        )
    ]
    assert import_edge.confidence == "confirmed"

    render_edge = edge_by_key[
        (
            "component:src.components.ParentSetup.ParentSetup",
            "renders",
            "component:src.components.ChildCard.ChildCard",
        )
    ]
    assert render_edge.confidence == "heuristic"

    vue_route = edge_by_key[
        (
            "route:VIEW:/vue",
            "renders",
            "component:src.components.ParentSetup.ParentSetup",
        )
    ]
    assert vue_route.confidence == "inferred"
    assert vue_route.resolution.strategy == "vue_router_static_route"

    assert any(
        warning.kind == "vue_dynamic_component_unresolved"
        and warning.path == "src/components/ParentSetup.vue"
        for warning in warnings
    )


def test_vue_variant_module_keeps_logical_display_name(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "Widget.ts").write_text(
        "export const widget = true;\n",
        encoding="utf-8",
    )
    (source_dir / "Widget.vue").write_text(
        '<script setup lang="ts">\nconst widget = true;\n</script>\n',
        encoding="utf-8",
    )
    files = FileScanner(
        tmp_path,
        [SourceRoot("src")],
        file_extensions=(".ts", ".vue"),
    ).scan()
    vue_file = next(file for file in files if file.path == "src/Widget.vue")

    result = TypeScriptFrameworkAnalyzer(str(tmp_path)).analyze([vue_file], [])
    module = next(node for node in result.nodes if node.kind == "module")

    assert "__arcgraph_variant_vue_" in vue_file.module
    assert module.id == f"mod:{vue_file.module}"
    assert module.name == "Widget"
    assert module.qualname == vue_file.module
    assert module.properties["display_module"] == "Widget"


def test_nextjs_and_vue_generated_outputs_are_ignored(tmp_path: Path) -> None:
    reader = _build_fixture(tmp_path)
    files = reader.read_files()
    nodes = reader.read_nodes()
    ignored_prefixes = (".next/", ".nuxt/", "dist/")

    assert not any(file.path.startswith(ignored_prefixes) for file in files)
    assert not any(
        node.path and node.path.startswith(ignored_prefixes) for node in nodes
    )
    assert "component:GeneratedNextPage.GeneratedNextPage" not in {
        node.id for node in nodes
    }


def test_nextjs_vue_are_framework_metrics_not_language_tiers(tmp_path: Path) -> None:
    reader = _build_fixture(tmp_path)
    tiers = reader.metadata["language_tiers"]
    assert tiers["typescript"]["tier"] == "L3"
    assert tiers["javascript"]["tier"] == "L3"
    assert "nextjs" not in tiers
    assert "vue" not in tiers

    frameworks = reader.metadata["adapter_metrics"]["typescript-static"]["frameworks"]
    assert frameworks["nextjs"]["status"] == "available"
    assert frameworks["vue"]["status"] == "available"
    assert frameworks["nextjs"]["routes"] >= 6
    assert frameworks["vue"]["components"] >= 3


def test_nextjs_openapi_route_collision_uses_global_route_identity(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        _skip_or_fail_typescript_fixture(
            "Node.js is required for the TypeScript framework semantics fixture."
        )
    repo_root = tmp_path / "repo"
    shutil.copytree(FIXTURE_ROOT, repo_root)
    openapi_path = repo_root / "openapi-collision.json"
    openapi_path.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "Collision", "version": "1.0.0"},
                "paths": {
                    "/api/users": {
                        "get": {
                            "summary": "List users",
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"

    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot(".")],
        openapi_path=openapi_path,
    ).build()
    reader = GraphStoreReader.from_current(output_dir)
    nodes = reader.read_nodes()
    edges = reader.read_edges()
    route_id = "route:GET:/api/users"
    operation_id = _operation_node_id("GET", "/api/users")

    route_nodes = [node for node in nodes if node.id == route_id]
    edge_keys = {(edge.source, edge.kind, edge.target) for edge in edges}

    assert len(route_nodes) == 1
    assert route_nodes[0].properties["framework"] == "nextjs"
    assert (operation_id, "defines", route_id) in edge_keys
    assert (route_id, "invokes", "fn:app.api.users.route.GET") in edge_keys
