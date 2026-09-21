from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.query_engine import QueryEngine
from arcgraph.core.scanner import SourceRoot
from arcgraph.interfaces.cli import main as cli_main
from arcgraph.pipeline.indexer import ArcGraphIndexer
from arcgraph.pipeline.scip_frontend import _external_symbol_id, _symbol_id

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "scip_protocol_baseline"
EXPECTATIONS_PATH = (
    Path(__file__).parent / "golden" / "scip_protocol_baseline" / "expectations.json"
)


def _load_expectations() -> dict[str, Any]:
    return json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))


def _build_fixture(tmp_path: Path) -> GraphStoreReader:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
        scip_graph_index_path=FIXTURE_ROOT / "scip-index.json",
    ).build()
    return GraphStoreReader.from_current(output_dir)


def test_scip_protocol_graph_ingestion_matches_golden(tmp_path: Path) -> None:
    reader = _build_fixture(tmp_path)
    expectations = _load_expectations()
    nodes = reader.read_nodes()
    edges = reader.read_edges()
    warnings = reader.read_warnings()

    node_by_id = {node.id: node for node in nodes}
    edge_by_key = {(edge.source, edge.kind, edge.target): edge for edge in edges}
    node_by_symbol = {
        str(node.properties.get("scip_symbol")): node
        for node in nodes
        if node.properties.get("scip_symbol")
    }

    for expected in expectations["expected_nodes"]:
        if "id" in expected:
            node = node_by_id.get(expected["id"])
        elif "symbol" in expected and expected["kind"] == "external_symbol":
            node = node_by_id.get(_external_symbol_id(expected["symbol"]))
        elif "symbol" in expected:
            node = node_by_symbol.get(expected["symbol"])
        else:
            node = next(
                (
                    candidate
                    for candidate in nodes
                    if candidate.kind == expected["kind"]
                    and candidate.path == expected["path"]
                ),
                None,
            )
        assert node is not None, expected
        assert node.kind == expected["kind"]
        assert node.properties.get("frontend_name") == "scip-protocol"

    for expected in expectations["expected_edges"]:
        source = expected.get("source")
        if "source_symbol" in expected:
            source = _symbol_id(expected["source_symbol"])
        target = expected.get("target")
        if target is None and "symbol" in expected:
            target = _symbol_id(expected["symbol"])
        if "target_symbol" in expected:
            target = (
                _external_symbol_id(expected["target_symbol"])
                if expected.get("target_external")
                else _symbol_id(expected["target_symbol"])
            )
        edge = edge_by_key.get((source, expected["kind"], target))
        assert edge is not None, expected
        assert edge.confidence == expected["confidence"]
        if "strategy" in expected:
            assert edge.resolution.strategy == expected["strategy"]
        assert edge.properties.get("frontend_name") == "scip-protocol"
        assert not any(evidence.snippet for evidence in edge.evidence)

    assert not {edge.kind for edge in edges}.intersection(
        expectations["forbidden_edge_kinds"]
    )
    warning_kinds = {warning.kind for warning in warnings}
    assert set(expectations["expected_warnings"]).issubset(warning_kinds)
    adapter_metrics = reader.metadata["adapter_metrics"]["scip-protocol"]
    for key, value in expectations["expected_adapter_metrics"].items():
        assert adapter_metrics[key] == value
    language_tiers = reader.metadata["language_tiers"]
    for language in ("go", "java", "rust", "csharp"):
        assert language_tiers[language]["tier"] == "L2"
        assert language_tiers[language]["frontend"] == "scip-protocol"
        assert language_tiers[language]["status"] == "available"
        assert "definitions and references" in language_tiers[language]["scope"]
    query_engine = QueryEngine(tmp_path / "arcgraph")
    assert query_engine.current()["language_tiers"] == language_tiers
    assert query_engine.stats()["language_tiers"] == language_tiers
    assert reader.metadata["phase_timings"]["scip-protocol"]["scip_protocol"] >= 0


def test_build_cli_accepts_scip_graph_index(tmp_path: Path, capsys) -> None:
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
            "--scip-graph-index",
            str(FIXTURE_ROOT / "scip-index.json"),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["node_count"] > 0
    reader = GraphStoreReader.from_current(output_dir)
    assert "scip-protocol" in reader.metadata["adapter_metrics"]


def test_scip_index_precision_input_does_not_enable_protocol_frontend(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "arcgraph"
    ArcGraphIndexer(
        repo_root=FIXTURE_ROOT,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
        scip_index_path=FIXTURE_ROOT / "scip-index.json",
    ).build()
    reader = GraphStoreReader.from_current(output_dir)

    assert "scip-protocol" not in reader.metadata.get("adapter_metrics", {})
    assert not any(
        node.properties.get("frontend_name") == "scip-protocol"
        for node in reader.read_nodes()
    )


def test_scip_protocol_missing_or_malformed_input_is_visible(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    src_dir = repo_root / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "main.go").write_text("package main\n", encoding="utf-8")

    missing_output = tmp_path / "missing-output"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=missing_output,
        source_roots=[SourceRoot("src")],
        scip_graph_index_path=repo_root / "missing-scip.json",
    ).build()
    missing_warnings = {
        warning.kind
        for warning in GraphStoreReader.from_current(missing_output).read_warnings()
    }
    assert "scip_protocol_missing" in missing_warnings

    bad_scip = repo_root / "bad-scip.json"
    bad_scip.write_text("{not-json", encoding="utf-8")
    bad_output = tmp_path / "bad-output"
    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=bad_output,
        source_roots=[SourceRoot("src")],
        scip_graph_index_path=bad_scip,
    ).build()
    bad_warnings = {
        warning.kind
        for warning in GraphStoreReader.from_current(bad_output).read_warnings()
    }
    assert "scip_protocol_parse_error" in bad_warnings


def test_scip_protocol_outside_repo_document_path_is_visible(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    src_dir = repo_root / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "main.go").write_text("package main\n", encoding="utf-8")
    scip_path = repo_root / "scip-index.json"
    scip_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "relativePath": "../outside.go",
                        "occurrences": [
                            {
                                "symbol": "local ../outside.go Outside().",
                                "range": [0, 0, 0, 7],
                                "symbolRoles": 1,
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "arcgraph"

    ArcGraphIndexer(
        repo_root=repo_root,
        output_dir=output_dir,
        source_roots=[SourceRoot("src")],
        scip_graph_index_path=scip_path,
    ).build()
    reader = GraphStoreReader.from_current(output_dir)
    warnings = reader.read_warnings()

    assert "scip_protocol_path_outside_repo" in {warning.kind for warning in warnings}
    assert reader.metadata["adapter_metrics"]["scip-protocol"]["path_outside_repo"] == 1
