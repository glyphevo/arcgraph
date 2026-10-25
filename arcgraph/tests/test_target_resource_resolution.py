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


@pytest.mark.parametrize("definitions,truncated", [(8, False), (9, True)])
def test_resource_name_reserves_one_extra_candidate_slot(
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
    assert len(resolution["candidates"]) == 9
    assert resolution["candidates_truncated"] is truncated
    if not truncated:
        assert "table:shared" in {item["id"] for item in resolution["candidates"]}
