from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "framework_adapter_matrix"
EXPECTATIONS_PATH = (
    Path(__file__).parent / "golden" / "framework_adapter_matrix" / "expectations.json"
)


def test_framework_adapter_matrix_contract(tmp_path: Path) -> None:
    output_dir = _build_matrix_index(tmp_path)
    expectations = _read_expectations()
    store = GraphStoreReader.from_current(output_dir)
    engine = QueryEngine(output_dir)
    nodes = {node.id: node for node in store.read_nodes()}
    edges = {(edge.source, edge.target, edge.kind): edge for edge in store.read_edges()}
    adapters = engine.current()["adapter_metrics"]["adapters"]
    unresolved = engine.unresolved(limit=1000)
    semantic_metrics = engine.semantic_stats()["metrics"]

    _assert_expected_nodes(nodes, expectations["expected_nodes"])
    _assert_expected_edges(edges, expectations["expected_edges"])
    _assert_adapter_matrix(adapters, expectations["adapter_matrix"])
    _assert_expected_unresolved(unresolved, expectations["expected_unresolved"])
    _assert_unresolved_summary(unresolved, expectations["unresolved_summary"])
    _assert_quality_floors(semantic_metrics, expectations["minimum_rates"])


def _build_matrix_index(tmp_path: Path) -> Path:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src"), SourceRoot("tests", "tests")],
    ).build()
    return output_dir


def _read_expectations() -> dict[str, Any]:
    return json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))


def _assert_expected_nodes(
    nodes: dict[str, Any],
    expected_nodes: list[dict[str, Any]],
) -> None:
    for expected in expected_nodes:
        node_id = expected["id"]
        assert node_id in nodes
        assert nodes[node_id].kind == expected["kind"]


def _assert_expected_edges(
    edges: dict[tuple[str, str, str], Any],
    expected_edges: list[dict[str, Any]],
) -> None:
    for expected in expected_edges:
        key = (expected["source"], expected["target"], expected["kind"])
        assert key in edges
        edge = edges[key]
        assert edge.confidence == expected["confidence"]
        assert getattr(edge.resolution, "strategy", None) == expected["strategy"]


def _assert_adapter_matrix(
    adapters: dict[str, dict[str, Any]],
    expected_matrix: dict[str, dict[str, Any]],
) -> None:
    for adapter_name, expected in expected_matrix.items():
        assert adapter_name in adapters
        adapter = adapters[adapter_name]
        for key, value in expected.items():
            assert adapter.get(key) == value


def _assert_expected_unresolved(
    unresolved: dict[str, Any],
    expected_records: list[dict[str, Any]],
) -> None:
    records = unresolved["unresolved"]
    counts = Counter(
        (item.get("raw_expression"), item.get("category")) for item in records
    )
    by_key: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
    for item in records:
        by_key.setdefault(
            (item.get("raw_expression"), item.get("category")), []
        ).append(item)

    for expected in expected_records:
        key = (expected["raw_expression"], expected["category"])
        assert counts[key] == expected["count"]
        for record in by_key[key]:
            assert record["release_blocking"] is expected["release_blocking"]
            assert record["risk_level"] == expected["risk_level"]


def _assert_unresolved_summary(
    unresolved: dict[str, Any],
    expected_summary: dict[str, Any],
) -> None:
    classification = unresolved["classification"]
    assert classification["total_unresolved"] == expected_summary["total"]
    assert (
        classification["release_blocking_count"]
        == expected_summary["release_blocking_count"]
    )
    for category, count in expected_summary["category_counts"].items():
        assert classification["category_counts"][category] == count


def _assert_quality_floors(
    semantic_metrics: dict[str, Any],
    minimum_rates: dict[str, float],
) -> None:
    expression_rates = semantic_metrics["resolution_rate_by_expression_kind"]
    assert semantic_metrics["resolution_rate"] >= minimum_rates["overall"]
    assert expression_rates["attribute"] >= minimum_rates["attribute"]
    assert expression_rates["chain"] >= minimum_rates["chain"]
    assert expression_rates["direct"] >= minimum_rates["direct"]
