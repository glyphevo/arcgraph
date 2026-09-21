from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from arcgraph.core.graph_store import GraphStoreReader
from arcgraph.core.scanner import SourceRoot
from arcgraph.pipeline.indexer import ArcGraphIndexer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "typescript_baseline"


def test_istanbul_coverage_maps_repo_local_tsx_to_module_and_symbol(
    tmp_path: Path,
) -> None:
    coverage_path = tmp_path / "istanbul-coverage.json"
    coverage_path.write_text(
        json.dumps(
            {
                "src/App.tsx": {
                    "path": "src/App.tsx",
                    "statementMap": {
                        "0": {
                            "start": {"line": 5, "column": 0},
                            "end": {"line": 8, "column": 1},
                        }
                    },
                    "s": {"0": 1},
                    "fnMap": {
                        "0": {
                            "name": "App",
                            "decl": {
                                "start": {"line": 5, "column": 16},
                                "end": {"line": 5, "column": 19},
                            },
                            "loc": {
                                "start": {"line": 5, "column": 0},
                                "end": {"line": 8, "column": 1},
                            },
                        }
                    },
                    "f": {"0": 1},
                }
            }
        ),
        encoding="utf-8",
    )

    reader = _build_fixture(tmp_path, coverage_path)
    edges = reader.read_edges()
    coverage_edges = [
        edge
        for edge in edges
        if edge.kind == "covers"
        and edge.properties.get("coverage_source") == "istanbul"
    ]

    assert reader.metadata["capabilities"]["coverage"] == "available"
    assert reader.metadata["coverage_metrics"]["format"] == "istanbul"
    assert {edge.target for edge in coverage_edges}.issuperset(
        {"mod:App", "component:App.App"}
    )
    for edge in coverage_edges:
        assert edge.confidence == "runtime-only"
        assert edge.properties["path"] == "src/App.tsx"
        assert "source" not in edge.properties
        assert "text" not in edge.properties


def test_browser_coverage_maps_url_ranges_without_persisting_source_or_query(
    tmp_path: Path,
) -> None:
    source_text = (FIXTURE_ROOT / "src" / "services" / "greeting.ts").read_text(
        encoding="utf-8"
    )
    coverage_path = tmp_path / "browser-coverage.json"
    coverage_path.write_text(
        json.dumps(
            [
                {
                    "url": "http://localhost:5173/src/services/greeting.ts?token=secret#debug",
                    "text": source_text,
                    "ranges": [{"start": 0, "end": len(source_text)}],
                }
            ]
        ),
        encoding="utf-8",
    )

    reader = _build_fixture(tmp_path, coverage_path)
    coverage_edges = [
        edge
        for edge in reader.read_edges()
        if edge.kind == "covers"
        and edge.properties.get("coverage_source") == "browser_coverage"
    ]
    serialized = json.dumps(
        [edge.model_dump(mode="json") for edge in coverage_edges], sort_keys=True
    )

    assert reader.metadata["capabilities"]["coverage"] == "available"
    assert reader.metadata["coverage_metrics"]["format"] == "browser_coverage"
    assert {edge.target for edge in coverage_edges}.issuperset(
        {"mod:services.greeting", "class:services.greeting.GreetingService"}
    )
    assert "token=secret" not in serialized
    assert "#debug" not in serialized
    assert source_text not in serialized
    assert "axios.get" not in serialized


def test_browser_coverage_without_text_creates_file_level_edge_only(
    tmp_path: Path,
) -> None:
    coverage_path = tmp_path / "browser-file-coverage.json"
    coverage_path.write_text(
        json.dumps(
            [
                {
                    "url": "http://localhost:5173/src/components/Button.tsx",
                    "ranges": [{"start": 0, "end": 10}],
                }
            ]
        ),
        encoding="utf-8",
    )

    reader = _build_fixture(tmp_path, coverage_path)
    coverage_edges = [
        edge
        for edge in reader.read_edges()
        if edge.kind == "covers"
        and edge.properties.get("coverage_source") == "browser_coverage"
    ]

    assert {edge.target for edge in coverage_edges} == {"mod:components.Button"}
    assert coverage_edges[0].properties["level"] == "file"
    assert "covered_lines" not in coverage_edges[0].properties


def test_browser_coverage_reports_unsafe_or_unmatched_inputs_without_edges(
    tmp_path: Path,
) -> None:
    coverage_path = tmp_path / "browser-partial-coverage.json"
    coverage_path.write_text(
        json.dumps(
            [
                {"url": "http://localhost:5173/assets/app.bundle.js", "ranges": []},
                {"url": "file:///C:/outside/project/app.ts", "ranges": []},
                {"url": "data:text/javascript,alert(1)", "ranges": []},
                {"url": "http://localhost:5173/src/missing.ts", "ranges": []},
            ]
        ),
        encoding="utf-8",
    )

    reader = _build_fixture(tmp_path, coverage_path)
    warning_kinds = {warning.kind for warning in reader.read_warnings()}

    assert reader.metadata["capabilities"]["coverage"] == "unavailable"
    assert reader.metadata["coverage_metrics"]["status"] == "partial"
    assert reader.metadata["coverage_metrics"]["format"] == "browser_coverage"
    assert reader.metadata["coverage_metrics"]["entries_total"] == 4
    assert reader.metadata["coverage_metrics"]["entries_unmatched"] == 2
    assert reader.metadata["coverage_metrics"]["entries_unsupported"] == 2
    assert (
        reader.metadata["coverage_metrics"]["reason"] == "no_matching_coverage_targets"
    )
    assert "coverage_browser_unmatched_url" in warning_kinds
    assert "coverage_browser_unsupported_url" in warning_kinds
    assert not [
        edge
        for edge in reader.read_edges()
        if edge.kind == "covers"
        and edge.properties.get("coverage_source") == "browser_coverage"
    ]


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


def _build_fixture(tmp_path: Path, coverage_path: Path) -> GraphStoreReader:
    if shutil.which("node") is None:
        _skip_or_fail_typescript_baseline(
            "Node.js is required for the browser coverage fixture."
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
        coverage_path=coverage_path,
    ).build()
    reader = GraphStoreReader.from_current(output_dir)
    warning_kinds = {warning.kind for warning in reader.read_warnings()}
    if "typescript_frontend_unavailable" in warning_kinds:
        _skip_or_fail_typescript_baseline(
            "TypeScript compiler API is unavailable in this environment."
        )
    return reader
