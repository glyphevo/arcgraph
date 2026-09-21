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
from arcgraph.pipeline.reindexer import ArcGraphReindexer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "typescript_config_resource"
EXPECTATIONS_PATH = (
    Path(__file__).parent
    / "golden"
    / "typescript_config_resource"
    / "expectations.json"
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


def _skip_or_fail_typescript_fixture(message: str) -> None:
    if _requires_typescript_baseline():
        pytest.fail(message)
    pytest.skip(message)


def _build_fixture(tmp_path: Path) -> GraphStoreReader:
    if shutil.which("node") is None:
        _skip_or_fail_typescript_fixture(
            "Node.js is required for the TypeScript config/resource fixture."
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


def test_typescript_config_resource_signals_are_explicit_and_conservative(
    tmp_path: Path,
) -> None:
    reader = _build_fixture(tmp_path)
    expectations = _load_expectations()
    nodes = reader.read_nodes()
    edges = reader.read_edges()
    warnings = reader.read_warnings()
    diagnostics = reader.read_diagnostics()

    node_by_id = {node.id: node for node in nodes}
    edge_by_key = {(edge.source, edge.kind, edge.target): edge for edge in edges}

    for expected in expectations["expected_nodes"]:
        node = node_by_id.get(expected["id"])
        assert node is not None, expected
        assert node.kind == expected["kind"]
        if node.kind == "config":
            assert node.path is None
            assert node.properties.get("frontend_name") == "shared-config-resource"

    for expected in expectations["expected_edges"]:
        edge = edge_by_key.get(
            (expected["source"], expected["kind"], expected["target"])
        )
        assert edge is not None, expected
        assert edge.confidence == expected["confidence"]
        assert edge.evidence
        assert edge.evidence[0].kind == expected["evidence_kind"]
        assert edge.evidence[0].path == "src/config.ts"
        assert edge.evidence[0].snippet is None
        assert edge.properties.get("frontend_name") == "typescript-static"

    for forbidden in expectations["forbidden_edges"]:
        assert (
            forbidden["source"],
            forbidden["kind"],
            forbidden["target"],
        ) not in edge_by_key

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

    dynamic_diagnostics = [
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.diagnostic_kind == "typescript_config_dynamic"
    ]
    assert dynamic_diagnostics
    assert all(
        diagnostic.frontend_name == "typescript-static"
        for diagnostic in dynamic_diagnostics
    )


def test_shared_config_node_survives_incremental_reindex(tmp_path: Path) -> None:
    """A config node shared with an unchanged file must not die with the changed one."""
    if shutil.which("node") is None:
        _skip_or_fail_typescript_fixture(
            "Node.js is required for the TypeScript config/resource fixture."
        )
    src = tmp_path / "src"
    src.mkdir()
    changed = src / "a.ts"
    changed.write_text(
        'export function readA(): string | null { return localStorage.getItem("a"); }\n',
        encoding="utf-8",
    )
    (src / "b.ts").write_text(
        'export function readB(): string | null { return localStorage.getItem("a"); }\n',
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental"
    ArcGraphIndexer(tmp_path, incremental_output, roots).build()

    config_id = "config:browser_storage:localStorage:a"
    before_nodes = {
        node.id
        for node in GraphStoreReader.from_current(incremental_output).read_nodes()
    }
    assert config_id in before_nodes

    changed.write_text(
        'export function readA(): string | null { return localStorage.getItem("b"); }\n',
        encoding="utf-8",
    )
    ArcGraphReindexer(tmp_path, incremental_output, roots).reindex_changed()
    full_output = tmp_path / "full"
    ArcGraphIndexer(tmp_path, full_output, roots).build()

    def resource_state(output: Path) -> dict[str, object]:
        reader = GraphStoreReader.from_current(output)
        nodes = {node.id: node for node in reader.read_nodes()}
        edges = reader.read_edges()
        config = nodes.get(config_id)
        profiles = {
            node.id: node.properties["similarity"].get("resource_targets")
            for node in nodes.values()
            if isinstance(node.properties.get("similarity"), dict)
        }
        return {
            "config_node_present": config is not None,
            "config_path": None if config is None else config.path,
            "config_start_line": None if config is None else config.start_line,
            "config_end_line": None if config is None else config.end_line,
            "config_properties": None if config is None else dict(config.properties),
            "reads_edges": sorted(
                (edge.source, edge.target) for edge in edges if edge.target == config_id
            ),
            "resource_targets": profiles,
        }

    incremental_state = resource_state(incremental_output)
    assert incremental_state["config_node_present"] is True
    assert incremental_state["config_path"] is None
    assert incremental_state["reads_edges"] == [("fn:b.readB", config_id)]
    assert incremental_state == resource_state(full_output)


def test_shared_env_config_syntax_stays_on_surviving_witness(tmp_path: Path) -> None:
    """Access syntax is evidence about the reference, not a resource property."""
    if shutil.which("node") is None:
        _skip_or_fail_typescript_fixture(
            "Node.js is required for the TypeScript config/resource fixture."
        )
    src = tmp_path / "src"
    src.mkdir()
    changed = src / "a.ts"
    changed.write_text(
        "export function readA(): string | undefined { return process.env.SHARED; }\n",
        encoding="utf-8",
    )
    (src / "b.ts").write_text(
        "export function readB(): string | undefined {"
        " return import.meta.env.SHARED; }\n",
        encoding="utf-8",
    )
    roots = [SourceRoot("src")]
    incremental_output = tmp_path / "incremental"
    ArcGraphIndexer(tmp_path, incremental_output, roots).build()

    config_id = "config:env:SHARED"
    before = next(
        node
        for node in GraphStoreReader.from_current(incremental_output).read_nodes()
        if node.id == config_id
    )
    assert before.path is None
    assert before.properties == {
        "config_kind": "env",
        "key": "SHARED",
        "frontend_name": "shared-config-resource",
        "frontend_version": "1",
        "language": "neutral",
    }

    changed.write_text(
        "export function readA(): string | undefined { return process.env.OTHER; }\n",
        encoding="utf-8",
    )
    ArcGraphReindexer(tmp_path, incremental_output, roots).reindex_changed()
    full_output = tmp_path / "full"
    ArcGraphIndexer(tmp_path, full_output, roots).build()

    def env_state(output: Path) -> dict[str, object]:
        node = next(
            candidate
            for candidate in GraphStoreReader.from_current(output).read_nodes()
            if candidate.id == config_id
        )
        return {
            "path": node.path,
            "start_line": node.start_line,
            "end_line": node.end_line,
            "properties": dict(node.properties),
        }

    incremental_state = env_state(incremental_output)
    assert incremental_state["path"] is None
    assert incremental_state["properties"] == {
        "config_kind": "env",
        "key": "SHARED",
        "frontend_name": "shared-config-resource",
        "frontend_version": "1",
        "language": "neutral",
    }
    witnesses = [
        e
        for e in GraphStoreReader.from_current(incremental_output).read_edges()
        if e.target == config_id
    ]
    assert len(witnesses) == 1
    assert witnesses[0].properties["syntax"] == "import.meta.env"
    assert witnesses[0].evidence[0].path == "src/b.ts"
    assert incremental_state == env_state(full_output)
