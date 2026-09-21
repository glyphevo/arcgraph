from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.cli import main as cli_main
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.openapi_frontend import _operation_node_id, _schema_node_id

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "openapi_protocol_baseline"
EXPECTATIONS_PATH = (
    Path(__file__).parent / "golden" / "openapi_protocol_baseline" / "expectations.json"
)


def _load_expectations() -> dict[str, Any]:
    return json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))


def _build_fixture(tmp_path: Path, spec_name: str = "openapi.json") -> GraphStoreReader:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
        openapi_path=FIXTURE_ROOT / spec_name,
    ).build()
    return GraphStoreReader.from_current(output_dir)


def test_openapi_protocol_graph_ingestion_matches_golden(tmp_path: Path) -> None:
    reader = _build_fixture(tmp_path)
    expectations = _load_expectations()
    nodes = reader.read_nodes()
    edges = reader.read_edges()
    warnings = reader.read_warnings()

    node_by_id = {node.id: node for node in nodes}
    edge_by_key = {(edge.source, edge.kind, edge.target): edge for edge in edges}

    for expected in expectations["expected_nodes"]:
        node_id = expected.get("id")
        if node_id is None and "operation" in expected:
            operation = expected["operation"]
            node_id = _operation_node_id(operation["method"], operation["path"])
        if node_id is None and "schema" in expected:
            node_id = _schema_node_id(expected["schema"])
        node = node_by_id.get(node_id)
        assert node is not None, expected
        assert node.kind == expected["kind"]
        if not str(node_id).startswith("route:"):
            assert node.properties.get("frontend_name") == "openapi-protocol"

    for expected in expectations["expected_edges"]:
        source = expected.get("source")
        if source is None and "source_operation" in expected:
            operation = expected["source_operation"]
            source = _operation_node_id(operation["method"], operation["path"])
        target = expected.get("target")
        if target is None and "target_schema" in expected:
            target = _schema_node_id(expected["target_schema"])
        edge = edge_by_key.get((source, expected["kind"], target))
        assert edge is not None, expected
        assert edge.confidence == expected["confidence"]
        assert edge.resolution.strategy == expected["strategy"]
        assert edge.properties.get("frontend_name") == "openapi-protocol"
        assert not any(evidence.snippet for evidence in edge.evidence)

    warning_kinds = {warning.kind for warning in warnings}
    assert set(expectations["expected_warnings"]).issubset(warning_kinds)
    adapter_metrics = reader.metadata["adapter_metrics"]["openapi-protocol"]
    for key, value in expectations["expected_adapter_metrics"].items():
        assert adapter_metrics[key] == value
    assert reader.metadata["phase_timings"]["openapi-protocol"]["openapi_protocol"] >= 0


def test_build_cli_accepts_openapi_spec(tmp_path: Path, capsys) -> None:
    output_dir = tmp_path / "arcgraph"
    exit_code = cli_main(
        [
            "--repo-root",
            str(FIXTURE_ROOT),
            "--output-dir",
            str(output_dir),
            "build",
            "--root",
            "src",
            "--openapi-spec",
            str(FIXTURE_ROOT / "openapi.json"),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["node_count"] > 0
    reader = GraphStoreReader.from_current(output_dir)
    assert "openapi-protocol" in reader.metadata["adapter_metrics"]


def test_openapi_operation_id_handler_match_is_visible_through_route_query(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
        openapi_path=FIXTURE_ROOT / "openapi.json",
    ).build()

    route = QueryEngine(output_dir).route("GET", "/api/v1/pets")

    assert route["status"] == "available"
    assert "fn:pkg.api.listPets" in {node["id"] for node in route["targets"]}
    assert (
        "route:GET:/api/v1/pets",
        "invokes",
        "fn:pkg.api.listPets",
    ) in {(edge["source"], edge["kind"], edge["target"]) for edge in route["edges"]}


def test_openapi_spec_is_not_scanned_without_explicit_input(tmp_path: Path) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
    ).build()
    reader = GraphStoreReader.from_current(output_dir)

    assert "openapi-protocol" not in reader.metadata.get("adapter_metrics", {})
    assert "openapi.json" not in {file.path for file in reader.read_files()}
    assert not any(
        node.properties.get("frontend_name") == "openapi-protocol"
        for node in reader.read_nodes()
    )


def test_openapi_yaml_input_is_explicitly_supported(tmp_path: Path) -> None:
    reader = _build_fixture(tmp_path, spec_name="openapi.yaml")
    edges = reader.read_edges()

    assert "openapi-protocol" in reader.metadata["adapter_metrics"]
    assert (
        "route:GET:/api/v1/yaml-pets",
        "invokes",
        "fn:pkg.api.listPets",
    ) in {(edge.source, edge.kind, edge.target) for edge in edges}


def test_openapi_missing_malformed_and_invalid_input_are_visible(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    src_dir = repo_root / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "app.py").write_text(
        "def handler():\n    return None\n", encoding="utf-8"
    )

    missing_output = tmp_path / "missing-output"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=missing_output,
        source_roots=[SourceRoot("src")],
        openapi_path=repo_root / "missing-openapi.json",
    ).build()
    missing_warnings = {
        warning.kind
        for warning in GraphStoreReader.from_current(missing_output).read_warnings()
    }
    assert "openapi_protocol_missing" in missing_warnings

    bad_openapi = repo_root / "bad-openapi.json"
    bad_openapi.write_text("{not-json", encoding="utf-8")
    bad_output = tmp_path / "bad-output"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=bad_output,
        source_roots=[SourceRoot("src")],
        openapi_path=bad_openapi,
    ).build()
    bad_warnings = {
        warning.kind
        for warning in GraphStoreReader.from_current(bad_output).read_warnings()
    }
    assert "openapi_protocol_parse_error" in bad_warnings

    invalid_openapi = repo_root / "invalid-openapi.json"
    invalid_openapi.write_text(
        json.dumps({"openapi": "2.0", "paths": {}}),
        encoding="utf-8",
    )
    invalid_output = tmp_path / "invalid-output"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=invalid_output,
        source_roots=[SourceRoot("src")],
        openapi_path=invalid_openapi,
    ).build()
    invalid_reader = GraphStoreReader.from_current(invalid_output)
    invalid_warnings = {warning.kind for warning in invalid_reader.read_warnings()}
    assert "openapi_protocol_invalid_spec" in invalid_warnings
    assert (
        invalid_reader.metadata["adapter_metrics"]["openapi-protocol"]["invalid_specs"]
        == 1
    )


def test_openapi_outside_repo_input_is_blocked(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    src_dir = repo_root / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "app.py").write_text(
        "def handler():\n    return None\n", encoding="utf-8"
    )
    outside_spec = tmp_path / "outside-openapi.json"
    outside_spec.write_text(
        json.dumps({"openapi": "3.0.3", "paths": {}}),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"

    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
        openapi_path=outside_spec,
    ).build()
    reader = GraphStoreReader.from_current(output_dir)
    warnings = reader.read_warnings()

    assert "openapi_protocol_path_outside_repo" in {
        warning.kind for warning in warnings
    }
    assert (
        reader.metadata["adapter_metrics"]["openapi-protocol"]["path_outside_repo"] == 1
    )
