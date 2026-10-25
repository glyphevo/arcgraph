"""Resources with bare display qualnames must compete with same-name symbols."""

from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreWriter
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.schemas import IndexMetadata, Node


@pytest.mark.parametrize(
    "kind,prefix",
    [
        ("table", "table"),
        ("queue", "queue"),
        ("worker_task", "worker"),
        ("mcp_tool", "mcp_tool"),
        ("log_sink", "log"),
        ("cli_command", "cli"),
        ("config", "config:env"),
    ],
)
def test_resource_bare_qualnames_do_not_override_same_name_definitions(
    tmp_path: Path, kind: str, prefix: str
) -> None:
    out = tmp_path / "out"
    resource = Node(id=f"{prefix}:shared", kind=kind, name="shared", qualname="shared")
    symbol = Node(
        id="fn:pkg.shared", kind="function", name="shared", qualname="pkg.shared"
    )
    GraphStoreWriter(out).write(
        IndexMetadata(
            index_version="resources", repo_root=str(tmp_path), source_roots=["."]
        ),
        files=[],
        nodes=[resource, symbol],
        edges=[],
        warnings=[],
    )
    engine = QueryEngine(out)
    result = engine.symbol("shared")["target_resolution"]
    assert result["status"] == "ambiguous"
    assert result["strategy"] == "exact_name"
    assert {candidate["id"] for candidate in result["candidates"]} == {
        resource.id,
        symbol.id,
    }
    by_id = engine.symbol(resource.id)["target_resolution"]
    assert by_id["strategy"] == "node_id"
    assert by_id["resolved_ids"] == [resource.id]
    qualified = engine.symbol("pkg.shared")["target_resolution"]
    assert qualified["status"] == "resolved"
    assert qualified["strategy"] == "exact_qualname"


@pytest.mark.parametrize("definitions,truncated", [(7, False), (8, True), (12, True)])
def test_exact_name_resource_is_pinned_before_candidate_truncation(
    tmp_path: Path, definitions: int, truncated: bool
) -> None:
    out = tmp_path / "out"
    nodes = [
        Node(
            id=f"fn:pkg.module_{i}.shared",
            kind="function",
            name="shared",
            qualname=f"pkg.module_{i}.shared",
        )
        for i in range(definitions)
    ]
    nodes.append(
        Node(id="table:shared", kind="table", name="shared", qualname="shared")
    )
    GraphStoreWriter(out).write(
        IndexMetadata(
            index_version="resource-candidate-bound",
            repo_root=str(tmp_path),
            source_roots=["."],
        ),
        files=[],
        nodes=nodes,
        edges=[],
        warnings=[],
    )
    resolution = QueryEngine(out).symbol("shared")["target_resolution"]
    assert resolution["status"] == "ambiguous"
    assert len(resolution["candidates"]) == 8
    assert resolution["candidates_truncated"] is truncated
    assert [item["id"] for item in resolution["candidates"]] == [
        "table:shared",
        *sorted(node.id for node in nodes if node.kind == "function")[:7],
    ]


@pytest.mark.parametrize(
    "query,status,ids,strategy",
    [
        (
            "pkg.tasks.only",
            "resolved",
            {"worker:celery:pkg.tasks.only"},
            "exact_qualname",
        ),
        (
            "pkg.tasks.add",
            "ambiguous",
            {"fn:pkg.tasks.add", "worker:celery:pkg.tasks.add"},
            "exact_qualname",
        ),
        ("celery:default", "resolved", {"queue:celery:default"}, "exact_qualname"),
        ("mealie.recipe", "resolved", {"log:mealie.recipe"}, "exact_name"),
        (
            "categories",
            "ambiguous",
            {"mod:categories", "table:categories"},
            "exact_name",
        ),
    ],
)
def test_review_qualified_resources_and_bare_module_competition(
    tmp_path: Path, query: str, status: str, ids: set[str], strategy: str
) -> None:
    out = tmp_path / "out"
    nodes = [
        Node(
            id="fn:pkg.tasks.add", kind="function", name="add", qualname="pkg.tasks.add"
        ),
        Node(
            id="worker:celery:pkg.tasks.only",
            kind="worker_task",
            name="only",
            qualname="pkg.tasks.only",
        ),
        Node(
            id="worker:celery:pkg.tasks.add",
            kind="worker_task",
            name="add",
            qualname="pkg.tasks.add",
        ),
        Node(
            id="queue:celery:default",
            kind="queue",
            name="default",
            qualname="celery:default",
        ),
        Node(
            id="log:mealie.recipe",
            kind="log_sink",
            name="mealie.recipe",
            qualname="mealie.recipe",
        ),
        Node(
            id="mod:categories", kind="module", name="categories", qualname="categories"
        ),
        Node(
            id="table:categories",
            kind="table",
            name="categories",
            qualname="categories",
        ),
    ]
    GraphStoreWriter(out).write(
        IndexMetadata(
            index_version="qualified-resources",
            repo_root=str(tmp_path),
            source_roots=["."],
        ),
        files=[],
        nodes=nodes,
        edges=[],
        warnings=[],
    )
    result = QueryEngine(out).symbol(query)["target_resolution"]
    assert result["status"] == status
    assert result["strategy"] == strategy
    actual = (
        result["resolved_ids"]
        if status == "resolved"
        else [item["id"] for item in result["candidates"]]
    )
    assert set(actual) == ids


@pytest.mark.parametrize("resource_count", [7, 10])
def test_all_resource_kinds_keep_their_order_before_nonresource_matches(
    tmp_path: Path, resource_count: int
) -> None:
    kinds = [
        "table",
        "queue",
        "worker_task",
        "mcp_tool",
        "log_sink",
        "cli_command",
        "config",
    ]
    resources = [
        Node(
            id=f"{kinds[i % 7]}:{i}:shared",
            kind=kinds[i % 7],
            name="shared",
            qualname="shared",
        )
        for i in range(resource_count)
    ]
    definitions = [
        Node(
            id=f"fn:pkg.m{i}.shared",
            kind="function",
            name="shared",
            qualname=f"pkg.m{i}.shared",
        )
        for i in range(12)
    ]
    out = tmp_path / "out"
    GraphStoreWriter(out).write(
        IndexMetadata(
            index_version="resource-order", repo_root=str(tmp_path), source_roots=["."]
        ),
        files=[],
        nodes=resources + definitions,
        edges=[],
        warnings=[],
    )
    result = QueryEngine(out).symbol("shared")["target_resolution"]
    expected = sorted(resources, key=lambda node: (node.kind, node.id)) + sorted(
        definitions, key=lambda node: node.id
    )
    assert [item["id"] for item in result["candidates"]] == [
        node.id for node in expected[:8]
    ]
    assert result["candidates_truncated"] is True
