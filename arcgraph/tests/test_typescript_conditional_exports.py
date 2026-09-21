from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "typescript_conditional_exports"
EXPECTATIONS_PATH = (
    Path(__file__).parent
    / "golden"
    / "typescript_conditional_exports"
    / "expectations.json"
)


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


def _load_expectations() -> dict[str, Any]:
    return json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))


def _build_fixture(tmp_path: Path) -> GraphStoreReader:
    if shutil.which("node") is None:
        _skip_or_fail_typescript_fixture(
            "Node.js is required for the TypeScript conditional exports fixture."
        )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    reader = GraphStoreReader.from_current(output_dir)
    warning_kinds = {warning.kind for warning in reader.read_warnings()}
    if "typescript_frontend_unavailable" in warning_kinds:
        _skip_or_fail_typescript_fixture(
            "TypeScript compiler API is unavailable in this environment."
        )
    return reader


def test_typescript_conditional_exports_resolve_static_runtime_targets(
    tmp_path: Path,
) -> None:
    reader = _build_fixture(tmp_path)
    expectations = _load_expectations()
    nodes = reader.read_nodes()
    edges = reader.read_edges()
    warnings = reader.read_warnings()

    node_by_id = {node.id: node for node in nodes}
    edge_by_key = {(edge.source, edge.kind, edge.target): edge for edge in edges}

    for expected in expectations["expected_nodes"]:
        node = node_by_id.get(expected["id"])
        assert node is not None, expected
        assert node.kind == expected["kind"]

    for expected in expectations["expected_edges"]:
        edge = edge_by_key.get(
            (expected["source"], expected["kind"], expected["target"])
        )
        assert edge is not None, expected
        assert edge.confidence == expected["confidence"]
        assert edge.properties.get("frontend_name") == "typescript-static"

    for forbidden in expectations["forbidden_edges"]:
        assert (
            forbidden["source"],
            forbidden["kind"],
            forbidden["target"],
        ) not in edge_by_key

    external_packages = {
        node.properties.get("package")
        for node in nodes
        if node.kind == "external_package"
    }
    assert not external_packages.intersection(
        expectations["forbidden_external_packages"]
    )

    for expected in expectations["expected_warnings"]:
        matching = [
            warning
            for warning in warnings
            if warning.kind == expected["kind"] and warning.path == expected["path"]
        ]
        assert matching, expected
        assert any(
            all(
                fragment in warning.message for fragment in expected["message_contains"]
            )
            for warning in matching
        )

    minimum_counts = expectations["minimum_counts"]
    ts_nodes = [
        node
        for node in nodes
        if node.properties.get("frontend_name") == "typescript-static"
    ]
    ts_edges = [
        edge
        for edge in edges
        if edge.properties.get("frontend_name") == "typescript-static"
    ]
    assert len(ts_nodes) >= minimum_counts["typescript_nodes"]
    assert len(ts_edges) >= minimum_counts["typescript_edges"]
