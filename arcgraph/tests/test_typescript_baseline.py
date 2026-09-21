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

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "typescript_baseline"
EXPECTATIONS_PATH = (
    Path(__file__).parent / "golden" / "typescript_baseline" / "expectations.json"
)


def _load_expectations() -> dict[str, Any]:
    return json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))


def _requires_typescript_baseline() -> bool:
    return os.environ.get("ARCGRAPH_REQUIRE_TS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _skip_or_fail_typescript_baseline(message: str) -> None:
    if _requires_typescript_baseline():
        pytest.fail(message)
    pytest.skip(message)


def _build_fixture(tmp_path: Path) -> GraphStoreReader:
    if shutil.which("node") is None:
        _skip_or_fail_typescript_baseline(
            "Node.js is required for the TypeScript baseline fixture."
        )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[
            SourceRoot("api"),
            SourceRoot("src"),
            SourceRoot("tests", "tests"),
        ],
    ).build()
    reader = GraphStoreReader.from_current(output_dir)
    warning_kinds = {warning.kind for warning in reader.read_warnings()}
    if "typescript_frontend_unavailable" in warning_kinds:
        _skip_or_fail_typescript_baseline(
            "TypeScript compiler API is unavailable in this environment."
        )
    return reader


def test_typescript_frontend_baseline_golden_matrix(tmp_path: Path) -> None:
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
        if "strategy" in expected:
            assert edge.resolution.strategy == expected["strategy"]

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
    assert set(expectations["expected_external_packages"]).issubset(external_packages)
    assert expectations["expected_frontend_metrics"][
        "phase_timings_key"
    ] in reader.metadata.get("phase_timings", {})

    warning_kinds = {warning.kind for warning in warnings}
    assert len(warnings) <= expectations["expected_warnings"]["max_total"]
    assert not warning_kinds.intersection(
        expectations["expected_warnings"]["forbidden_kinds"]
    )

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
    ts_modules = [node for node in ts_nodes if node.kind == "module"]
    minimum_counts = expectations["minimum_counts"]
    assert len(ts_nodes) >= minimum_counts["typescript_nodes"]
    assert len(ts_edges) >= minimum_counts["typescript_edges"]
    assert len(ts_modules) >= minimum_counts["typescript_modules"]


def test_typescript_frontend_missing_node_stays_warning(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "arcgraph.pipeline.typescript_frontend.shutil.which",
        lambda _name: None,
    )
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    warning_kinds = {
        warning.kind
        for warning in GraphStoreReader.from_current(output_dir).read_warnings()
    }
    assert "typescript_frontend_unavailable" in warning_kinds
